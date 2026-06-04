# Changelog

## v2.9 — 2026-06-04

Grafana 11 → 13 upgrade. The deferred major bump from the v2.8 dependency
refresh, handled as its own release given the two-major jump.

### Changed
- Grafana `11.2.2` → `13.0.2`.

### Fixed
- **Installer: platform dashboards never deployed on ParallelCluster.**
  The dashboard-deploy step copied from `grafana/dashboards/${PLATFORM}`,
  but `PLATFORM` is `parallelcluster` while the subdir is named
  `pcluster`. The copy silently no-op'd (hidden by `2>/dev/null`) and the
  follow-up `rm -rf` then deleted `pcluster/`, so `logs.json` (Cluster
  Logs) and `head-node-details.json` (HeadNode Details) were lost on every
  clean PC install. PCS was unaffected (`PLATFORM=pcs` matched `pcs/`).
  Now maps `parallelcluster`→`pcluster`. (Surfaced during Grafana 13
  validation; v2.7 appeared to work only because those dashboards were
  hand-deployed during that session.)

### Compatibility audit
- No Angular panels (removed in v12): all dashboards use modern React
  panel types (timeseries, stat, table, gauge, bargauge, logs); schema
  version 39 throughout.
- No legacy alerting (removed in v11/v12) — this project ships no alert
  rules.
- Datasources are file-provisioned and referenced by name, so the v13
  removal of deprecated numeric-id data source APIs and the RBAC/Terraform
  data-source-permission changes do not apply.
- Leftover `valueName` keys on a few `stat` panels are harmless dead
  fields (the live config is in `options.reduceOptions`); left as-is.
- Grafana `GF_*` env vars and Cognito OAuth keys unchanged across 11→13.

## v2.8 — 2026-06-04

Dependency refresh. Bumps the low-risk pinned components to current
upstream releases. No functional changes. Grafana is intentionally held
at 11.2.2 (the 11→13 jump needs a separate dashboard-compatibility pass)
and DCGM exporter stays at 4.2.0-4.1.0 (Docker 29.x pull constraint, #47;
configurability tracked in #50).

### Changed
- Prometheus `v3.1.0` → `v3.12.0`
- node_exporter `v1.9.0` → `v1.11.1` (all compose files)
- Pushgateway `v1.11.2` → `v1.11.3`
- Docker Compose plugin `v2.29.7` → `v5.1.4` (AL2 / AL2023 binary install;
  Ubuntu/RHEL continue to use the distro `docker-compose-plugin` package)
- README Components table synced; dropped stale "v2" wording from the
  Compose label and installer comments.

## v2.7 — 2026-06-03

EFA fabric metrics and a searchable CloudWatch Logs dashboard. No breaking
changes. Closes the main gaps identified against the AWS HPC blog's
`observability_for_pcs` solution.

### Added
- **EFA hardware counters** via a new textfile collector
  (`custom-metrics/efa-metrics.sh` + `systemd/efa-metrics.{service,timer}`).
  node_exporter's `--collector.infiniband` only reads the standard IB
  `counters/` directory; the EFA-specific counters
  (RDMA read/write bytes, SRD retransmits, work-request errors) live in
  `/sys/class/infiniband/<dev>/ports/<port>/hw_counters/`, which it does
  not read. The collector surfaces them as `node_amazonefa_*` metrics
  (matching the upstream awsome-distributed-training EFA exporter names),
  scraped via node_exporter's textfile collector on compute and head/login
  nodes. Emits an empty file on non-EFA instances (no-op).
- **Compute Node Details**: four new EFA panels — RDMA Read Throughput,
  RDMA Write Throughput, SRD Retransmitted Packets, and Work-Request
  Errors — alongside the existing bandwidth/packet-rate panels. The
  existing bandwidth/packet panels were also migrated from the stock
  `node_infiniband_*` metrics to `node_amazonefa_*`: on real EFA hardware
  node_exporter's infiniband collector emits no `node_infiniband_*`
  series, so those panels previously stayed empty on EFA instances.
- New **Cluster Logs** dashboard (`grafana/dashboards/pcluster/logs.json`,
  ParallelCluster only): searchable CloudWatch Logs for slurmctld, slurmd,
  clustermgtd, computemgtd, cfn-init, and cloud-init, plus an all-streams
  panel. Includes a `log_group` picker (CloudWatch `logGroups` template
  variable, auto-discovers `/aws/parallelcluster/*`) and a free-text
  filter — no per-cluster token substitution needed. Replaces the old
  `logs.json.disabled` stub.

### Changed
- `compose/compute.yml` and `compose/compute.gpu.yml`: node_exporter now
  runs the textfile collector (`--collector.textfile`) so EFA metrics are
  scraped on compute nodes (head node already had it).
- `compose/head.yml`: the Grafana container now mounts the host-refreshed
  AWS credentials file (`/run/prometheus-ec2-creds`) so the CloudWatch
  datasource can read Logs under `Imds.Secured=true`.
- `custom-metrics/refresh-ec2-credentials.sh`: the credentials file is now
  world-readable (0644, tmpfs-only) so all three consumer containers can
  read it — prometheus (uid 65534), cloudwatch-exporter (uid 0), and
  grafana (uid 472/gid 0). The previous `root:65534 0640` blocked Grafana.
- `grafana/datasources/datasource.yml`: CloudWatch `defaultRegion` is now
  substituted from the cluster region (was hardcoded to an invalid
  `us-east`); the installer also `sed`s `__AWS_REGION__` here.
- `installer/install.sh`: on ParallelCluster, wires the Cluster Logs
  dashboard to the cluster's CloudWatch log group by substituting the log
  group name (from `/etc/chef/dna.json`) and ARN (built from region +
  account). Drops the dashboard if logging is disabled.

### Notes
- EFA metrics require EFA-capable instances (p4/p5/hpc6a/c5n/...). The
  required CloudWatch Logs IAM permissions are already in
  `iam/monitoring-head-node-policy.json`.
- `node_amazonefa_*` counters intentionally omit the Prometheus `_total`
  suffix to stay name-compatible with the upstream EFA node exporter.
- The Logs dashboard pins the log group ARN at install time rather than
  using a template variable: Grafana's CloudWatch logs query requires the
  ARN, and the PC log group name carries a creation-timestamp suffix that
  can't be hardcoded.
- Validated end-to-end on a live PC cluster (hpc8a.96xlarge + g6e.16xlarge,
  both EFA-enabled): EFA `node_amazonefa_*` metrics scraped with correct
  `instance_id` labels, and the Logs dashboard panels return slurmctld /
  slurmd / computemgtd rows.

## v2.6.5 — 2026-06-03

GPU monitoring fixes for AWS PCS on Docker 29.x. No breaking changes.
Contributed and validated on real PCS GPU hardware by
[@DaisukeMiyamoto](https://github.com/DaisukeMiyamoto).

### Fixed
- `compose/compute.gpu.yml`: repin dcgm-exporter from
  `4.5.2-4.8.1-ubuntu22.04` to `4.2.0-4.1.0-ubuntu22.04`. From tag
  4.2.3 onward NVCR publishes an OCI image index with attestation/SBOM
  manifests that Docker 29.x cannot pull (`error from registry:
  Incorrect Repository Format`), leaving GPU compute nodes with no
  dcgm-exporter and empty GPU dashboards. `4.2.0-4.1.0` is the newest
  tag whose manifest is a plain Docker manifest-list v2; it pulls
  cleanly on Docker 29.x and emits the same `DCGM_FI_DEV_*` fields the
  dashboards use. (#47 via #49)
- `grafana/dashboards/gpu-health.json`: remove the "GPUs with thermal
  throttle" and "GPUs with power throttle" summary tiles. They counted
  `rate(DCGM_FI_DEV_*_VIOLATION[1h]) > 0`, but those are monotonic
  accumulated-throttle-time counters, so any GPU that has ever
  throttled reads non-zero — healthy H100s were flagged red essentially
  always (16/16 on an idle cluster). The per-node throttle time-series
  panels below are kept as relative trends. (#47 via #49)

### Notes
- The earlier GPU Health XID summary-tile false-positive (also #47) was
  fixed in v2.6.4: `count_over_time(DCGM_FI_DEV_XID_ERRORS[1h]) > 0`
  (sample count, always true) → `max_over_time(...) > 0`.

## v2.6.4 — 2026-06-03

AWS PCS reliability fixes. No breaking changes. Contributed and
validated on real PCS hardware by
[@DaisukeMiyamoto](https://github.com/DaisukeMiyamoto).

### Fixed
- `prometheus/prometheus-pcs.yml`: fix Login Node dashboard HTTP 422
  (`many-to-many matching not allowed`). A PCS-managed login node
  carries the `aws:pcs:cluster-id` tag, so it was discovered by both
  the static `login_node` job and EC2 service discovery, producing
  duplicate `node_uname_info` series for one `instance_id` that broke
  the dashboard `* on(instance_id) group_left(...)` joins. Node-type
  selection now keys on the existing `monitoring-role` tag and drops
  `monitoring-role=login` from EC2 SD, so the login node is scraped
  only once. (#45 via #46)
- First-boot `Stale file handle` race: the installer extracted,
  `chown`ed, and `sed`ed the monitoring tree under the shared `/home`
  filesystem (FSx/NFS on PCS). Concurrent login + compute bootstrap
  clobbered each other's inodes, intermittently failing with
  `error reading input file: Stale file handle` and leaving nodes
  unmonitored. The tree now installs on node-local `/opt`
  (`post-install.sh`, `installer/install.sh`); compose bind-mounts
  resolve via a `__MONITORING_HOME__` token and the per-platform
  compose env file is dropped. (#48)

### Changed
- node-type selection on PCS is now decoupled from the user-facing
  `Name` tag — `Name` is free for arbitrary operator use. Everything
  left in EC2 SD after the `monitoring-role=login` drop is reported as
  `instance_name=Compute`. Backward compatible: nodes without a
  `monitoring-role` tag are treated as compute, and legacy
  `Name=Compute` nodes resolve to the same value.

## v2.6.3 — 2026-05-31

Ubuntu support for AWS PCS AMIs. No breaking changes. Contributed and
validated on the PCS Ubuntu 24.04 DLAMI base by
[@DaisukeMiyamoto](https://github.com/DaisukeMiyamoto).

### Fixed
- Enable a clean install on Ubuntu-based PCS AMIs, where the OS user is
  `ubuntu` rather than `ec2-user`:
  - `installer/install.sh`: remove an illegal top-level `local`
    declaration in the PCS login-node branch. Under Ubuntu's bash with
    `set -euo pipefail` this raised `local: can only be used in a
    function` and aborted the install (AL2023's environment happened to
    tolerate it).
  - `installer/platform/pcs.sh` and `post-install.sh`: auto-detect the
    platform user (`ubuntu` when `/home/ubuntu` exists and the `ubuntu`
    user is present, otherwise the existing `ec2-user` default).

### Notes
- Fully backward compatible with Amazon Linux 2023: when `/home/ubuntu`
  is absent, the `ec2-user` default is preserved. No changes to the
  ParallelCluster (`pcluster`) code path. (#43 via #44)

## v2.4 — 2026-05-13

DCGM coverage expansion. New GPU Health dashboard plus the Datacenter
Profiling (DCP) metrics that ML workloads need to see whether tensor
cores are doing useful work. No breaking changes.

### Added
- New **GPU Health** dashboard (`grafana/dashboards/gpu-health.json`,
  24 panels) — fault-focused view distinct from the workload-focused
  GPU Node Details:
  - Cluster-wide health summary: GPUs with XID / thermal throttle /
    power throttle / row-remap-failure (last hour)
  - XID error rate timeseries + recent-XID-events table
  - Throttle violations: thermal, power, sync-boost, board limit,
    reliability — all with per-GPU breakdown
  - Memory health: ECC SBE/DBE rates, retired pages (SBE/DBE/pending),
    remapped rows (correctable/uncorrectable), row-remap failure flag
  - Interconnect errors: NVLink CRC FLIT/DATA, replay, recovery; PCIe
    replay counter
- **GPU Node Details** ("Compute Pipeline Activity" row): SM Active /
  SM Occupancy stats, Tensor pipe activity, DRAM activity, per-precision
  pipe activity (FP64 / FP32 / FP16). Tells ML users whether the
  tensor cores they're paying for are actually being utilized.
- New `dcgm/counters.csv` — bind-mounted into the dcgm-exporter
  container via `compose/compute.gpu.yml`. Inherits the dcgm-exporter
  defaults and enables (currently commented out by default upstream):
  - All throttle/violation counters (power, thermal, sync_boost,
    board_limit, reliability, low_util)
  - ECC counters (single + double bit, volatile + persistent)
  - Retired-page counters (SBE / DBE / pending)
  - NVLink replay error counter (other NVLink errors were already on)
  - DCP profiling: `SM_ACTIVE`, `SM_OCCUPANCY`, `PIPE_TENSOR_ACTIVE`,
    `PIPE_FP64/FP32/FP16_ACTIVE`, `DRAM_ACTIVE`
  - `PCIE_REPLAY_COUNTER`, `TOTAL_ENERGY_CONSUMPTION`
- Cross-dashboard links: GPU Node Details → GPU Health, GPU Health →
  GPU Node Details + GPU Node List.

### Changed
- `installer/install.sh`: GPU compute branch now substitutes
  `__MONITORING_DIR__` in `compose/compute.gpu.yml` and passes the
  `cfn_cluster_user` env (parity with the head node compose
  invocation). Required for the new dcgm counters bind-mount.

### Notes
- Profiling metrics (`DCGM_FI_PROF_*`) require Volta+ GPUs. All current
  AWS GPU instances (p3/p4/p5/g4dn/g5/g6) qualify; older p2 instances
  will simply not report them.
- All 58 PromQL expressions in the new and modified dashboards (excluding
  pre-existing `$__interval` Grafana-templated queries) pass
  `promtool check rules` validation.
- The XID code surfaced by `DCGM_FI_DEV_XID_ERRORS` is the raw NVIDIA
  XID number — refer to the
  [NVIDIA XID error guide](https://docs.nvidia.com/deploy/xid-errors/)
  for what each code means. Common ones to investigate first: 31 (GPU
  memory page fault), 43 (GPU stopped processing), 48 (DBE error),
  63/64/65 (row remap related), 79 (GPU has fallen off the bus).

## v2.3 — 2026-05-13

Slurm coverage expansion. New dashboard plus partition/user/account/scheduler
metrics on the existing Cluster Summary. No breaking changes.

### Added
- New **Slurm Detail** dashboard (`grafana/dashboards/slurm-detail.json`):
  - Per-partition table (nodes alloc/idle/down, CPUs alloc/idle, CPU load)
  - Pending-jobs-by-reason timeseries + bar gauge (`slurm_pending_reason_total`)
  - Top users table (CPUs/mem allocated, jobs running/pending)
  - Top accounts table (same shape)
  - Account quota utilization (CPU/mem/job limits vs allocated)
  - Scheduler health: controller threads, DBD agent queue size, backfill
    cycles, last backfill depth
  - Top RPC message types and top users by RPC count
    (`slurm_rpc_msg_type_*`, `slurm_rpc_user_*`)
  - Slurm license usage (only renders if licenses configured)
- **Cluster Summary** additions (existing dashboard):
  - Idle node-hours (last 24h) — quantifies wasted compute time
  - Idle CPU-hours (last 24h)
  - Top 5 users by allocated CPUs (bar gauge)
  - Top 5 partitions by allocated CPUs (bar gauge)
  - Dashboard link to "Slurm Detail" in the top-right of the page
- PCS scrape: added `/metrics/jobs-users-accts` endpoint scrape
  (every 120s — Slurm docs warn this endpoint is unbounded, see
  `prometheus/prometheus-pcs.yml` for tuning notes).
- PCS compat rules expanded: 16 recording rules now translate native
  Slurm 25.11 metric names (`slurm_user_jobs_*`, `slurm_account_jobs_*`,
  `slurm_partition_*`, `slurm_bf_*`, `slurm_sched_*`) to the rivosinc
  names the dashboards expect.

### Notes
- All 66 PromQL expressions across the modified dashboards pass
  `promtool check rules` validation.
- The new user/account panels on PCS depend on the unbounded
  `/metrics/jobs-users-accts` endpoint. If your slurmctld has 100k+
  users or accounts, comment out that scrape job in
  `prometheus/prometheus-pcs.yml`.
- Some scheduler panels (`Controller Threads`, `DBD Agent Queue`)
  show "No data" on PCS — Slurm's openmetrics plugin does not yet
  expose these as native metrics.

## v2.2 — 2026-05-13

Bug fixes and refactors on top of v2.1. No breaking changes.

### Fixed
- `slurm-job-nodes` textfile collector now finds `squeue`/`scontrol`
  on PCS AMIs (probes `/opt/slurm/bin` and `/opt/aws-pcs/slurm/bin`,
  falls back to PATH).
- `post-install.sh` default ref bumped from `v2.0` to `v2.1` to match
  documented version.

### Changed
- `prometheus.yml`: replaced 60-line GPU/accelerator instance-type
  allowlist with `parallelcluster:node-type` tag filter (present since
  PC 3.0). Discovery scoped to current cluster via
  `parallelcluster:cluster-name`.
- Example `pcluster.yaml` refreshed: pinned to v2.1, points at
  `iam/render-policy.sh` output, drops overbroad managed policies from
  compute fleet.

### Refactored
- `installer/common.sh`: dropped dead `*_IMAGE` env vars (compose files
  are the source of truth).

### Documentation
- `CHANGELOG.md` rewritten from v2.0/v2.1 commit messages.
- README Quickstart now includes Cognito SSO section.

## v2.1 — 2026-05-12

AWS PCS support, GPU Node List dashboard, dashboard polish. Layers on top
of v2.0; ParallelCluster behaviour unchanged.

### Added
- **AWS PCS** (Parallel Computing Service) support end-to-end:
  - Platform detection module (`installer/platform/{platform,pcs,parallelcluster}.sh`)
    exporting a uniform `PLATFORM`, `PLATFORM_NODE_TYPE`, `PLATFORM_CLUSTER_NAME`,
    `PLATFORM_REGION`, `PLATFORM_USER`. Detects PCS via `/etc/aws-pcs/`
    or `aws:pcs:*` IMDS instance tags.
  - `prometheus/prometheus-pcs.yml`: scrapes Slurm's native OpenMetrics
    endpoints on `slurmctld:6817` (`/metrics/{nodes,jobs,partitions,scheduler}`).
    Requires Slurm 25.11+ with `MetricsType=metrics/openmetrics` +
    `CommunicationParameters=enable_http`. No extra exporter process.
  - `prometheus/rules/pcs-compat.yml`: recording rules that translate
    `slurm_nodes_{idle,alloc,mixed,down,drain}` →
    `slurm_node_count_per_state{state=...}` so the existing dashboards
    work unchanged on PCS.
  - PCS-specific dashboard: `grafana/dashboards/pcs/login-node-list.json`.
- **GPU Node List** dashboard (`gpu-node-list.json`): fleet table view
  with click-through into the per-GPU `gpu.json` detail dashboard.
  Available on both platforms.
- Platform-specific dashboard layout: `grafana/dashboards/pcluster/`
  and `grafana/dashboards/pcs/` directories. Installer copies the right
  set based on detected platform.
- Documentation screenshots in `docs/screenshots/`.
- Dual-platform Quickstart in README.

### Changed
- README rewritten for dual-platform install.
- Cost-metrics and Grafana-password-refresh scripts detect platform via
  IMDS `aws:pcs:cluster-id` tag in addition to the legacy `cfnconfig`
  path.
- `compose/head.yml` mounts `prometheus/rules/` so recording rules load.
- `ParallelCluster.json` rebuilt against the dual-platform metric set
  (now uses `slurm_node_count_per_state` exclusively, no legacy field
  dependencies).

### Fixed
- HeadNode dashboard moved into `grafana/dashboards/pcluster/` so it
  doesn't surface on PCS clusters where it has no data.

## v2.0 — 2026-05-11

Ground-up rewrite. Validated on ParallelCluster 3.15 + Amazon Linux 2023.
No breaking changes to end-user pcluster.yaml structure.

### Added
- OS-aware installer (`installer/install.sh` + `installer/os/*.sh`)
  supporting Amazon Linux 2, Amazon Linux 2023, Ubuntu 22.04, Ubuntu 24.04,
  and RHEL / Rocky / Alma / CentOS Stream 9.
- Docker Compose v2 plugin installation on every supported OS.
- GPU detection via `lspci` / `/dev/nvidia0` — no longer depends on
  parsing the EC2 instance-type string (works with p4de, p5, p5e, p5en,
  g5, g6, g6e, and future families).
- `nvidia-container-toolkit` installation on GPU compute nodes
  (replaces deprecated `nvidia-docker2`).
- Per-cluster Grafana admin password in SSM Parameter Store
  (SecureString) at `/parallelcluster/<cluster>/grafana/admin-password`,
  materialized into a tmpfs file by `refresh-grafana-password.sh`.
  Handles the post-init `grafana cli admin reset-admin-password` flow
  so the SSM-managed password actually wins after the DB is initialized.
- Optional Cognito SSO via SSM SecureString JSON at
  `/parallelcluster/<cluster>/grafana/cognito` →
  `/run/grafana-secrets/cognito.env` env-file. See `cognito/README.md`.
- Least-privilege IAM policy (`iam/monitoring-head-node-policy.json` +
  `iam/render-policy.sh`) replacing four AWS-managed policies. Region-
  scoped, resource-scoped to the cluster's own stack and SSM path.
- `Imds.Secured=true` compatibility: host-side systemd timer
  (`refresh-ec2-credentials.sh`) writes IMDS role credentials to
  `/run/prometheus-ec2-creds/credentials` (tmpfs, 0640, owner
  `root:65534`) bind-mounted read-only into the Prometheus container.
  Architecture in `docs/imds-secured-design.md`.
- Slurm job-to-node textfile collector (`slurm-job-nodes.sh` + systemd
  timer) producing `slurm_job_node{jobid,nodename,user,jobname,state}`
  for the Compute Node List dashboard.
- Self-signed nginx TLS certificate with multi-SAN (localhost, private
  IP, private hostname). 4096-bit RSA, 10-year validity. Optional
  ALB+ACM path documented in `docs/public-access.md`.

### Changed
- `prometheus-slurm-exporter`: replaced unmaintained GPLv3
  `vpenso/prometheus-slurm-exporter` (built from source on every cluster
  boot) with Apache-2.0 `rivosinc/prometheus-slurm-exporter` v1.8.0
  prebuilt binary downloaded from GitHub releases.
- Pinned every container image to an explicit version tag; removed
  every `:latest` reference. Grafana 11.2.2, Prometheus v3.1.0,
  node_exporter v1.9.0, pushgateway v1.11.2, nginx 1.27-alpine,
  DCGM exporter 4.5.2.
- Removed the `version:` key from all compose files (ignored by
  Compose v2).
- Compose files moved from `docker-compose/` to `compose/` and
  renamed `master` → `head` to match ParallelCluster terminology.
- Cost-metrics: collapsed `1m-cost-metrics.sh` + `1h-cost-metrics.sh`
  into a single `cost-metrics.sh`. Pricing API responses cached for
  24 hours. Cron `MAILTO=""` set to silence email spam (closes #15).
- Dashboards now use Grafana template variables; the old sed token
  replacement for `<HOSTNAME>`, `<INSTANCE_ID>`, etc. is gone.
- `aws-region.py` migrated from deprecated `pkg_resources` to
  `pathlib`.

### Fixed
- S3 cost tier detection referenced an undefined `$VAR` variable
  (fell through to the `Inf` tier for all buckets > 50 TB).
- FSx cost script now handles the `PERSISTENT_2` deployment type.
- EBS cost script defaults to `gp3` (the PC 3.x default) instead of
  `gp2`.
- `install-monitoring.sh` was Amazon-Linux-only (`yum install docker`
  hardcoded) and failed on Ubuntu and AL2023.

### Removed
- `parallelcluster-setup/install-monitoring.sh` (replaced by
  `installer/install.sh`).
- `docker-compose/` directory (replaced by `compose/`).
- In-tree `git clone` + `go build` of the slurm exporter on the
  HeadNode. Removes the `golang-bin` install and the
  `go mod download && go build` step.

## v0.9 and earlier

See git history for pre-modernization releases.
