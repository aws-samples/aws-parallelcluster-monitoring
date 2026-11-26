# How the Cluster Costs estimates are made

The **Cluster Costs** dashboard estimates what an AWS ParallelCluster or AWS
PCS cluster costs while it runs: the cost per hour now, since tracking
started, this month and projected for the month, broken down by component,
queue, instance type and purchase option, with idle cost and per-user and
per-account estimates. The figures are estimates from public prices, not
your AWS bill.

This page describes where the numbers come from, what they leave out, how
the collector reports its own health, and how to troubleshoot it.

- [What the dashboard shows](#what-the-dashboard-shows)
- [Sources and formulas](#sources-and-formulas)
- [Per-user and per-account cost](#per-user-and-per-account-cost)
- [Idle and unallocated cost](#idle-and-unallocated-cost)
- [What's left out](#whats-left-out)
- [Accumulation, gaps and months](#accumulation-gaps-and-months)
- [Health, stages and error codes](#health-stages-and-error-codes)
- [Troubleshooting](#troubleshooting)
- [IAM](#iam)
- [Metric reference](#metric-reference)
- [Who can see the figures and who can change them](#who-can-see-the-figures-and-who-can-change-them)
- [PCS login node groups without `monitoring-role=login`](#pcs-login-node-groups-without-monitoring-rolelogin)
- [Supported partitions](#supported-partitions)
- [Upgrade notes](#upgrade-notes)
- [Accuracy and validation](#accuracy-and-validation)

## How it works

A collector, `/usr/local/bin/cluster-cost-collector`, runs as root on the
monitoring node (the ParallelCluster head node or the PCS login node that
runs the monitoring stack) from `cluster-cost.timer`, every 60 seconds and
60 seconds after boot. Each run:

1. finds the cluster's running instances (`ec2:DescribeInstances`) and the
   EBS volumes attached to them (`ec2:DescribeVolumes`);
2. reads Slurm's nodes and running jobs (`scontrol show nodes --json` and
   `squeue --json`);
3. refreshes the cached prices that are due: On-Demand, EBS and AWS PCS list
   prices (`pricing:GetProducts`), Spot prices
   (`ec2:DescribeSpotPriceHistory`) and, on PCS, the cluster's size and
   accounting mode (`pcs:GetCluster`) and node group names
   (`pcs:ListComputeNodeGroups`);
4. prices every instance, volume and PCS fee, and splits the compute node
   cost among Slurm users and accounts;
5. adds the run to the accumulated totals in
   `/var/lib/aws-parallelcluster-monitoring/cost/state.json`;
6. writes the rates, the totals and the health of the run to
   `/var/lib/prometheus/node-exporter/cluster_cost.prom`, which node_exporter
   serves and Prometheus scrapes.

Prometheus recording rules (`prometheus/rules/cost.yml`) add the total rate,
the projection for the month and a freshness signal. A run ends within
120 seconds, and the dashboard shows a change in the fleet within about
2 minutes.

The installer writes the configuration to
`/etc/aws-parallelcluster-monitoring/cluster-cost.env`: `PLATFORM`
(`parallelcluster` or `pcs`), `CLUSTER_ID` (the ParallelCluster cluster name
or the PCS cluster ID) and `REGION`. Re-running the installer keeps the
accumulated totals.

## What the dashboard shows

| Row | Panels |
|---|---|
| About these numbers | A notice: the sources, what's left out, and the per-user model. |
| Collector health | **Status** (OK, Degraded or Not reporting), **Last update** (seconds since the collector wrote its file), **Unpriced resources**, **Unaccounted time this month** (hours, for "Cost lines" and for "Users and idle"), **Unpriced resource-hours this month**, and the **Stages** table with each stage's status and error code. |
| Totals | **Est. cost per hour**, **Cost since start**, **Tracking since**, **Cost this month**, **Projected this month**. |
| Breakdown now ($/h) | **By component** (Head node, Login nodes, Compute instances, EBS volumes, PCS controller, PCS node management, PCS accounting), **By queue** (on PCS: by compute node group), **By instance type**, **Spot vs On-Demand**. |
| Breakdown this month ($) | The same four panels for the month-to-date totals. |
| Idle and unallocated | **Idle nodes**, **Idle node cost per hour**, **Unallocated on busy nodes per hour**, **Idle and unallocated this month**, **Idle cost by queue and type**, and **Over time** (idle and unallocated cost per hour). |
| Users and accounts | **Allocated to jobs per hour**, **Top users now**, **Top accounts now**, the **Users** and **Accounts** tables (now, selected range, this month, since start), and **Per user over time**. |
| Users and accounts, detail | A collapsed row with the **Users and accounts** table: one row per (user, account) pair, with the same four columns. |
| Over time | **Cost per hour by component** (stacked), **Cost per hour by purchase option**, **Cost this month, running total**. |

Rates and the projection show only while the collector's file is fresh,
that is, written less than 5 minutes ago. When the collector stops, they
read "No data" and the status turns red ("Not reporting") instead of
showing the last values. The accumulated totals keep their last value,
which stays correct.

Some panels are empty on purpose:
- With no running job, the per-user and per-account panels have no rows, and
  **Allocated to jobs per hour** reads 0.
- When the `slurm` stage fails (`NodeMappingIncomplete` included), the idle,
  unallocated and per-user rate panels read "No data". The month and
  since-start columns keep their last values.
- The **Error code** column of the Stages table is empty while every stage is
  OK.
- **Unpriced resources** reads 0 only when discovery succeeded, and "No data"
  otherwise.
- The "Selected range" columns of the Users and Accounts tables are limited
  by Prometheus retention (15 days by default), and their headers say so.

## Sources and formulas

All prices are public list prices in USD. Every rate is in USD per hour.

### Instances

- **On-Demand**: `pricing get-products` for `AmazonEC2`, filtered on the
  region, the instance type, the operation (the instance's
  `UsageOperation`, which tells Linux, RHEL, SUSE and Ubuntu Pro apart), the
  tenancy (`Shared` or `Dedicated`) and `capacitystatus=Used`. The filter
  matches exactly one product. A price is cached for 24 hours, and a new
  instance type is priced in the run where it first appears.
- **Spot**: `ec2:DescribeSpotPriceHistory` at the run's time, for every
  running Spot (instance type, Availability Zone, product description). The
  price is refreshed every 5 minutes, and at once when a new Spot (type, AZ)
  appears. The product description comes from the operation: Linux/UNIX,
  Red Hat Enterprise Linux or SUSE Linux. Other operating systems, such as
  Ubuntu Pro, aren't priced on Spot (`spot_platform_unsupported`).
- **Capacity Blocks** and **Dedicated Hosts** have no public hourly price, so
  they are listed as unpriced (`capacity_block`, `dedicated_host`). They
  don't mark the estimate as Degraded. Their EBS volumes and PCS node fees
  are still counted.

The purchase option comes from the instance's lifecycle: `spot`,
`capacity_block`, or `on_demand` for everything else.

### EBS volumes

The collector lists the volumes attached to the running cluster instances
in every run, one call per 100 instances, and counts each volume once, also
a multi-attach volume. Prices come from `pricing get-products` once a day.
With `H` = 24 × the number of days in the current UTC month:

| Volume type | Hourly rate |
|---|---|
| every type | `Size (GiB) × storage price / H` |
| gp3 | plus `max(0, IOPS − 3000) × IOPS price / H` and `max(0, throughput − 125 MiB/s) × throughput price / H` |
| io1 | plus `IOPS × IOPS price / H` |
| io2 | plus the three IOPS tiers: up to 32,000, 32,001 to 64,000, and above 64,000 |
| gp2, st1, sc1, standard | storage only. Magnetic I/O request charges aren't estimated. |

When a call that lists volumes fails, the collector uses each instance's
volume records from an earlier run if they are at most one hour old.
Otherwise that instance's volumes are left out, the instance counts as one
unpriced volume (`volume_list_unavailable`), and the estimate is Degraded.

### AWS PCS fees

- **Controller**: the hourly price for the cluster's size (Small, Medium or
  Large), while the cluster is ACTIVE, UPDATING or UPDATE_FAILED.
- **Accounting**: the hourly price for the cluster's size, in the same
  states, when Slurm accounting is `STANDARD`.
- **Node management**: for every running instance that a PCS node group
  launched, login node groups included. The advanced tier applies to the
  P and Trn instance families, the standard tier to the others.

The size, status and accounting mode come from `pcs get-cluster`, refreshed
every 10 minutes and used for up to 24 hours. Without them (the call has
never succeeded, or its last result is older than 24 hours), the controller
line is unpriced (`kind="fee"`). The accounting line is unpriced too when
the last known result had accounting `STANDARD`, and left out when the
collector has no cluster details at all, because it can't tell whether
accounting is on. PCS accounting storage, billed per GB-month, isn't
estimated: no API reports the size of the accounting database.

On PCS, the queue breakdown uses compute node group names, because a PCS
queue can span several node groups. Until `pcs list-compute-node-groups`
has named a group, its ID is used.

### Worked example

A ParallelCluster cluster in us-east-1, in a 30-day month (`H` = 720), with
example list prices: c6i.large On-Demand $0.085 per hour, gp3 storage $0.08
per GB-month, gp3 IOPS $0.005 per IOPS-month and gp3 throughput $0.04 per
MiB/s-month. The collector always uses the prices it fetches.

| Resource | Rate (USD/h) |
|---|---|
| Head node, c6i.large On-Demand | 0.085 |
| Its root volume, gp3 40 GiB, 3000 IOPS, 125 MiB/s | 40 × 0.08 / 720 = 0.004444 |
| Compute node `od`, c6i.large On-Demand, gp3 40 GiB | 0.085 + 0.004444 = 0.089444 |
| Compute node `spot`, c6i.large Spot at $0.0321, gp3 40 GiB | 0.0321 + 0.004444 = 0.036544 |
| **Total** | **0.215433** |

A gp3 volume of 500 GiB with 6000 IOPS and 250 MiB/s adds
500 × 0.08 / 720 + 3000 × 0.005 / 720 + 125 × 0.04 / 720
= 0.055556 + 0.020833 + 0.006944 = 0.083333 USD per hour.

On PCS, a Small cluster with accounting `STANDARD` adds the controller
($0.5825 per hour in us-east-1), accounting ($0.17) and $0.08 per hour of
node management for each instance that a node group launched.

## Per-user and per-account cost

Per-user cost covers compute node cost only: a compute instance's rate,
plus its priced volumes and its priced PCS node management fee. Head and
login nodes and the PCS controller and accounting fees are shared cost and
aren't split.

A running job's share of a node is the largest of its CPU share, its GPU
share and its memory share of that node. Memory counts only when Slurm
schedules memory (`SelectTypeParameters` with `CR_*_MEMORY`). With memory
scheduling off, as on ParallelCluster by default, the memory figure of a job
reserves nothing. The node's totals are the CPUs and memory that jobs can
use, without what Slurm reserves for itself (`CoreSpecCount`,
`CpuSpecList`, `MemSpecLimit`). A job with whole nodes (`--exclusive`) has a
share of 1. When the shares on a node add up to more than 1, they are
scaled down to 1.

Each (user, account) gets node cost × share. A job without an account
counts under `account="none"`. A user whose UID doesn't resolve on the
monitoring node shows as `uid:<id>`.

Example: a compute node c6i.large with a node cost of $0.089444 per hour,
2 CPUs and 3600 MiB of memory for jobs. `user1` runs a job with 1 CPU and
3000 MiB under account `A`.
- Memory not scheduled (`CR_CPU`): the share is 1/2. `user1`/`A` gets
  $0.044722 per hour, and the other $0.044722 is unallocated.
- Memory scheduled (`CR_CPU_MEMORY`): the share is max(1/2, 3000/3600) =
  0.8333. `user1`/`A` gets $0.074537 per hour, and $0.014907 is unallocated.
- A g4dn.xlarge node has 1 GPU, so a job that holds the GPU and 1 of its
  4 CPUs has a share of 1.
- Two jobs with shares of 0.75 and 0.5 on one node are scaled to 0.6 and
  0.4.

Slurm text fields such as job names and comments are never used: the
collector reads Slurm's JSON. Accumulated (user, account) pairs are capped
at 5,000 per period. Beyond that, new pairs go to
`username="(other)",account="(other)"`. Without Slurm accounting, users can
pass any `--account` string, which is why the cap exists.

## Idle and unallocated cost

- **Idle node**: a running, priced compute instance that holds no job. A
  compute instance that maps to no Slurm node is idle too. Its whole node
  cost is idle cost (`reason="idle_node"`).
- **Unallocated**: the part of a busy node that no job holds
  (`reason="partial_node"`), node cost × (1 − the sum of the shares).

Per-user + idle + unallocated cost equals the compute node cost of every
priced compute instance, now and in the accumulated totals.

On ParallelCluster, a dynamic node that finished its job is idle until
`ScaledownIdletime` ends. A node that is booting (POWERING_UP) holds its
CONFIGURING job but costs nothing until its instance runs.

The collector maps each Slurm node to its instance by `instance_id`, then by
private IP address, then by private DNS name. If a node that runs a job maps
to no running instance, the run fails the `slurm` stage with
`NodeMappingIncomplete` and exports no idle, unallocated or per-user rates,
because the job's instance would otherwise show as idle. Nodes that are
booting, powering down, DOWN or NOT_RESPONDING, and CONFIGURING and
COMPLETING jobs don't count for this check. In the minute or so after a Spot
interruption, before Slurm marks the node, one run can still report it.

## What's left out

- Discounts, Savings Plans, Reserved Instances, credits and taxes.
- Data transfer, public IPv4 addresses ($0.005 per address-hour), NAT
  gateways and load balancers.
- FSx and EFS file systems, EBS snapshots, CloudWatch, and Marketplace
  software charges of an AMI.
- Capacity Block and Dedicated Host instances (their volumes and PCS fees
  are counted).
- PCS accounting storage, and the PCS accounting fee while the collector has
  no cluster details at all (see [AWS PCS fees](#aws-pcs-fees)).
- Magnetic (`standard`) EBS I/O request charges.
- Instances in Local Zones, which have their own price list region: they
  show as unpriced.
- Cost before the collector's first run on the monitoring node. On
  ParallelCluster that is a few minutes after the head node boots. On PCS it
  is when the login node was installed.

The month and since-start totals leave out a resource for the time it was
unpriced or its volumes couldn't be listed, and such an instance counts as
one volume. The **Unpriced resource-hours this month** stat shows how many
resource-hours that is.

## Accumulation, gaps and months

Each run adds the average of the previous and the current rate times the
elapsed time to the totals. For example, a total rate of $0.20 per hour
followed one minute later by $0.26 adds (0.20 + 0.26) / 2 × 60 / 3600 =
$0.003833.

- A gap of up to one hour (a reboot, an API outage, a stopped timer) is
  filled the same way.
- For a longer gap, for example a head node stopped for three hours, the
  collector adds one hour at the last rates and reports the rest as
  unaccounted time. **Unaccounted time this month** shows it, separately for
  the cost lines ("Cost lines") and for the per-user, idle and unallocated
  totals ("Users and idle"). The second family also counts the time before
  its first good Slurm run.
- Months are UTC calendar months. A gap that crosses a month boundary is
  split at the boundary.
- **Since start** counts from the collector's first successful run on this
  node. **Tracking since** shows when that was.
- **Projected this month** is the month-to-date cost plus the average cost
  per hour of the last 24 hours, for the hours left in the UTC month. When
  tracking started this month, the projection covers only the tracked part
  of the month.
- The month and since-start totals leave out a resource for the time it was
  unpriced. **Unpriced resource-hours this month** shows how much.

The totals live in `state.json`, with a backup copy `state.json.bak`. If
`state.json` is unreadable, the collector uses the backup. If both are
unreadable, tracking starts again and the `state` stage fails with
`StateUnreadable` for that run; the unreadable file is kept as
`state.json.corrupt`.

Tracking starts again from zero when the monitoring node changes: a
replaced PCS login node, or a new head node. With several PCS login nodes
that run the monitoring stack, each one reports the whole cluster's cost,
with its own start time.

## Health, stages and error codes

The **Status** stat reads:
- **OK**: every stage that applies succeeded, and nothing is unpriced except
  Capacity Block and Dedicated Host instances.
- **Degraded**: a stage failed, or a resource is unpriced for another reason.
  The Stages table names the failing stage and its error code. Figures that
  depend on it are missing or come from cached prices. A failure is never
  shown as a zero.
- **Not reporting**: the collector's file is older than 5 minutes or isn't
  served.

### Stages

| Stage | What it does | Codes besides the AWS codes |
|---|---|---|
| `discovery` | Lists the running cluster instances: on ParallelCluster by the `parallelcluster:cluster-name` tag, on PCS by `aws:pcs:cluster-id` and `pcs-cluster-id`. When it fails, the run exports no rates, only the accumulated totals and the health. | `NoInstancesFound` |
| `ebs` | Lists the attached volumes (every run) and refreshes the EBS prices (daily). | |
| `slurm` | Finds the Slurm client, reads nodes and jobs, and maps nodes to instances. When it fails, there are no idle, unallocated or per-user rates. | `SlurmNotFound`, `SlurmCommandFailed`, `Timeout`, `SlurmJsonInvalid`, `NodeMappingIncomplete` (no AWS codes) |
| `on_demand_prices` | Refreshes the On-Demand prices of the running instance types. | |
| `spot_prices` | Refreshes the Spot prices. | |
| `pcs` (PCS only) | Refreshes the cluster details, the node group names and the PCS fee prices. | |
| `state` | Saves `state.json` and the price cache. | `StateWriteFailed`, `StateUnreadable` (no AWS codes) |
| any | An unexpected error in the collector. | `InternalError` |

Each failed stage shows one code. A stage whose source keeps failing stays
failed in every run until a call succeeds, also while the collector waits
before it tries again. After `AccessDenied`, `Throttled`, `CredentialsError`,
`EndpointUnreachable`, `Timeout` or `AwsCliNotFound`, the collector stops
calling that source for a while: 15 minutes for Pricing, 5 minutes for
Spot prices and 10 minutes for the PCS API. Cached prices stay in use.

### Error codes

| Code | Meaning | What to do |
|---|---|---|
| `AwsCliNotFound` | The `aws` command can't be run. | Install the AWS CLI v2 in `/usr/local/bin` or `/usr/bin`. |
| `Timeout` | A call didn't finish in time, or there was no time left in the run. | See [EndpointUnreachable or Timeout](#endpointunreachable-or-timeout). |
| `EndpointUnreachable` | The node couldn't connect to the AWS endpoint or the proxy. | See [EndpointUnreachable or Timeout](#endpointunreachable-or-timeout). |
| `CredentialsError` | No valid credentials. | Check the node's instance profile and IMDS access. The collector uses only the instance profile. |
| `AccessDenied` | The instance profile lacks a permission. | Add the permission for that stage (see [IAM](#iam)). |
| `Throttled` | AWS throttled the call. | Nothing; the collector backs off and retries. |
| `AwsApiError` | Another AWS error for one request. | Read the WARNING in the journal, which has the first 200 characters of the error. |
| `InvalidResponse` | The call succeeded but its output wasn't what the collector expects. | Report it with the journal lines. |
| `AwsCliError` | The AWS CLI failed in another way. | Read the WARNING in the journal. |
| `NoInstancesFound` | No running instance carries the cluster's tag. | Check `CLUSTER_ID` and `REGION` in `/etc/aws-parallelcluster-monitoring/cluster-cost.env`. |
| `SlurmNotFound` | No directory with executable `squeue` and `scontrol`. | On ParallelCluster, check `/opt/slurm/bin`. On PCS, check the `/etc/aws/pcs/scheduler/slurm` symlink. |
| `SlurmCommandFailed` | `scontrol` or `squeue` failed or returned errors. | Run `scontrol show nodes --json` as root and check that slurmctld answers. |
| `SlurmJsonInvalid` | Slurm's JSON couldn't be parsed. | Report it with your Slurm version. |
| `NodeMappingIncomplete` | A Slurm node that runs a job maps to no running instance. | Check `slurm.busy_unmapped_nodes` in `last-run.json`. Right after a Spot interruption it clears by itself. |
| `StateWriteFailed` | `state.json` couldn't be saved. | Check free space and that `/var` is writable. The run exits with 1. |
| `StateUnreadable` | `state.json` and its backup were unreadable, so tracking started again. | Nothing for the next runs. The unreadable file is `state.json.corrupt`. |
| `InternalError` | A bug in the collector. | Report it with the traceback from the journal. |

### Unpriced reasons

`cluster_cost_unpriced_resources{kind, reason}` counts the resources left
out of the estimate. `kind` is `instance`, `volume` or `fee`.

| Reason | Meaning |
|---|---|
| `not_in_price_list` | The price list has no product for it, for example a new instance type or a Local Zone instance. Retried every 6 hours. |
| `ambiguous_price` | Several distinct prices match. The journal lists the SKUs. Retried every 6 hours. |
| `price_api_error` | The price call failed, or its source is backed off, and nothing is cached. |
| `price_lookup_pending` | The run had no time left or used its 20 Pricing calls. A later run fetches it. The stage stays OK. |
| `spot_price_unavailable` | No Spot price: the Spot call failed or is backed off and no cached price under 6 hours old exists, or AWS returned none. |
| `spot_platform_unsupported` | A Spot instance whose operating system has no Spot price description, such as Ubuntu Pro. |
| `unknown_volume_type` | The EBS price list has no price for that volume type. |
| `invalid_record` | An instance or volume record failed validation. |
| `volume_list_unavailable` | A running instance whose volumes couldn't be listed, and no records under one hour old exist. Counted once per instance. |
| `capacity_block` | A Capacity Block instance. Not priced by design; doesn't make the estimate Degraded. |
| `dedicated_host` | An instance on a Dedicated Host. Not priced by design; doesn't make the estimate Degraded. |

## Troubleshooting

Start with:

```bash
systemctl status cluster-cost.timer cluster-cost.service
sudo journalctl -u cluster-cost --since "-15min"
sudo python3 -m json.tool /var/lib/aws-parallelcluster-monitoring/cost/last-run.json
```

Each run logs one line such as `run done: 0.215433000 USD/h, 3 instances,
3 volumes, 1 users, 2.4 s, failed stages: none`, plus one WARNING per failed
call. `last-run.json` (root only) records each stage's result, every
instance with its role, queue, purchase option and price, every volume and
fee, the Slurm node mapping and the shares per node.

A dry run runs every stage and prints what it would write. It takes no lock
and writes nothing:

```bash
sudo bash -c 'set -a; . /etc/aws-parallelcluster-monitoring/cluster-cost.env;
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
  exec /usr/local/bin/cluster-cost-collector --dry-run'
```

For DEBUG logging, add `Environment=CLUSTER_COST_DEBUG=1` in a drop-in, as
for the proxy below.

### EndpointUnreachable or Timeout

The node needs a route to the regional EC2 endpoint, to the PCS endpoint on
PCS, and to the Pricing endpoint in us-east-1. A cluster in another region
without a NAT gateway or a proxy lacks the last one.

Behind a proxy, add a drop-in:

```ini
# /etc/systemd/system/cluster-cost.service.d/proxy.conf
[Service]
Environment=HTTPS_PROXY=http://<proxy>:<port>
Environment=NO_PROXY=169.254.169.254,localhost,127.0.0.1
```

then run `sudo systemctl daemon-reload`. `NO_PROXY` keeps the instance
metadata requests direct. The installer never writes into
`cluster-cost.service.d`, so the drop-in survives re-runs. A proxy set in
`/etc/environment` or in a login shell's profile doesn't reach a systemd
service, although it reached the old cron job on some operating systems.

Depending on the AWS CLI version, a proxy that accepts connections and never
answers shows as `EndpointUnreachable` (CLI 2.33.0 and 2.38.0) or `Timeout`
(CLI 2.31.35). Both back off the source the same way.

### CredentialsError or AccessDenied

The collector uses only the node's instance profile. It ignores root's
`~/.aws` files and profiles: the unit sets `AWS_CONFIG_FILE` and
`AWS_SHARED_CREDENTIALS_FILE` to `/nonexistent`, and the collector sets
them again for every call. It also ignores credential variables exported in
a root shell, such as `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN` and `AWS_PROFILE`, `--dry-run` included. So a
`CredentialsError` or `AccessDenied` always points at the instance profile.
Variables such as `AWS_ENDPOINT_URL_*`, the proxy variables and the retry
settings still apply.

### Not reporting

If the journal shows runs that exit with 2, the configuration is invalid
and nothing is written. The ERROR line says why. One cause is the textfile
directory: every install sets `/var/lib/prometheus/node-exporter` to
root:root 0755, and the collector refuses to write (exit 2, "Not reporting")
when that directory is owned by another user or writable by group or
others, for example after a `chmod g+w`. Textfile writers that don't run as
root must write elsewhere or run as root. Re-running the installer restores
the owner and mode. The state directory,
`/var/lib/aws-parallelcluster-monitoring/cost`, has the same checks.

If the journal shows good runs:
1. Query `up{job=~"ec2_instances|login_node",instance_name="HeadNode"}` in
   Prometheus. It is 0 or absent when Prometheus has lost the monitoring
   node's target. On ParallelCluster that target comes from EC2 service
   discovery and the head node's `Name=HeadNode` tag.
2. Query `node_textfile_scrape_error{job=~"ec2_instances|login_node",instance_name="HeadNode"}`.
   It is 1 when any file in the textfile directory failed to parse, and
   `docker logs node-exporter` names the file. When it is
   `cluster_cost.prom`, please report it. When it is another collector's
   file, the cost figures aren't affected.

After a reboot, the status reads "Not reporting" for up to about a minute,
until the first run.

### price_lookup_pending for more than a few minutes

Runs end before prices can be fetched. Check
`cluster_cost_collector_run_duration_seconds` and the journal for slow
calls.

## IAM

The collector makes these calls, with the instance profile of the
monitoring node:

| Action | Used for | If it's denied |
|---|---|---|
| `ec2:DescribeInstances` | the running cluster instances | `discovery` fails: no rates. |
| `ec2:DescribeVolumes` | the attached EBS volumes | `ebs` fails; after one hour every instance counts as a `volume_list_unavailable` volume. |
| `ec2:DescribeSpotPriceHistory` | Spot prices | `spot_prices` fails; after 6 hours Spot instances are unpriced. |
| `pricing:GetProducts` | EC2, EBS and PCS list prices | `on_demand_prices`, `ebs` or `pcs` fails; cached prices stay in use. |
| `pcs:GetCluster` (PCS) | cluster size, status and accounting mode | `pcs` fails; after 24 hours the controller fee is unpriced. |
| `pcs:ListComputeNodeGroups` (PCS) | node group names for the queue breakdown | `pcs` fails; node group IDs are shown instead of names. |

Without one of them, the dashboard reads Degraded and names the stage with
`AccessDenied`. It never shows zeros instead.

- **ParallelCluster**: the least-privilege policy in
  [iam/README.md](../iam/README.md) grants the EC2 actions in the cluster's
  region and `pricing:GetProducts`. With the managed-policy quick start in
  the [README](../README.md), add `AmazonEC2ReadOnlyAccess`: none of the
  other managed policies, and not the head-node role that ParallelCluster
  creates, grants `ec2:DescribeSpotPriceHistory`.
- **AWS PCS**: the login node's instance profile needs the six actions above,
  as listed in the README's PCS permissions. Compute node groups need only
  `pcs:RegisterComputeNodeGroupInstance`.

The Pricing API is called in us-east-1, whatever the cluster's region.

## Metric reference

Every series comes from the monitoring node's node_exporter, so it carries
that target's labels: `job` (`ec2_instances` on ParallelCluster,
`login_node` on PCS), `instance`, `instance_name="HeadNode"`, and on
ParallelCluster `instance_type` and the other `ec2_instances` labels. Select
them with the matcher S:

```
job=~"ec2_instances|login_node",instance_name="HeadNode"
```

for example `sum(cluster_cost_usd_per_hour{job=~"ec2_instances|login_node",instance_name="HeadNode"})`.
S limits a query to the monitoring node's own figures, so a series with the
same name from another source can't change the result. Recording-rule
outputs have no `job` label; read them with `{job=""}`.

To group cost by instance type, use `ec2_instance_type`. `instance_type` on
these series is the monitoring node's own type, a scrape target label.

| Metric | Labels | Meaning |
|---|---|---|
| `cluster_cost_usd_per_hour` | `component` (`instance`, `ebs`, `pcs_node_management`, `pcs_controller`, `pcs_accounting`), `node_role` (`head`, `login`, `compute`), `queue`, `ec2_instance_type`, `purchase_option` (`on_demand`, `spot`, `capacity_block`) | Rate per cost line |
| `cluster_cost_user_usd_per_hour` | `username`, `account` | Compute node cost held by running jobs |
| `cluster_cost_unallocated_usd_per_hour` | `reason` (`idle_node`, `partial_node`), `queue`, `ec2_instance_type`, `purchase_option` | Compute node cost that no job holds; 0 rows when nothing is idle |
| `cluster_cost_idle_nodes` | `queue`, `ec2_instance_type`, `purchase_option` | Running, priced compute instances with no job |
| `cluster_cost_nodes` | `node_role`, `queue`, `ec2_instance_type`, `purchase_option` | Running cluster instances, priced or not |
| `cluster_cost_accumulated_usd` | `period` (`since_start`, `month`), plus the labels of `cluster_cost_usd_per_hour` | Accumulated cost lines |
| `cluster_cost_user_accumulated_usd` | `period`, `username`, `account` | Accumulated per user and account |
| `cluster_cost_unallocated_accumulated_usd` | `period`, `reason`, `queue`, `ec2_instance_type`, `purchase_option` | Accumulated idle and unallocated cost |
| `cluster_cost_period_start_timestamp_seconds` | `period` | Start of tracking, and of the month (the later of the month start and the tracking start) |
| `cluster_cost_unaccounted_seconds` | `period`, `family` (`lines`, `alloc`) | Seconds not integrated: gaps over one hour, and for `alloc` the time before its first good run |
| `cluster_cost_unpriced_resources` | `kind`, `reason` | Resources left out; only non-zero counts |
| `cluster_cost_unpriced_resource_seconds` | `period`, `kind`, `reason` | Resource-seconds that the totals leave out because the resource was unpriced |
| `cluster_cost_collector_up` | | 1 when the run produced a complete estimate, 0 otherwise |
| `cluster_cost_collector_stage_ok` | `stage` | 1 or 0 for each stage that ran |
| `cluster_cost_collector_stage_error` | `stage`, `code` | 1 for each failed stage, with its code |
| `cluster_cost_collector_last_success_timestamp_seconds` | | Last run with `cluster_cost_collector_up` 1 |
| `cluster_cost_collector_run_duration_seconds` | | Duration of the run |
| `cluster_cost_price_age_seconds` | `source` (`on_demand`, `spot`, `ebs`, `pcs`, `volumes`) | Age of the oldest cached value used |
| `cluster_cost_collector_info` | `platform`, `cluster`, `region` | Always 1 |

The recording rules add:
- `cluster_cost_collector:fresh`: present while the collector's file is less
  than 5 minutes old;
- `cluster:cluster_cost_usd_per_hour:sum`: the total rate, while fresh;
- `cluster:cluster_cost_projected_month_usd:avg_24h`: the projection for the
  month, while fresh.

### From the old metrics

Releases before this one pushed `cluster_cost_per_hour` and
`cluster_cost_accumulated` to the Pushgateway (`job="cost"`). They are
removed. Custom dashboards or alerts on them must move to the new names, in
which S is `job=~"ec2_instances|login_node",instance_name="HeadNode"`:

| Old | New |
|---|---|
| `cluster_cost_per_hour{component="total"}` | `cluster:cluster_cost_usd_per_hour:sum{job=""}` |
| `cluster_cost_per_hour{component="headnode"}` | `sum(cluster_cost_usd_per_hour{S, component="instance", node_role=~"head\|login"})`. On PCS it now covers every login node, not only the monitoring node. |
| `cluster_cost_per_hour{component="compute"}` | `sum(cluster_cost_usd_per_hour{S, component="instance", node_role="compute"})` |
| `cluster_cost_per_hour{component="ebs"}` | `sum(cluster_cost_usd_per_hour{S, component="ebs"})` |
| `cluster_cost_per_hour{component="pcs_controller"}` | `sum(cluster_cost_usd_per_hour{S, component="pcs_controller"})` |
| `cluster_cost_accumulated` | `sum(cluster_cost_accumulated_usd{S, period="since_start"})` |

## Who can see the figures and who can change them

Per-user and per-account figures go to Prometheus, so anyone who can query
Prometheus or reach node_exporter can read them:
- every Grafana user who can open the dashboard, including Cognito
  self-signups, who get the Viewer role. Grafana OSS can restrict who opens
  the dashboard, with dashboard or folder permissions, but not who queries
  the data source: Editors and Admins can read any series in Explore;
- every local user on the monitoring node, through Prometheus on loopback,
  which has no authentication;
- anyone who can open an SSM port-forwarding session to the node, through
  nginx's `/prometheus/`. Other network clients get 403 there;
- every host that can reach the monitoring node's port 9100. node_exporter
  listens on all interfaces without authentication, and serves
  `cluster_cost.prom`. On ParallelCluster, the head node's security group
  admits all traffic from the compute nodes, so any Slurm job can read the
  figures with `curl http://<head node>:9100/metrics`. On PCS, the login
  node's security groups decide. The same port already serves
  `slurm_jobs.prom`, with usernames and job names.

The Slurm Detail dashboard already shows usernames, accounts and CPU
allocation to the same audience. Like those panels, the per-user figures
don't apply Slurm's `PrivateData` setting.

Only these can change the figures:
- root on the monitoring node. Only root can write to
  `/var/lib/prometheus/node-exporter` (root:root 0755) and to the state
  directory (0750, files 0640);
- the cluster's default user (`cfn_cluster_user` on ParallelCluster;
  `ec2-user`, `ubuntu` or `rocky` on PCS). The installer adds it to the
  `docker` group, which is root-equivalent, and makes it the owner of
  `/opt/aws-parallelcluster-monitoring`, which holds the rule file, the
  Prometheus configuration and the dashboard;
- whatever answers on the monitoring node's port 9100. A local user who binds
  that port while node_exporter is down would be scraped as the monitoring
  node;
- on ParallelCluster, AWS principals that can tag instances. An instance
  tagged with the cluster's name, `parallelcluster:node-type=HeadNode` and
  `Name=HeadNode` would be scraped as a second head node.

Prometheus reads the cost figures only from the monitoring node's
node_exporter: every rule and panel selects them with S. The Pushgateway
accepts unauthenticated pushes from every local user, so Prometheus drops
pushed series named `cluster_cost_*`, `cluster:cluster_cost_*` or
`node_textfile_*` at scrape. Exporters run by Slurm jobs on compute nodes
and RES desktops can't change a figure either. If you maintain your own
`prometheus.yml`, keep the drop rule of the `pushgateway` job.

## PCS login node groups without `monitoring-role=login`

On PCS, the collector treats an instance as a login node when it has
`monitoring-role=login` or wasn't launched by a compute node group. An
instance of a login node group without that tag counts as compute, as the
installer and Prometheus service discovery already treat it: it shows in
the queue breakdown under its group's name and, because no Slurm node maps
to it, as an idle node. Its cost is in the total either way. When the
group's launch template doesn't run the monitoring installer, tag its
instances `monitoring-role=login`.

## Supported partitions

- **`aws`**: supported.
- **AWS China** (`cn-*` regions): rejected. The collector exits with "AWS
  China regions are not supported: their price lists are in CNY", and the
  dashboard reads "Not reporting".
- **AWS GovCloud (US)** and the **AWS European Sovereign Cloud**: not
  validated. The Pricing endpoint in us-east-1 doesn't accept their
  credentials, so prices fail with a credentials or access error, and the
  dashboard shows it.

## Upgrade notes

Upgrading from an earlier release, in place or with a new cluster:
- The installer removes the old `cost-metrics.sh` cron entry from root's
  crontab and keeps every other line. If only the `MAILTO=""` line that old
  releases added remains, it removes root's crontab. If other entries
  remain, that `MAILTO=""` line stays; remove it to get cron mail back.
- Accumulated cost starts again from zero. The old accumulator and price
  cache in `/var/lib/prometheus/cost-cache` and the old Pushgateway group
  `job="cost"` are deleted.
- The installer restarts Prometheus on head and login nodes on every run,
  to load `rules/cost.yml` and the Pushgateway drop rule. The restart leaves
  a scrape gap of a few seconds, or of several minutes on a large Prometheus
  while it replays its write-ahead log.
- Add the new IAM permissions ([IAM](#iam)) before you upgrade, or Spot
  nodes, volumes and PCS node group names read as unpriced or failed until
  you do.
- A node that reaches AWS through a proxy needs the
  [proxy drop-in](#endpointunreachable-or-timeout).
- Head and login nodes need `python3` 3.7 or later. The installer installs it
  from the OS repositories when it's missing.

## Accuracy and validation

For the resources it covers, the estimate uses the same list prices as the
AWS price list and the current Spot price. Your bill differs by what's
[left out](#whats-left-out) and by any discount. EBS rates use the number
of days in the current month, as EBS bills per GB-month.

The release's live validation compares the collector's figures with an
independent calculation from the same AWS responses and Slurm allocations,
within $0.000001 per hour: instance prices, cost lines, breakdowns, fees,
users, accounts, idle and unallocated cost. It also injects failures (denied
permissions, an unreachable endpoint, a stopped collector) and checks the
health the dashboard shows.

Validated live:
- ParallelCluster 3.16.x on Amazon Linux 2023 (On-Demand, Spot and GPU
  queues, upgraded in place from v2.13) and Ubuntu 24.04;
- AWS PCS with Slurm 25.11, upgraded in place to 26.05, on the Amazon
  Linux 2023 sample AMI, with accounting `STANDARD`;
- the standalone PCS login node's discovery and fees.

Covered by unit tests only:
- the Slurm 24.11 JSON of older ParallelCluster versions;
- multi-node GPU jobs;
- RHEL 10, Rocky Linux 10 and Ubuntu 26.04, whose AWS CLI 2.33.0 and
  2.31.35 error formats are unit-test fixtures, except the operating system
  of a PCS login-node smoke test on a v2.13 AMI, if that test runs.
