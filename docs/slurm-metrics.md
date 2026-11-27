# Slurm metrics on ParallelCluster

On an AWS ParallelCluster head node, the Slurm dashboards get their data from
one of two sources:

- **Slurm's native metrics.** slurmctld serves them itself, over HTTP, when
  `MetricsType=metrics/openmetrics` is set (Slurm 25.11 and later). On
  ParallelCluster, Prometheus scrapes them at `localhost:6820/metrics/*`; 6820
  is the first port of ParallelCluster's `SlurmctldPort` range, 6820-6829.
  AWS PCS clusters use the same endpoints, on port 6817 of the PCS
  slurmctld.
- **The Slurm exporter**,
  [rivosinc/prometheus-slurm-exporter](https://github.com/rivosinc/prometheus-slurm-exporter)
  1.8.0. It runs on the head node as `slurm_exporter.service`, on
  127.0.0.1:9092, and reads `sinfo`, `squeue` and `sdiag`. On 2026-04-30 a
  maintainer wrote that it's probably time to sunset the project, and that
  slurmctld's native metrics should be the preferred method
  ([rivosinc/prometheus-slurm-exporter#132](https://github.com/rivosinc/prometheus-slurm-exporter/issues/132)).

The installer uses native metrics when slurmctld serves them, and the Slurm
exporter otherwise. A head node uses one source at a time: on the native path
the exporter isn't installed.

Native metrics fix the job panels, which read 0 with the exporter, but have
no data for some panels that the exporter feeds. See
[What changes on the native path](#what-changes-on-the-native-path).

- [Requirements](#requirements)
- [Enable native metrics](#enable-native-metrics)
- [How the installer chooses](#how-the-installer-chooses)
- [Check which source a head node uses](#check-which-source-a-head-node-uses)
- [Switch back to the Slurm exporter](#switch-back-to-the-slurm-exporter)
- [What changes on the native path](#what-changes-on-the-native-path)
- [Security](#security)
- [Cost](#cost)

## Requirements

- **ParallelCluster 3.15 or later**, which ship Slurm 25.11. Validated on
  ParallelCluster 3.16.3 (Slurm 25.11.8) with Amazon Linux 2023, Ubuntu
  24.04 and RHEL 9 head nodes. Not validated: ParallelCluster 3.15, arm64
  head nodes, Ubuntu 22.04, clusters with Slurm accounting or memory-based
  scheduling, and large clusters.
- **Amazon Linux 2023 on ParallelCluster 3.15** may serve no HTTP at all.
  ParallelCluster 3.16.0 fixed the dynamic linker's discovery of the
  http-parser library on Amazon Linux 2023, which slurmctld's HTTP listener
  needs. Without it, the installer uses the Slurm exporter.
- **Never set `MetricsType` on ParallelCluster 3.14.** It ships Slurm 24.11,
  which has no `MetricsType` key: an unknown key is a parsing error in
  slurm.conf, and slurmctld doesn't start.
- **`PrivateData` stays at ParallelCluster's default**, `PrivateData=cloud`,
  which Slurm 25.11 reads as no restriction. With any real value, such as
  `PrivateData=jobs`, slurmctld answers the metrics endpoints with HTTP 401
  for every user, root included, and the installer uses the Slurm exporter.
  Slurm 25.11.8 has no setting that exempts the metrics.
- **HTTP stays enabled.** `CommunicationParameters=disable_http` turns the
  endpoints off.
- If the cluster config sets `CustomSlurmSettingsIncludeFile` instead of
  `CustomSlurmSettings`, add the line `MetricsType=metrics/openmetrics` to
  that file.

## Enable native metrics

Add `MetricsType` to the cluster config:

```yaml
Scheduling:
  Scheduler: slurm
  SlurmSettings:
    CustomSlurmSettings:
      - MetricsType: metrics/openmetrics
```

A new cluster uses native metrics from its creation.

On a running cluster:

1. Update the cluster with the new config:

   ```bash
   pcluster update-cluster --cluster-name <cluster-name> --cluster-configuration <config.yaml>
   ```

   In tests on ParallelCluster 3.16.3, the update needed no compute fleet
   stop: slurmctld restarted, and running and suspended jobs kept running.
2. Re-run the installer on the head node. `pcluster update-cluster` doesn't
   run it, so until then the head node keeps its Slurm exporter:

   ```bash
   sudo bash /opt/aws-parallelcluster-monitoring/installer/install.sh
   ```

   Re-running `post-install.sh` with a version, as for an upgrade, works too.

   For 5 minutes after the switch, Prometheus still returns the last samples
   of the `slurm_exporter` scrape job: Total Nodes and the other node and
   CPU counts read double, and the panels that native metrics can't feed
   show the exporter's last values.

## How the installer chooses

On every run on a ParallelCluster head node, the installer requests
`http://127.0.0.1:6820/metrics/jobs` and chooses the source from the response:

| Response | Meaning | Source |
|---|---|---|
| HTTP 200, with the `slurm_jobs` series | slurmctld serves native metrics | native |
| HTTP 200, empty | `MetricsType` isn't set | Slurm exporter |
| HTTP 401 | `PrivateData` is set | Slurm exporter |
| No HTTP response in 6 tries, 5 s apart | Slurm older than 25.11 (ParallelCluster 3.14), `CommunicationParameters=disable_http`, the http-parser library missing (Amazon Linux 2023 on ParallelCluster 3.15), or a changed `SlurmctldPort` | Slurm exporter |

Any other response also selects the Slurm exporter. The request tests what
Prometheus will scrape, so it needs no version check and no parsing of
slurm.conf.

- **Native:** the installer writes the five native scrape jobs into
  Prometheus's configuration. It stops and disables `slurm_exporter.service`,
  and deletes its unit and `/usr/bin/prometheus-slurm-exporter`, if they
  exist.
- **Slurm exporter:** the installer writes the `slurm_exporter` scrape job
  and installs the exporter, as in earlier releases, with the SHA-256 check
  of its release tarball.

The Slurm scrape jobs sit between the lines `  # BEGIN slurm_metrics` and
`  # END slurm_metrics` of
`/opt/aws-parallelcluster-monitoring/prometheus/prometheus.yml`. Every run
replaces those lines with `prometheus/scrape-slurm-native.yml` or
`prometheus/scrape-slurm-exporter.yml`, so edits between them last only until
the next run. A `prometheus.yml` without both lines, for example one you
maintain yourself, keeps its Slurm jobs: the installer logs a warning and
uses the Slurm exporter.

The installer chooses only when it runs: at cluster creation, at a re-run and
at an upgrade. A running head node doesn't switch when `MetricsType` changes.

## Check which source a head node uses

On the head node:

- The install log names the source and the reason of the last
  `post-install.sh` run, at cluster creation or at an upgrade:

  ```bash
  sudo grep 'Slurm metrics source' /var/log/parallelcluster-monitoring-install.log
  ```

  ```
  [monitoring-install] Slurm metrics source: native (slurmctld serves them on 127.0.0.1:6820)
  ```

  A re-run of `install.sh` prints the same line in its own output, and
  doesn't write the install log. After such a re-run, the install log still
  names the source of the last `post-install.sh` run, so use the two checks
  below.

- `systemctl status slurm_exporter` reports that the unit can't be found on
  the native path, and that it is active on the Slurm exporter path.
- Prometheus's scrape jobs:

  ```bash
  curl -s http://127.0.0.1:9090/api/v1/targets | jq -r '.data.activeTargets[] | "\(.labels.job) \(.scrapeUrl) \(.health)"' | grep slurm
  ```

  On the native path, the jobs are `slurm_openmetrics`,
  `slurm_openmetrics_jobs`, `slurm_openmetrics_partitions`,
  `slurm_openmetrics_scheduler` and `slurm_openmetrics_jobs_users_accts`, on
  `localhost:6820`. On the Slurm exporter path, the job is `slurm_exporter`,
  on `localhost:9092`.

## Switch back to the Slurm exporter

Remove `MetricsType` from the cluster config, run `pcluster update-cluster`,
then re-run the installer as in [Enable native metrics](#enable-native-metrics).
The installer installs the Slurm exporter again.

Until the re-run, the head node keeps its native scrape jobs, and they get no
data. Without `MetricsType`, slurmctld still answers every endpoint with HTTP
200 and an empty body, so Prometheus marks the targets up, stores no samples,
and the Slurm panels read 0 or show no data.
`scrape_samples_scraped{job=~"slurm_openmetrics.*", job!="slurm_openmetrics_jobs_users_accts"} == 0`
shows this state. It leaves out the job `slurm_openmetrics_jobs_users_accts`,
whose endpoint is also empty when the cluster has no jobs.

To keep the Slurm exporter on a cluster, don't set `MetricsType`.

## What changes on the native path

### The service, the scrape jobs and the labels

- `slurm_exporter.service` and `/usr/bin/prometheus-slurm-exporter` are
  removed, and nothing listens on 127.0.0.1:9092.
- The `slurm_exporter` scrape job is replaced by five jobs on
  `localhost:6820`, each with the label `slurm_endpoint`:
  `slurm_openmetrics` (`/metrics/nodes`), `slurm_openmetrics_jobs`,
  `slurm_openmetrics_partitions` and `slurm_openmetrics_scheduler`, every
  30 s, and `slurm_openmetrics_jobs_users_accts`, every 120 s. Series of the
  job `slurm_exporter` stop.
- The `partition` label holds the partition's name, such as `cpu`. In its
  node and CPU series, the Slurm exporter marks the default partition with
  `sinfo`'s `*`, as in `cpu*`.
- On a cluster without Slurm accounting, jobs have no account, and the
  per-account series have no `account` label. The Slurm exporter reports
  `account="(null)"`. The account column of the Top Accounts panel is then
  empty.

### Metric names

The recording rules in
[`prometheus/rules/pcs-compat.yml`](../prometheus/rules/pcs-compat.yml),
which AWS PCS uses too, record the Slurm exporter's names that the dashboards
query. Custom dashboards and alerts on other exporter names must move to the
native names. The table lists every name the Slurm exporter exported in the
validation, and the native series that replaces it. The equivalences were
checked on the same cluster, at the same moment, with both sources.

| Slurm exporter | Native path |
|---|---|
| `slurm_node_count_per_state`, `slurm_partition_node_count` | Recorded by the rules, with the states `idle`, `idle~`, `alloc`, `mix`, `drng`, `down` and `drain` |
| `slurm_cpus_per_state` | Recorded by the rules, with the states `idle`, `alloc` and `idle~`. `idle` and `alloc` are the free and allocated CPUs of all nodes; the Slurm exporter files each node's CPUs under the node's state |
| `slurm_partition_alloc_cpus`, `slurm_partition_idle_cpus` | Recorded by the rules |
| `slurm_backfill_cycle_counter`, `slurm_backfill_last_depth`, `slurm_daemon_thread_count`, `slurm_dbd_agent_queue_size` | Recorded by the rules, from `slurm_bf_cycle_cnt`, `slurm_bf_last_depth`, `slurm_server_thread_cnt` and `slurm_slurmdbd_queue_size` |
| `slurm_user_state_total`, `slurm_user_cpu_alloc`, `slurm_account_job_state_total`, `slurm_account_job_state_cpu_alloc` | Recorded by the rules, with the job states `RUNNING` and `PENDING`, and `RUNNING` only for the CPUs |
| `slurm_partition_total_cpus` | `slurm_partition_cpus` |
| `slurm_partition_real_mem`, `slurm_partition_free_mem` | `slurm_partition_nodes_mem_tot`, `slurm_partition_nodes_mem_free`, in megabytes. The Slurm exporter reports the same figures multiplied by 10^6 |
| `slurm_cpus_total`, `slurm_cpus_idle` | `sum(slurm_node_cpus)`, `sum(slurm_node_cpus_idle)` |
| `slurm_mem_real`, `slurm_mem_free`, `slurm_mem_alloc` | `sum(slurm_node_memory_bytes)`, `sum(slurm_node_memory_free_bytes)`, `sum(slurm_node_memory_alloc_bytes)`. Slurm 25.11 reports megabytes in these series, despite the `_bytes` suffix |
| `slurm_backfill_cycle_count` (the total time of the backfill cycles, in µs), `slurm_backfill_last_depth_try_sched` | `slurm_bf_cycle_tot`, `slurm_bf_last_depth_try` |
| `slurm_partition_job_state_total{partition, state}` | One series per state, such as `slurm_partition_jobs_running{partition}` and `slurm_partition_jobs_pending{partition}` |
| `slurm_pending_reason_total`, `slurm_rpc_msg_type_avg_time`, `slurm_rpc_msg_type_count`, `slurm_rpc_msg_type_total_time`, `slurm_rpc_user_count`, `slurm_rpc_user_total_time`, `slurm_cpu_load`, `slurm_partition_cpu_load`, `slurm_user_mem_alloc`, `slurm_account_job_state_mem_alloc`, `slurm_backfill_job_count`, `slurm_partition_weight`, `slurm_feature_cpu_alloc`, `slurm_feature_mem_alloc`, `slurm_feature_total` | No equivalent |
| `slurm_diag_scrape_duration`, `slurm_job_scrape_duration`, `slurm_node_scrape_duration`, `slurm_diag_scrape_error`, `slurm_node_scrape_error` | `scrape_duration_seconds` and `up` of the jobs `slurm_openmetrics.*` |

The native endpoints serve 252 metric names in Slurm 25.11.8, all gauges. See
[Slurm's metrics documentation](https://slurm.schedmd.com/metrics.html).

### Panels

These panels read 0 with the Slurm exporter and show data on the native path:

- Cluster Summary: **Jobs Running**, **Jobs Pending**, **Jobs Completed**,
  **Jobs Failed** and **Jobs Over Time**;
- Slurm Detail: **Total Pending Now**, **Total Running Now**,
  **Failed (recent)** and **Suspended**.

These have no data on the native path, as on AWS PCS:

- Slurm Detail **Pending Jobs by Reason** and **Top Pending Reasons (now)**:
  native metrics have no pending reasons;
- Slurm Detail **Top RPC Message Types (avg time)** and **Top Users by RPC
  Count**: native metrics have no RPC statistics;
- the **CPU Load** column of the Slurm Detail **Partitions** table;
- the memory columns of the Slurm Detail **Top Users** and **Top Accounts**
  tables (Mem Alloc (B)) and of **Account Quota Utilization** (Mem Used (B)).

When the four panels are empty, they say why. The descriptions of the four
tables name their empty column. The Job ID and Job Name columns of Compute
Node List come from the `slurm-job-nodes` collector, on both paths.

Other differences:

- **Completed and failed jobs.** slurmctld holds a job for `MinJobAge` after
  it ends, 300 s by default, and the native series count only the jobs it
  holds. Jobs Completed, Jobs Failed and Failed (recent) count the jobs that
  ended in the last few minutes.
- **Jobs waiting for their nodes.** A job whose nodes are still starting,
  such as a job on a ParallelCluster dynamic node that is powering up,
  counts as running in Jobs Running, Total Running Now, Jobs Over Time, Top
  Users and Top Accounts. `squeue` shows it as CONFIGURING.
- **Suspended jobs.** Their CPUs count as allocated in Top users (CPUs
  allocated) on Cluster Summary, and in Top Users, Top Accounts and Account
  Quota Utilization on Slurm Detail.
- **Controller Threads** reads 0 on an idle controller. With the Slurm
  exporter it reads 1, because the exporter's own sdiag request counts as a
  thread.
- **Node states.** Slurm counts nodes per partition and per state flag, not
  per node, so the rules derive the states that `sinfo` shows. These cases
  stay approximate:
  - a drained node that is also down, in a partition with idle nodes,
    makes Nodes Idle read one low and Nodes Down/Drain one high;
  - a powered-down node that is also down or drained (`down~`, `drain~`), in
    a partition with powered-up idle nodes, moves one idle node to `idle~`,
    so Idle node-hours (last 24h) counts one idle node too few;
  - draining nodes are taken out of `mix` before `alloc`, so Node States can
    split the busy nodes differently from `sinfo`; Nodes Allocated is exact;
  - the CPUs of powered-down nodes, which Idle CPU-hours (last 24h) leaves
    out, are estimated from each partition's CPUs per node, which is exact
    when all nodes of a partition have the same CPU count;
  - CPUs Idle also counts the free CPUs of drained, draining and down nodes,
    which `sinfo` counts as other.

## Security

- **No authentication.** Any local user on the head node, and any process
  on any node of the cluster, can read the metrics: in tests, root, the
  cluster's default user and `nobody` got them, on the head node and from a
  compute node. They show what `sinfo` and `squeue` show every user
  of the cluster: job counts per state, per user and per account, node and
  partition states, and scheduler statistics.
- slurmctld listens on every port of 6820-6829 on all interfaces. The head
  node's default security group admits only the cluster's compute security
  group and SSH, so nothing outside the cluster can reach these ports. Don't
  open them wider with an additional security group.
- `PrivateData` closes the endpoints to every user (HTTP 401). The installer
  then uses the Slurm exporter.

## Cost

Measured on a 5-node cluster with ParallelCluster 3.16.3:

- a Prometheus scrape took 0.9-1.5 ms per endpoint;
- after 25 minutes of scrapes, slurmctld had used 1.0 % CPU on average since
  its start 35 minutes earlier, with 14.7 MB of resident memory.

`/metrics/jobs-users-accts` has 29 series per user and 29 per account, one per
job state, and Slurm's documentation warns that it grows without bound with
the number of users and accounts. Prometheus scrapes it every 120 s. On a
cluster with many users, check Prometheus's memory. To stop scraping it,
remove the job `slurm_openmetrics_jobs_users_accts` from
`/opt/aws-parallelcluster-monitoring/prometheus/scrape-slurm-native.yml` and
re-run the installer with `install.sh`, as above. Top users (CPUs allocated)
on Cluster Summary, and Top Users, Top Accounts and Account Quota Utilization
on Slurm Detail, are then empty. An upgrade restores the job.
