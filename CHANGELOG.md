# Changelog

## Unreleased

Adds CI, and removes the account-specific values from `test-clusters/`.
Neither changes what the installer deploys.

### Added

- **CI.** A GitHub Actions workflow, `.github/workflows/ci.yml`, runs on
  every push to `main` and on every pull request. It needs no AWS account
  and no secrets, and runs:
  - shellcheck on every shell script, yamllint on every YAML file, and
    actionlint on the workflow;
  - `promtool check config` on `prometheus/prometheus.yml` and
    `prometheus/prometheus-pcs.yml`, rendered as the installer renders them,
    each with and without the RES snippet; `promtool check rules` on
    `prometheus/rules/` and on every Prometheus query in the dashboards; and
    `promtool test rules` on the rule unit tests;
  - `docker compose config` on the three compose files, with checks that the
    head node's services keep listening on 127.0.0.1 and that only
    Prometheus, Grafana and cloudwatch-exporter get the group that reads the
    credentials file; `nginx -t` on `nginx/conf.d/`; and cloudwatch-exporter's
    `--check-config` on its rendered configuration;
  - checks that every dashboard is valid JSON, that the UIDs are unique in
    the ParallelCluster and the AWS PCS layout, and that every
    `/grafana/d/<uid>` link on the landing page and in the dashboards opens
    a dashboard of the same layout;
  - a check of the relative links and anchors in the Markdown files;
  - the cost collector's unit tests, on Python 3.7 and 3.14.

  The tools are pinned by version and SHA-256, and the checkout action by
  commit SHA. The workflow's token can only read the repository. The
  Prometheus, nginx and cloudwatch-exporter checks run the images that
  `compose/head.yml` pins. The check scripts are in `.github/ci/` and also
  run locally.

### Changed

- **`test-clusters/` has no account-specific values.** The scripts no longer
  contain subnet, VPC, FSx, key pair or AMI IDs, or a Region. They read
  their parameters from `test-clusters/config.env`, which git ignores, or
  from the environment: `REGION` and `SUBNET_ID` are required, and `REF`
  and `REPO` choose what the nodes install (`main` by default). The VPC
  comes from the subnet, and the AMIs from a lookup by name. The old values
  remain in the git history.
- **`test-clusters/` matches the current platforms.**
  - `pc-create.sh` writes the ParallelCluster configuration to
    `.state/pc-cluster.yaml`, which replaces `pc-cluster.yaml`, and
    `--dryrun` validates it without creating anything. The head node gets
    the README's quick-start policies, `AmazonEC2ReadOnlyAccess` included.
    The Python virtual environment that pcluster 3.15 needed is gone.
  - On AWS PCS, the cluster runs Slurm 26.05 by default. The login and CPU
    nodes use the newest AWS PCS sample AMI and the GPU node the newest
    PCS-ready DLAMI, which has the NVIDIA driver. The login node is a node
    group in no queue instead of a separate instance. The IAM roles have the
    permissions that the README lists, and the instance tags come from the
    launch templates, as in the README's quickstart.
  - `test-clusters/README.md` describes the parameters, the resources and
    their prices.

### Documentation

- **Roadmap.** The CI item is done. hadolint is dropped from it, because
  the repository has no Dockerfiles.
- **v3.0 on RHEL 10, Rocky Linux 10 and Ubuntu 26.04.** After its release,
  on 2026-11-27, v3.0 was validated live on these operating systems on AWS
  PCS with Slurm 26.05.5, with a login node and a CPU compute node each.
  [docs/cost-estimates.md](docs/cost-estimates.md) moves them from "Covered
  by unit tests only" to "Validated live", and the README's section on them
  says what the v3.0 and v2.13 validations covered.

## vNEXT — 2026-11-27

Adds support for AWS PCS compute node groups in standard configuration,
which have no launch template. Since 2026-11-12 the PCS console creates
node groups this way by default. Their nodes get no user data, and only a
custom launch template can set instance metadata options such as instance
tags, which the installer needed to find the cluster and the node's role:
it stopped with `Cannot detect platform`. `post-install.sh` now runs there
as a PCS node lifecycle action, on login, CPU and GPU nodes. Node groups
that use a launch template with user data keep working with the same
configuration.

### Added

- **Install as a node lifecycle action.** Add `post-install.sh` to a node
  group at the `nodeBootstrapped` stage, with the arguments
  `--role login latest` on the login node group and `latest` on compute
  node groups. The README gives the console fields and the
  `--node-lifecycle-actions` JSON. The installer takes the cluster and the
  compute node group from the `PCS_CLUSTER_ID` and
  `PCS_COMPUTE_NODE_GROUP_ID` variables that the PCS agent exports to
  lifecycle scripts. It needs PCS agent 1.5.0-1 or later.
- **`post-install.sh --role login|compute`** (also `--role=<role>`,
  anywhere among the arguments) sets the node's role. Any other value exits
  with status 2 before anything is downloaded. Without `--role`, the role
  comes from the `monitoring-role` instance tag as before, else the node is
  a compute node. The positional `[ref] [repo_slug]` arguments are
  unchanged.
- **The installer saves each PCS node's identity** in
  `/etc/aws-parallelcluster-monitoring/pcs-node.env` (root, 0644): the
  instance ID, cluster ID, compute node group ID and role. A re-run of
  `sudo bash /opt/aws-parallelcluster-monitoring/installer/install.sh`, for
  Cognito SSO or RES, uses it when the lifecycle action's variables and the
  instance tags are missing, so a standard-configuration login node stays a
  login node. The file is parsed, never sourced, and only counts on the
  instance that wrote it, so the instances of an AMI baked from a monitored
  node don't inherit its role. The install log names the source it used:
  `PCS identity from node lifecycle action`, `instance tags` or
  `saved identity`.

### How a login node is identified in standard configuration

This was an open design decision. **A node is a login node when its node
group's lifecycle action passes `--role login`.** The login node then
records its own compute node group, and Prometheus EC2 service discovery
and the cost collector treat every instance of that node group as a login
node.

Why: standard configuration gives a node no user data, no launch-template
tags and no instance metadata tags, so nothing on the node or in EC2 can
carry a role, and the PCS API has no role field for a node group. Lifecycle
actions are configured per node group and take arguments, so the role
belongs there, and the PCS agent exports the node group ID that ties the
role to the fleet. This needs no tags and no new IAM permission, and
Prometheus and the collector classify instances the same way.

Rejected:
- **Queue membership** (a node group in no queue is a login group). It
  needs `pcs:ListQueues` and `pcs:GetQueue` on the login node's role, which
  v3.0 deferred as a change for existing users. It also isn't final when the
  login node installs: the console lets a node group join a queue only once
  it is active, so a compute node group created with the login group would
  still be in no queue when the login node writes its Prometheus
  configuration, and would be left out of monitoring.
- **The node group's name**, such as `login`: a naming convention, not a
  contract.
- **Tagging itself** with `ec2:CreateTags`: a write permission on EC2 for
  the login node's role.
- **Node group tags**: PCS doesn't document that they reach the instances.

### Changed

- **PCS detection no longer looks for `/etc/aws-pcs`**, a path that PCS
  doesn't create. A node is a PCS node when it has `PCS_CLUSTER_ID` from a
  lifecycle action, an identity saved on this instance, or the `aws:pcs:*`
  or `pcs-cluster-id` instance tags.
- **The cluster and node group IDs are checked.** They must consist of
  letters, digits, `_` and `-` (every PCS ID does), because they go into
  the Prometheus configuration and env files. The install now stops with a
  message naming the source, for example on an empty `pcs-cluster-id` tag.
- **Prometheus: EC2 service discovery drops the login node's own node
  group.** The `ec2_instances` job of `prometheus-pcs.yml` has a second drop
  rule, on `aws:pcs:compute-node-group-id`, with the ID that the installer
  substitutes, or `none` (which matches no node group) for a login node
  launched outside PCS. Without it, a standard-configuration login node was
  scraped a second time as `Compute`, with duplicate `node_uname_info`
  series (#45). The static `login_node` job still serves the login node as
  `instance_name=HeadNode`, so the dashboards are unchanged. With a launch
  template, the instances of the login node's group already carry
  `monitoring-role=login`, so the same instances are dropped as before.
- **Cluster Costs: the login node's own node group counts as login.**
  `cluster-cost.env` has a new key, `LOGIN_NODE_GROUP_ID`: the login node's
  compute node group on PCS, empty on ParallelCluster and on a login node
  launched outside PCS. The collector counts that group's instances as login
  nodes. Before, a login node without `monitoring-role=login` counted as an
  idle compute node in its group's queue line. The total is unchanged; the
  login, compute and idle figures move. An env file without the key keeps
  v3.0's classification until the installer runs again.
- **The Grafana password refresh** reads the cluster ID from
  `pcs-node.env` first, then from the instance tags as before, so it works
  on nodes without tags.
- **Ubuntu head and login nodes with Docker preinstalled**, such as the
  PCS-ready DLAMI: the `apt-get install` of a missing `jq`, `bc` or
  `python3` waits up to 10 minutes for the dpkg lock
  (`DPkg::Lock::Timeout=600`) instead of failing, as v2.13 did for the NVIDIA
  Container Toolkit. A lifecycle action runs minutes after boot, when
  unattended-upgrades can hold that lock.

### Known limitations

- **One login node group in standard configuration.** Only the monitoring
  login node's own node group is known. Instances of another
  standard-configuration login node group show in Compute Node List, and in
  Cluster Costs as idle compute nodes in their group's queue line; their
  cost is in the total either way. With a launch template, tag them
  `monitoring-role=login` as before.
- **`DCGM_EXPORTER_IMAGE` needs a launch template.** Node lifecycle actions
  can't set environment variables, so the dcgm-exporter override is only
  available through user data.
- **No instance tags in standard configuration.** PCS documents instance
  metadata options as configurable only with a custom launch template, so
  `--role` is the only way to set the role on these node groups, and
  `monitoring-role=none` has no equivalent there: to skip a node group,
  leave the lifecycle action off it.

### Documentation

- README, "Quickstart — AWS PCS": step 2 is now "Install on your node
  groups", with a "Standard configuration (node lifecycle actions)"
  subsection (console fields, the `--node-lifecycle-actions` JSON for login
  and compute node groups, updates, requirements, how the login node is
  identified and the limitation, logs, re-runs, debugging with
  `STOP_SEQUENCE`) and the existing launch-template text under "Launch
  template configuration (user data)". Step 3 says both configurations need
  the same permissions, plus `s3:GetObject` when the script is hosted in S3.
  "Testing from a fork" adds the lifecycle form, and the PCS row of
  Supported platforms names both configurations.
- `docs/cost-estimates.md`: "PCS login node groups" (formerly "PCS login
  node groups without `monitoring-role=login`") gives the three rules and
  the limitation; "How it works" lists `LOGIN_NODE_GROUP_ID`; an upgrade
  note covers env files without it.

### Validation

This release was validated live on 2026-11-27 in us-east-1, on an AWS PCS
cluster of size SMALL with Slurm 26.05.5, accounting off, and PCS agent
1.6.0-1:
- **Standard configuration**, set up from the README's two JSON blocks: a
  login node group (t3.large), a CPU node group (c6i.large) and a GPU node
  group (g4dn.xlarge, Tesla T4, driver 595.112.07) on the PCS-ready DLAMI
  (Ubuntu 24.04, Docker 29.9.3), which is the console's default, and a CPU
  node group on the Amazon Linux 2023 sample AMI, where the action
  installed Docker 25.0.14. The actions took 105 s on the login node, 20 s
  and 35 s on the CPU nodes, and 71 s on the GPU node, where the installer
  updated the NVIDIA Container Toolkit from 1.20.1 to 1.20.2 and started
  dcgm-exporter.
- **Launch templates** with the README's unchanged user data: a CPU node
  group and, later, a login node group tagged `monitoring-role=login`, both
  on the Amazon Linux 2023 sample AMI.

Every check passed:
- **Install.** Every lifecycle action exited 0. Each install log named the
  source of the node's identity (`node lifecycle action` or
  `instance tags`), and each `pcs-node.env` held the node's instance,
  cluster, node group and role.
- **Login node identified.** Prometheus on the login node dropped its own
  node group from EC2 service discovery and served the login node once, as
  `HeadNode`. The four compute nodes were `Compute`, with no duplicate
  `node_uname_info` series. Login Node List and Compute Node List showed
  exactly those rows. The cost collector counted 1 login and 4 compute
  nodes. An independent calculation, which took the login node groups from
  the PCS queues (a node group in no queue), matched all 87 figures within
  $0.000001 per hour. Eight dashboards ran every panel query through
  Grafana without an error.
- **Re-run and reboot.** A re-run of `install.sh` without the lifecycle
  action's variables used the saved identity. It left `prometheus.yml`,
  `cluster-cost.env`, `pcs-node.env` and the containers unchanged. The
  Grafana password refresh worked, and so did an admin login with the
  password from SSM. After `scontrol reboot`, the CPU node kept its
  instance, the action didn't run again, and Prometheus scraped the node
  again after 43 s.
- **Launch templates.** Both launch-template nodes found their identity in
  the instance tags, as before. With both login nodes up, the
  standard-configuration login node dropped the launch-template login node
  by its tag and counted it as a login node. The launch-template login
  node showed the other one as a compute node in its node group's queue
  line, which is the known limitation above. After the
  standard-configuration login node group was deleted, the launch-template
  login node passed the same checks on its own.
- **v3.0**, run on the same standard-configuration node, stopped with
  `Cannot detect platform`.
- **No instance tags in IMDS.** On every standard-configuration node,
  `meta-data/tags/instance` returned 404. The launch templates that PCS
  creates for these node groups don't enable instance tags in metadata, and
  the node group's own tags don't reach the instances. The
  `aws:pcs:cluster-id` and `aws:pcs:compute-node-group-id` EC2 tags are
  present, and EC2 service discovery and the collector rely on them.
  `/etc/aws-pcs` existed on no node.

Not validated live: Slurm 25.11, arm64, RHEL 10, Rocky Linux 10 and
Ubuntu 26.04 custom AMIs in standard configuration, more than one
standard-configuration login node group, and node groups that scale
(minimum below maximum). The dpkg lock wait didn't run either: the
PCS-ready DLAMI already has `jq`, `bc` and `python3`, so the installer
called no `apt-get`.

## v3.0 — 2026-11-27

Rebuilds the Cluster Costs feature, collector and dashboard, on
ParallelCluster and AWS PCS. The new collector prices every running cluster
instance at its purchase option, counts the EBS volumes actually attached,
adds every AWS PCS fee, splits compute node cost among Slurm users and
accounts, and reports its own health instead of showing failures as zeros.
It also fixes four dashboard defects that v2.12 lists under Known issues.
[docs/cost-estimates.md](docs/cost-estimates.md) describes the model, the
health states and the troubleshooting.

### Breaking changes

- **Metric names and labels change.** `cluster_cost_per_hour` and
  `cluster_cost_accumulated` are removed. Custom dashboards or alerts on
  them must move to the new names, in which S is
  `job=~"ec2_instances|login_node",instance_name="HeadNode"`:

  | Old | New |
  |---|---|
  | `cluster_cost_per_hour{component="total"}` | `cluster:cluster_cost_usd_per_hour:sum{job=""}` |
  | `cluster_cost_per_hour{component="headnode"}` | `sum(cluster_cost_usd_per_hour{S, component="instance", node_role=~"head\|login"})`. On PCS it now covers every login node, not only the monitoring node. |
  | `cluster_cost_per_hour{component="compute"}` | `sum(cluster_cost_usd_per_hour{S, component="instance", node_role="compute"})` |
  | `cluster_cost_per_hour{component="ebs"}` | `sum(cluster_cost_usd_per_hour{S, component="ebs"})` |
  | `cluster_cost_per_hour{component="pcs_controller"}` | `sum(cluster_cost_usd_per_hour{S, component="pcs_controller"})` |
  | `cluster_cost_accumulated` | `sum(cluster_cost_accumulated_usd{S, period="since_start"})` |

  S limits a query to the monitoring node's own figures. The new series come
  from the monitoring node's node_exporter (job `ec2_instances` on
  ParallelCluster, `login_node` on PCS), not from the Pushgateway group
  `job="cost"`, so they carry that target's labels. To group cost by
  instance type, use `ec2_instance_type`. `instance_type` on these series is
  the monitoring node's own type.
- **The cost cron job goes away.** The installer removes the
  `cost-metrics.sh` entry from root's crontab and keeps every other line. If
  only the `MAILTO=""` line that old releases added remains, it removes
  root's crontab. If other entries remain, that `MAILTO=""` line stays, so
  cron mail stays off for them; remove it to get cron mail back.
  `/usr/local/bin/cost-metrics.sh` is deleted. Collection runs from
  `cluster-cost.timer` and `cluster-cost.service`, every 60 seconds.
- **Accumulated cost starts again from zero** at the upgrade. The old
  accumulator and price cache in `/var/lib/prometheus/cost-cache` are
  deleted, and so is the old Pushgateway group `job="cost"`. The collector
  no longer uses the Pushgateway, and Prometheus no longer ingests the old
  group's series even if the deletion fails (next item).
- **Prometheus drops some series pushed to the Pushgateway.** On both
  platforms, the `pushgateway` scrape job drops series named
  `cluster_cost_*`, `cluster:cluster_cost_*` or `node_textfile_*`, so that
  no local user can push cost figures or a fake freshness signal. Other
  pushes are unaffected. Anyone who pushes their own series with these name
  prefixes must rename them. Users who maintain their own `prometheus.yml`
  must add the rule to keep the figures protected.

### Added

- **`cluster-cost-collector`** (`cost/cluster_cost.py`), a Python 3 collector
  with the standard library only, run as root by `cluster-cost.timer` on the
  ParallelCluster head node or the PCS login node. Each run, bounded to
  120 seconds:
  - finds the running cluster instances and their attached EBS volumes;
  - prices them from the Pricing API (On-Demand, EBS, AWS PCS) and the Spot
    price history, with the prices cached in
    `/var/lib/aws-parallelcluster-monitoring/cost/prices.json`;
  - reads Slurm's nodes and running jobs as JSON and splits each compute
    node's cost among its jobs by their largest share of CPU, GPU or
    (when Slurm schedules it) memory;
  - accumulates the totals since start and for the UTC month in
    `state.json`, with a backup copy;
  - writes `cluster_cost.prom` to node_exporter's textfile directory, and a
    run report, `last-run.json`.
- **A new Cluster Costs dashboard** (same uid and URL): a notice on what the
  figures are; collector health (status, last update, unpriced resources,
  unaccounted time, unpriced resource-hours, and the stages with their
  error codes); totals (cost per hour, since start, this month, projected);
  breakdowns by component, queue (PCS: node group), instance type and
  purchase option, now and this month; idle and unallocated cost; per-user
  and per-account estimates; and the rates over time.
- **Recording rules** in `prometheus/rules/cost.yml`: the freshness signal
  `cluster_cost_collector:fresh`, the total rate
  `cluster:cluster_cost_usd_per_hour:sum` and the projection
  `cluster:cluster_cost_projected_month_usd:avg_24h`.
- **Tests**: unit tests in `cost/tests` (run with
  `python3 -m unittest discover -s cost/tests -t cost` on Python 3.7 or
  later) and promtool tests for the rules and the dashboard queries in
  `cost/tests/promtool`.

### Changed

- **The numbers change, mostly upwards.** Compute is priced for every
  instance type. On PCS, compute no longer depends on `Name=Compute`. Spot
  nodes use the Spot price. EBS uses the real attached volumes, including
  gp3 IOPS and throughput above the baseline. PCS adds the node management
  and accounting fees. The PCS controller uses the list price for the
  cluster's actual size: $0.5825 per hour for Small in us-east-1, where the
  old script used a fixed $0.59.
- **Failures are shown, not hidden.** When an AWS or Slurm call fails, the
  dashboard reads "Degraded" and names the failing stage and an error code,
  in every run until the call succeeds again. The affected figures are
  missing or come from a cached price. The old script reported zeros.
- **New IAM permissions.**
  - `ec2:DescribeSpotPriceHistory` is new on both platforms.
  - `ec2:DescribeVolumes`, and `pcs:ListComputeNodeGroups` on PCS, come
    back. v2.12 removed them as unused, so users who applied v2.12's policy
    or list need them again. `pricing:GetProducts`, and `pcs:GetCluster` on
    PCS, stay required.
  - Users of the managed-policy alternative need `AmazonEC2ReadOnlyAccess`
    added, because none of the listed policies, and not the head-node role
    ParallelCluster creates, grants `ec2:DescribeSpotPriceHistory`. The
    README's quick start, the commented quick start in
    `parallelcluster-setup/pcluster.yaml` and `iam/README.md` all list it,
    so there are five managed policies instead of four.
  - Without these permissions, Spot nodes or other parts are unpriced and
    the dashboard reads "Degraded", naming the missing parts, instead of
    showing zeros.
- **python3 3.7 or later is required on head and login nodes.** The
  installer installs it from the OS repositories if it's missing. If it
  can't, the node's bootstrap fails before the monitoring stack is
  configured. `bc` stays installed, although nothing uses it any more.
- **The installer restarts Prometheus on head and login nodes, on every
  run.** This loads `rules/cost.yml`, which is new on ParallelCluster, and
  the Pushgateway drop rule. The restart leaves a scrape gap of a few
  seconds, or of several minutes on a large Prometheus while it replays its
  write-ahead log. It also applies other `prometheus.yml` changes that
  re-runs make, such as the RES job, which until now waited for a manual
  restart. The README's RES steps drop that manual restart.

  The install now fails when Prometheus doesn't stay up after that restart,
  for example because `prometheus.yml` is invalid (after a second RES re-run
  on ParallelCluster) or another process holds port 9090. That shows within
  a few seconds, and the log then holds the port's listeners and the end of
  `docker logs prometheus`. When Prometheus runs but isn't ready 300 s after
  the restart, for example while it replays a very large write-ahead log,
  the install only warns, and Prometheus loads the cost rules when it is
  ready. It also only warns when Prometheus is up but hasn't loaded the cost
  rules. This is the rule of v2.13's `wait_for_prometheus`. The restart is
  the installer's last step on these nodes, so everything else is installed
  by then.
- **The Cluster Costs dashboard keeps its uid and URL**, with an entirely
  new layout. As with every release, local edits to the provisioned
  dashboard are overwritten at install. The landing page's Cluster Costs
  card describes the new dashboard.
- **On PCS, the queue breakdown shows compute node group names**, because a
  PCS queue can span several node groups.
- **Per-user and per-account cost estimates are new data, and wide audiences
  can see them.** They are visible to:
  - every Grafana user who can open the dashboard, including Cognito
    self-signups;
  - every local user on the monitoring node, through Prometheus on
    loopback;
  - anyone who can open an SSM port-forwarding session to the node, through
    nginx's `/prometheus/`. v2.12's loopback-only lock-down gives network
    clients 403 there;
  - every host that can reach the monitoring node's port 9100, where
    node_exporter listens on all interfaces without authentication. On
    ParallelCluster the head node's security group admits all traffic from
    the compute nodes, so any Slurm job can read them. On PCS the login
    node's security groups decide. The same port already serves
    `slurm_jobs.prom`, with usernames and job names.

  Grafana OSS can restrict who opens the dashboard, but not who queries the
  data source. Only root and the cluster's default user
  (`cfn_cluster_user` on ParallelCluster; `ec2-user`, `ubuntu` or `rocky` on
  PCS) on the monitoring node can change the figures, as long as only
  node_exporter answers on the node's port 9100. The installer has made
  that user root-equivalent since earlier releases, through the `docker`
  group and its ownership of the monitoring files.
- **New files on monitoring nodes:**
  - `/etc/aws-parallelcluster-monitoring/cluster-cost.env`;
  - `/var/lib/aws-parallelcluster-monitoring/cost/` (state, price cache,
    last-run report);
  - `/var/lib/prometheus/node-exporter/cluster_cost.prom`;
  - `/usr/local/bin/cluster-cost-collector`.

  Every install sets `/var/lib/prometheus/node-exporter` to root:root 0755.
  The collector stops writing (exit 2, "Not reporting") when that directory
  belongs to another user or its group or others can write to it. Textfile
  writers that don't run as root must write elsewhere or run as root.
- **AWS China regions are rejected.** The collector exits with "AWS China
  regions are not supported: their price lists are in CNY", and the
  dashboard reads "Not reporting". The old script reported zeros there.
  GovCloud and the European Sovereign Cloud are not validated.
- **The collector uses only the instance profile, and needs a drop-in to use
  a proxy.** The old cron script ran with root's cron environment, so it
  could pick up root's `~/.aws` profile and keys and, on OSes whose cron
  reads `/etc/environment`, a proxy set there. The new collector ignores
  root's `~/.aws` files (it sets `AWS_CONFIG_FILE` and
  `AWS_SHARED_CREDENTIALS_FILE` to `/nonexistent`), and also ignores
  credential variables such as `AWS_ACCESS_KEY_ID` and `AWS_PROFILE`
  exported in a root shell, `--dry-run` included. A systemd service doesn't
  read `/etc/environment`. A cluster that reached AWS only through a proxy
  configured that way reads Degraded with `EndpointUnreachable` or
  `Timeout` until it adds
  `/etc/systemd/system/cluster-cost.service.d/proxy.conf`
  ([docs/cost-estimates.md](docs/cost-estimates.md#endpointunreachable-or-timeout)).

### Fixed

- **Cluster Costs under-reported** (the repo review's P1-4). Compute read $0
  for instance types that started after the daily price cache was built,
  PCS compute read $0 unless the operator set `Name=Compute`, Spot nodes
  were priced at On-Demand, EBS was a fixed 35 GB of gp3 per instance, two
  of the three PCS fees were missing, and failures pushed zeros. The cron
  job also ran with cron's default `PATH`, `/usr/bin:/bin`. Now every
  instance type is priced in the run where it first appears, at its
  purchase option, the attached volumes and all PCS fees are counted, and a
  failure shows as Degraded. The unit's `PATH` covers `/usr/local/bin` and
  `/usr/bin`, where the supported AMIs install the AWS CLI.
- **The cost collector no longer needs a root cron job** (P1-6, for this
  feature). v2.12 moved the cron entry's edit from a fixed `/tmp` file into
  a pipe, but kept the job in root's crontab. The installer now only removes
  the old entry, without temporary files and with tracing suspended, so no
  line of root's crontab reaches the install log, and none of the new
  install steps uses a path under `/tmp`.
- **ParallelCluster: HeadNode Details could open on a compute node.** Its
  hidden instance selector now lists only the head node, so the dashboard
  opens on the head node. If you override the head node's `Name` tag, the
  panels read "No data" instead of another node's data.
- **ParallelCluster: Compute Node Details could open on a node's previous
  instance.** The hidden `node_instance_id` variable now lists the node's
  instances newest first. It reads only the node's own `node_uname_info`
  series, one step every 15 s of the range. The links from Compute Node
  List and Login Node List add `var-node_instance_id` to the URL, so a click
  opens exactly the instance of that row. After ParallelCluster replaces a
  static node, the dashboard opens on the replacement.
- **Cluster Summary: Idle node-hours and Idle CPU-hours (last 24h)
  overstated idle capacity**, on both platforms. The steps without an idle
  node, or without data, now count as 0. The values drop, often by far, to
  the idle time actually recorded: v2.12's validation measured 24.0
  node-hours where the right value was 0.25. On a head or login node with
  less than 24 hours of data, the stats cover only the recorded time, and
  their descriptions say so. Alerts or reports built on the old values need
  new thresholds.
- **Storage: the EFS Storage Bytes graph had no Infrequent Access line.**
  Series C now asks for `storage_class="IA"`, the name CloudWatch uses, and
  the IA line appears.

As with every release, local edits to these provisioned dashboards are
overwritten at install.

### Documentation

- New [docs/cost-estimates.md](docs/cost-estimates.md): what the dashboard
  shows, sources and formulas with a worked example, the per-user model,
  idle and unallocated cost, what's left out, accumulation, health states,
  stages and error codes, troubleshooting (including the proxy drop-in),
  IAM, the metric reference with the old-to-new mapping, who can see and
  change the figures, PCS login node groups, supported partitions, upgrade
  notes and validation.
- README: the Features bullet, the Dashboards row and the Cluster Costs
  section describe the new dashboard; the managed-policy quick start adds
  `AmazonEC2ReadOnlyAccess`; the PCS login-node permissions add
  `ec2:DescribeVolumes`, `ec2:DescribeSpotPriceHistory` and
  `pcs:ListComputeNodeGroups`; the RES steps drop the manual Prometheus
  restart; the custom-AMI requirements for RHEL 10, Rocky Linux 10 and
  Ubuntu 26.04 name the cost collector and `python3` 3.7; the architecture
  diagram shows the `cluster-cost` timer.
- `iam/README.md`: the policy replaces five managed policies, and the EC2
  statement grants `ec2:DescribeVolumes` and `ec2:DescribeSpotPriceHistory`
  too.
- `parallelcluster-setup/pcluster.yaml` pins this release and lists
  `AmazonEC2ReadOnlyAccess` in its commented quick start.

### Validation

This release was validated live on 2026-11-26 in us-east-1, on:

- ParallelCluster 3.16.3 with Slurm 25.11.8: Amazon Linux 2023 with
  On-Demand c6i.large, Spot c6i.large and g4dn.xlarge GPU queues, upgraded
  in place from v2.13; and Ubuntu 24.04, installed fresh;
- AWS PCS, size SMALL with accounting `STANDARD`: Slurm 25.11.8, upgraded in
  place to 26.05.5, with a login node group (Amazon Linux 2023 sample AMI),
  On-Demand, Spot and GPU node groups (the GPU node on the PCS-ready
  DLAMI), and a standalone login node.

Every check passed on both platforms:

- **In-place upgrade from v2.13.** The cost cron entry, the old script,
  `cost-cache` and the Pushgateway `job="cost"` group were removed, other
  root crontab lines were kept, and `cluster-cost.timer` ran every 60–61 s
  with every stage OK.
- **Figures.** An independent calculation from the same AWS responses and
  Slurm allocations matched every instance price, cost line, breakdown, PCS
  fee, user, account, idle and unallocated figure within $0.000001 per
  hour: 59 checks on ParallelCluster, and 82 to 85 on PCS for each of
  Slurm 25.11, Slurm 26.05 and the standalone login node. Users, idle and
  unallocated cost added up to the compute cost.
- **Failures.** With `ec2:DescribeInstances`, `ec2:DescribeVolumes`,
  `ec2:DescribeSpotPriceHistory`, `pricing:GetProducts`, `pcs:GetCluster`
  or `pcs:ListComputeNodeGroups` denied, a silent Pricing endpoint or an
  unroutable proxy, the dashboard read Degraded, named the stage and its
  error code, and no cost line read 0. Cached prices stayed in use where
  documented. Runs stayed within their time budget, and every run without
  an injected timeout took under 20 s.
- **Integrity.** Non-root users couldn't change the collector's file, and
  a forged Pushgateway push and a fake exporter on a compute node didn't
  change the figures. After the collector had been stopped for more than 5
  minutes, the dashboard read "Not reporting".
- **Accumulation.** The since-start and this-month totals kept growing
  across a 5-minute stop, a reboot, an installer re-run, a restart of
  node-exporter and Prometheus, and the PCS Slurm 26.05 upgrade. Replacing
  the PCS login node restarted tracking, as documented.
- **Dashboards.** On Grafana 13.3.2 every Cluster Costs query returned
  data, and headless Chrome rendered Cluster Costs, HeadNode Details,
  Compute Node Details, Cluster Summary and Storage with no error badge.
  The landing-page card opened Cluster Costs. The four fixes (HeadNode
  Details, the Compute Node Details links, the 24-hour idle stats and the
  EFS Infrequent Access line) showed the right data.
- **IAM.** The IAM policy simulator allowed every collector call for the
  README quick start, the commented quick start in
  `parallelcluster-setup/pcluster.yaml` and the least-privilege policy, and
  a head node with only the least-privilege policy and
  `AmazonSSMManagedInstanceCore` reported every stage OK.
- **AWS CLI error formats.** The error samples of AWS CLI 2.40.3 (the
  ParallelCluster head node) and 2.41.0 (the PCS login node) map to the
  expected error codes. They are unit-test fixtures now, with the Slurm JSON
  captured on both platforms.

RHEL 10, Rocky Linux 10 and Ubuntu 26.04 are covered by unit tests only: the
smoke test on a v2.13 operating system didn't run, because its AMI no longer
exists. Multi-node GPU jobs are covered by unit tests only.

## v2.13 — 2026-11-26

Adds AWS PCS support for RHEL 10, Rocky Linux 10 and Ubuntu 26.04. On the
operating systems that were already supported, the installer runs the same
steps as before, apart from the behavior changes below.

Behavior changes:
- On head and login nodes, the installer stops and disables `cockpit.socket`
  when it listens on port 9090, which Prometheus needs. Rocky Linux EC2
  images enable it.
- A port conflict on 9090 now fails the install instead of passing
  silently (see Changed).
- On Ubuntu GPU nodes (22.04, 24.04 and 26.04), the NVIDIA Container
  Toolkit's `apt-get install` waits up to 10 minutes for the dpkg lock
  instead of failing (see Fixed).

### Added

- **RHEL 10, Rocky Linux 10 and Ubuntu 26.04 on AWS PCS**, for login and
  compute nodes, CPU and GPU. AWS PCS added these operating systems on
  2026-11-11, and the installer stopped on them with `Unsupported OS`.
  ParallelCluster 3.16 doesn't offer them, so this applies to AWS PCS only.
  - `installer/os/rhel10.sh` and `installer/os/ubuntu26.sh` install Docker CE
    and the Docker Compose plugin from Docker's repository, as the RHEL 9 and
    Ubuntu 24.04 scripts do. The installer also sends AlmaLinux 10 and CentOS
    Stream 10 to `rhel10.sh`, as it does for 9.x; those two are not validated.
  - Unlike `rhel9.sh`, `rhel10.sh` doesn't turn off signature checking for
    Docker's repository when the key import fails. That fallback works around
    a GPGME failure on some RHEL 9 AMIs (#41). On EL10, `rpm` verifies
    signatures with rpm-sequoia, so a failed import now stops the install.
  - GPU nodes install the NVIDIA Container Toolkit from NVIDIA's repository,
    with the same commands as on the other operating systems.
  - On EL10, the netfilter modules that Docker's iptables rules need
    (`xt_conntrack`, `br_netfilter`, `nft_compat` and others) ship in
    `kernel-modules-extra`, which `iptables-nft` only recommends. Left to
    dnf, that package came from the newest kernel in the repositories and
    pulled that kernel in, so the running kernel had no modules and
    `dockerd` failed with `RULE_APPEND failed (No such file or directory):
    rule in chain PREROUTING` until a reboot. The live validation hit this on
    Rocky Linux 10.3 (running kernel 6.12.0-264.20.1, newest 6.12.0-264.21.1).
    `rhel10.sh` now installs `kernel-modules-extra` for the running kernel
    when the modules are missing.
  - The `apt-get install` calls in `ubuntu26.sh` wait up to 10 minutes for
    the dpkg lock (`DPkg::Lock::Timeout=600`). On a node booted from an AMI
    that is more than a day old, `apt-daily-upgrade.timer` starts
    unattended-upgrades, which can hold that lock, in the first minutes
    after boot.
  - The Rocky Linux EC2 images enable `cockpit.socket` on port 9090, where
    Prometheus listens, so Prometheus restarted in a loop on Rocky login
    nodes. Before it starts the containers on a head or login node, the
    installer now stops and disables `cockpit.socket` when it listens on
    9090; it reads the socket unit, because `ss` shows systemd as the owner.
    Cockpit on another port is left alone. If you use Cockpit on a login
    node, move it to another port before you install.
  - AWS PCS provides no AMI for these operating systems. The README lists what
    a custom AMI must include: the AWS CLI on login nodes, and the NVIDIA
    driver and a way to detect the GPU on GPU nodes.

### Changed

- **Head and login nodes: the install fails when Prometheus can't serve.**
  `docker compose up` succeeds even when Prometheus can't bind
  127.0.0.1:9090 and restarts in a loop, and the install used to report
  success with every dashboard empty. After it starts the containers, the
  installer now waits up to 60 s for Prometheus to answer `/-/ready`. The
  install fails when the Prometheus container isn't running or restarts
  during the wait, for example because another process holds port 9090, and
  prints the port 9090 listeners and the Prometheus log. When Prometheus runs
  but isn't ready yet, for example while it replays a large write-ahead log
  on a re-run, the installer only warns.

### Fixed

- **PCS nodes on Rocky Linux AMIs failed before the installer started.**
  Rocky Linux AMIs have a `rocky` user and no `ec2-user`, so `post-install.sh`
  stopped at `chown: invalid user: 'ec2-user:ec2-user'`. `post-install.sh` and
  `installer/platform/pcs.sh` now use `rocky` when `ec2-user` doesn't exist.
  Nodes that have `ec2-user`, or `ubuntu` with `/home/ubuntu`, keep the user
  they had.
- **Ubuntu GPU nodes: the NVIDIA Container Toolkit install could fail when
  another apt process held the dpkg lock.** `apt-get install` stopped at
  once with `Could not get lock /var/lib/dpkg/lock-frontend`. In the live
  validation, unattended-upgrades held the lock on an Ubuntu 26.04 GPU node a
  few minutes after boot; the same step runs on Ubuntu 22.04 and 24.04 GPU
  nodes. The toolkit's `apt-get install` now waits up to 10 minutes for the
  dpkg lock (`DPkg::Lock::Timeout=600`).

This release was validated live on 2026-11-26 on AWS PCS with Slurm 26.05
(x86_64), one cluster per operating system, on custom AMIs of RHEL 10.3,
Rocky Linux 10.3 and Ubuntu 26.04.1 with the PCS agent 1.6.0-1. Each
cluster had a login node and a CPU compute node (m6i.large), and a GPU
compute node (g4dn.xlarge, NVIDIA T4, driver 620.70.10). At boot every node
installed Docker CE 29.10.1 and the Docker Compose plugin 5.8.0, and the GPU
nodes the NVIDIA Container Toolkit 1.20.2. arm64, Slurm 25.11 on these
operating systems, AlmaLinux 10 and CentOS Stream 10 were not validated.

## v2.12 — 2026-11-26

Security fixes, the latest stable component versions, fixes for the AWS PCS
compatibility rules, the Slurm dashboards' node, CPU and job panels, the
Storage stats and the Cluster Logs queries, and corrected IAM documents.
It fixes the problems listed under Known issues in v2.11; only the memory
columns of the Slurm user and account panels can stay empty (see Fixed).
Known issues lists what is still open.

Behavior changes:
- The monitoring services on the head/login node listen on 127.0.0.1 only,
  and nginx serves `/prometheus/` and `/pushgateway/` only to local clients,
  which includes SSM port forwarding. Other clients get HTTP 403 on those
  two paths, and pushes to the Pushgateway from other hosts stop working.
  Grafana and the landing page are unchanged.
- dcgm-exporter 4.9.0 exports the GPU metrics under their DCGM 4.7 names
  and units, with a `hostname` label instead of `Hostname`. The dashboards
  are updated; your own GPU queries and alerts need the new names. A
  `DCGM_EXPORTER_IMAGE` override must be dcgm-exporter 4.9.0 or later.
- On ParallelCluster Ubuntu 24.04 head nodes, the installer stops and
  disables `apache2` when it holds port 80 or 443.
- The head/login node stack now creates a Docker bridge network, which
  takes an address range from Docker's default pools, usually
  172.18.0.0/16 (see cloudwatch-exporter under Changed).

### Security
- **Every local user could read the instance role's credentials.**
  `refresh-ec2-credentials.sh` writes them to `/run/prometheus-ec2-creds/`
  for the containers that call AWS. The directory was 0755 and the file
  0644, both `root:root`, so any user on the head or login node could read
  credentials that, on ParallelCluster, carry everything ParallelCluster
  attaches to the head node plus your `AdditionalIamPolicies`. That also
  defeated `Imds.Secured`, which limits IMDS to root. The directory is now
  0750 and the file 0640, both `root:monitoring-creds`, a system group
  that the installer creates. Only the prometheus, grafana and
  cloudwatch-exporter containers get the group, and cluster users get
  "Permission denied". To fix a running cluster, re-run the installer on
  the head/login node (see Upgrade notes): it creates the group, rewrites
  the file and recreates the containers with the group.
- **`/prometheus/` and `/pushgateway/` answered any client that could reach
  nginx, and the monitoring services listened on every interface.**
  Neither Prometheus nor the Pushgateway has authentication, and the
  Pushgateway accepts writes and deletes. Grafana (3000), Prometheus (9090),
  the Pushgateway (9091), the Slurm exporter (9092) and cloudwatch-exporter
  (9106) now listen on 127.0.0.1 only. nginx serves `/prometheus/` and
  `/pushgateway/` only to the node itself
  (`allow 127.0.0.1; allow ::1; deny all;`), which includes SSM port
  forwarding, because the SSM agent connects from 127.0.0.1. HTTP basic
  auth was rejected: it would make the Grafana admin password a second
  credential to distribute and rotate, add browser credential prompts, and
  exclude Cognito users. What changes:
  1. Clients that reach nginx over the network get HTTP 403 on
     `/prometheus/` and `/pushgateway/`. That covers README Option A (public
     IP), the ALB setup in `docs/public-access.md`, and other hosts in the
     VPC, compute nodes included. Use SSM port forwarding (README Option B),
     or Grafana's Explore view for PromQL queries.
  2. Pushes to the Pushgateway from other hosts stop working. The stack's
     own pushes go to 127.0.0.1:9091 and are unaffected.
  3. Grafana and the landing page don't change. The landing page's
     Prometheus card says that it needs SSM port forwarding.

  Ports 3000, 9090, 9091, 9092 and 9106 no longer answer on the network.
  nginx (80/443) and node_exporter (9100, which Prometheus scrapes at the
  node's private IP) still do.
- **Root used predictable `/tmp` paths.** `post-install.sh` and the
  custom-AMI component downloaded the tree to fixed `/tmp` names, the
  installer copied root's crontab through `/tmp/crontab.tmp`, and the README
  user-data snippets ran `/tmp/post-install.sh`. A local user who created
  such a path first could change it between root's write and read, and so
  add lines to root's crontab or have root run a modified tarball or
  script. Downloads now go to a `mktemp -d` directory that is removed on
  exit, the crontab is edited in a pipe, and the documented snippets use
  `mktemp`.
- **The Slurm exporter download wasn't verified.** The installer now checks
  the rivosinc `prometheus-slurm-exporter` 1.8.0 tarball with
  `sha256sum -c` against the SHA-256 that the release publishes, pinned in
  `installer/common.sh`, before it extracts it. On a mismatch the install
  fails.
- **The README put the monitoring policy on compute nodes too.** Its
  ParallelCluster Quickstart showed the policy in a snippet for both
  `HeadNode` and each `SlurmQueue`. Compute nodes make no AWS calls for
  monitoring, and on a compute node the policy lets any job read and change
  the Grafana admin password. If you followed that snippet, remove the
  policy from your queues' `AdditionalIamPolicies` and keep it on the
  `HeadNode`. If untrusted users run jobs on the cluster, also rotate the
  Grafana admin password, as in step 1 of the v2.11 notes below.

### Changed
- **Component versions**, the latest stable releases on 2026-11-25:

  | Component | v2.11 | v2.12 |
  |---|---|---|
  | Grafana | 13.0.2 | 13.3.2 |
  | Prometheus | v3.12.0 | v3.16.0 |
  | Pushgateway | v1.11.3 | v1.11.4 |
  | Node Exporter | v1.11.1 | v1.13.1 |
  | NGINX | `1.27-alpine` | `1.30-alpine` (stable branch, 1.30.8 at release) |
  | NVIDIA DCGM Exporter | `4.2.0-4.1.0-ubuntu22.04` | 4.9.0 (`4.7.0-4.9.0`, DCGM 4.7.0, pinned by digest) |
  | CloudWatch Exporter | v0.16.0 | v0.19.0 |
  | Docker Compose plugin (Amazon Linux 2/2023) | 5.1.4 | 5.8.0 |
  | prometheus-slurm-exporter | 1.8.0 | 1.8.0, now SHA-256 verified |

  On Ubuntu and RHEL, the Compose plugin still comes from the docker-ce
  repository.
- **dcgm-exporter 4.9.0 uses the DCGM 4.7 field names and units.**
  `dcgm/counters.csv` and the GPU dashboards use the new names. For
  example, `DCGM_FI_DEV_GPU_UTIL` (percent) is now
  `DCGM_FI_DEV_GPU_UTIL_RATIO` (0–1), `DCGM_FI_DEV_SM_CLOCK` (MHz) is
  `DCGM_FI_DEV_SM_CLOCK_HERTZ` (Hz), `DCGM_FI_DEV_FB_USED` (MiB) is
  `DCGM_FI_DEV_FB_USED_BYTES` (bytes), and `DCGM_FI_DEV_THERMAL_VIOLATION`
  (µs) is `DCGM_FI_DEV_GPU_THERMAL_THROTTLE_SECONDS_TOTAL` (s). Every series
  has a `hostname` label instead of `Hostname`. The dashboard panels show
  the same numbers and units as before. If you have your own queries,
  recording rules or alerts on the old names or on `Hostname`, update them:
  `dcgm/counters.csv` lists the 48 fields with their new names and units,
  and `git diff v2.11 v2.12 -- dcgm/counters.csv` shows each old name next
  to its replacement.
- **A `DCGM_EXPORTER_IMAGE` override must be dcgm-exporter 4.9.0 or later**,
  because older images don't know the DCGM 4.7 field names. The default
  image now covers B300 (p6-b300), so most clusters need no override.
- **cloudwatch-exporter runs as a non-root user, on a bridge network.**
  v0.19.0 runs as uid 65534 and reads the credentials file through the
  `monitoring-creds` group. It takes a listen port but no address, so it
  left the host network and is published on 127.0.0.1:9106 only. Docker
  Compose creates the bridge network for it, `monitoring-head_default`,
  the first Docker network of the head/login node stack. Docker gives it a
  subnet from its default address pools, usually 172.18.0.0/16, and the
  node then routes that subnet to the bridge, so it can no longer reach
  hosts in that range elsewhere, such as in a peered VPC, a secondary VPC
  CIDR or an on-premises network. Run
  `docker network inspect monitoring-head_default` to see the subnet. If
  you use that range, move the network to an unused one with
  `default-address-pools` in `/etc/docker/daemon.json`, as the README
  Security section shows.
- **Grafana 13.3.2.** The dashboards and datasources provision as before.
  Grafana 13.3 runs the CloudWatch datasource as a separate plugin process,
  which gets Grafana's environment only when the plugin is listed in
  `GF_PLUGINS_FORWARD_HOST_ENV_VARS`. `compose/head.yml` sets it to
  `cloudwatch`, so that the plugin reads the credentials file named by
  `AWS_SHARED_CREDENTIALS_FILE`; without it, the CloudWatch datasource and
  the Cluster Logs dashboard fail on ParallelCluster head nodes with
  `Imds.Secured`. That environment also holds the Grafana admin password
  and, with Cognito, the OAuth client secret, because the image's
  entrypoint turns each `GF_*__FILE` setting into a variable that holds
  the file's content. The plugin runs as Grafana's user in the Grafana
  container, which can read both files anyway. `compose/head.yml` also
  sets `GF_PLUGINS_PREINSTALL_AUTO_UPDATE=false`, so Grafana runs the
  plugins that the image bundles, among them CloudWatch 12.11.3 and
  Prometheus 13.2.7; by default Grafana 13.3 updates them from grafana.com
  when it starts. It still downloads the default app plugins that its data
  volume doesn't have yet, such as the Drilldown apps, from grafana.com.
  The image no longer contains `bash`, and `grafana-cli` is not on its
  `PATH`; use `sh` and `grafana cli` in your own `docker exec` commands.
- **The Slurm exporter collects Slurm's diagnostics** (ParallelCluster).
  `slurm_exporter.service` passes `-slurm.collect-diags`, so the exporter
  also reports the backfill, RPC, controller thread and DBD queue series
  that the Slurm Detail dashboard uses.

### Fixed
- **ParallelCluster, Ubuntu 24.04 head node: the landing page and Grafana
  were unreachable.** The official ParallelCluster Ubuntu 24.04 AMIs run
  `apache2` on port 80, so the monitoring nginx couldn't bind, while the
  install still reported success. Before it starts the containers, the
  installer now stops and disables `apache2` when it holds port 80 or 443,
  as `ss` shows; `apache2` on other ports is left alone. If you use
  `apache2` on the head node, move it off ports 80 and 443 before you
  install. After it starts the containers, the installer waits up to 60 s
  for nginx to serve `https://127.0.0.1/`. If nginx doesn't, the install
  fails and prints the port 80/443 listeners and the nginx log. Closes the
  v2.11 known issue "ParallelCluster, Ubuntu 24.04 head node: the landing
  page and Grafana are unreachable".
- **AWS PCS: a node's install could fail with "Cannot detect platform".**
  Under `set -o pipefail`, a `producer | grep -q` pipeline fails when
  `grep` exits at the first match and `SIGPIPE` kills the producer. The
  platform check, the GPU check and the Grafana password refresh now match
  against here-strings. Closes the v2.11 known issue "AWS PCS: a node's
  install can fail with 'Cannot detect platform'".
- **AWS PCS: the job-to-node mapping was empty.** `slurm-job-nodes.sh` now
  finds `squeue` on PCS: after the usual paths, it asks a root login shell,
  which has the `PATH` that PCS sets, and then uses
  `/opt/aws/pcs/scheduler/slurm-<version>/bin` if there is exactly one. A
  node without Slurm exits quietly. Each `slurm_job_node` series also gets
  the job's node address, `node_addr`, which is the instance's private IP,
  and the Compute Node List joins jobs to instances on that address: on PCS
  the Slurm node name, such as `cpu-1`, is not the host name. The join keeps
  only the job's labels, so the **Job ID**, **User** and **Job Name**
  columns fill in on the node's row on both platforms. On ParallelCluster
  they used to show on rows of their own, because the job series carried
  the head node's labels. Closes the v2.11 known issue "AWS PCS: the
  job-to-node mapping is empty".
- **AWS PCS: panels that depend on the compatibility rules were empty.**
  `prometheus/rules/pcs-compat.yml` read three names that Slurm doesn't
  emit (`slurm_partition_cpus_alloc`, `slurm_partition_cpus_idle` and
  `slurm_bf_last_depth_cycle`) and wrote lowercase job states. It now maps
  the names that Slurm 25.11 and 26.05 emit, adds per-state partition node
  counts, per-user and per-account CPU allocation, the controller thread
  count and the DBD queue, and writes the job states the ParallelCluster
  exporter reports (`RUNNING`, `PENDING`). On PCS, Cluster Summary **Top
  users** and **Top partitions**, and the Slurm Detail **Partitions** table
  (except CPU load), **Top Users**, **Top Accounts**, **Controller
  Threads**, **DBD Agent Queue** and **Last Backfill Depth** now show data.
  The memory columns stay empty on PCS, because the unit of Slurm's
  per-user memory series is unclear.
- **Dashboard panels that stayed empty.** Closes the v2.11 known issue
  "Some dashboard panels stay empty, because their queries don't match the
  metrics the stack collects":
  - **Load / Core** in the Compute Node List and the RES Node List, on
    every platform: the query now matches the per-node CPU count on
    `instance_id`.
  - ParallelCluster: Cluster Summary **Top users (CPUs allocated)**, and
    Slurm Detail **Top Users**, **Top Accounts** and the usage series of
    **Account Quota Utilization**: the panels filter on the job states the
    exporter reports (`RUNNING`, `PENDING`). Their memory columns stay
    empty when the exporter reports no per-user or per-account memory, as
    on the v2.11 validation clusters.
  - ParallelCluster: Slurm Detail **Backfill Cycles per Minute**: the
    exporter now runs with `-slurm.collect-diags` (see Changed). The panel
    and **Backfill Cycles (since boot)** read the cycle count,
    `slurm_backfill_cycle_counter`, not the exporter's
    `slurm_backfill_cycle_count`, which is the summed duration of the
    cycles in microseconds (sdiag `bf_cycle_sum`). On PCS, the
    compatibility rules record Slurm's cycle count under the same name,
    `slurm_backfill_cycle_counter`, instead of
    `slurm_backfill_cycle_count`.
- **Cluster Summary: Nodes Allocated and Nodes Down/Drain always showed
  0.** Each added two series whose `state` labels differ, and such series
  never match. They now count the nodes in the `alloc` and `mix` states,
  and in the `down`, `drain` and `drng` (draining) states. The node-state
  queries also match the flag that `sinfo` appends to a state, such as `*`
  (not responding), `~` (powered down) or `#` (powering up), so a drained
  node that stops responding (`drain*`) counts under Down/Drain, and a
  powered-down dynamic node (`idle~`) under Idle, as `sinfo` counts them.
  The same goes for **Nodes Idle** and **Node States**. **Idle node-hours
  (last 24h)** matches the flags too, except `~`: a powered-down node has
  no instance, so it isn't idle capacity, and v2.11 left it out as well.
  **Top partitions (CPUs allocated)** no longer filters on the node state,
  so it counts every allocated CPU.
- **ParallelCluster: the Slurm Detail Partitions table showed 0 Nodes
  Busy** unless the partition also had idle nodes and down or drained
  nodes. The column subtracted those counts from the partition's total,
  but a PromQL subtraction keeps only the partitions found on both sides,
  and the Slurm exporter reports only the states that have nodes. Nodes
  Busy now counts the nodes in every state other than idle, down, drain
  and `drng`. Earlier releases have this too. Nodes Down/Drain now also
  counts `drng`, so on ParallelCluster a draining node (`drng`: drain
  flag, jobs still running) counts under Nodes Down/Drain in both Cluster
  Summary and Slurm Detail. On AWS PCS it counts under Nodes Allocated and
  Nodes Busy (see Known issues).
- **ParallelCluster: node names longer than 30 characters merged into one
  node.** The Slurm exporter's default `sinfo` format cuts node names at
  30 characters and partition names at 15, and the exporter keys nodes by
  that name. On the example cluster in
  `parallelcluster-setup/pcluster.yaml`, the nodes
  `queue0-dy-queue0-compute-resource-0-1` to `-4` became one node, so the
  node, CPU and partition panels were wrong. `slurm_exporter.service` now
  passes the same format with both fields 100 characters wide
  (`-slurm.sinfo-cli`). Earlier releases have this too.
- **ParallelCluster: CPUs Allocated and CPUs Idle left out mixed nodes.**
  The exporter files each node's CPUs under the node's state, so panels
  that read the `alloc` and `idle` states missed every partly allocated
  node. **CPUs Allocated**, **CPUs Idle**, **CPU Allocation** and **Idle
  CPU-hours (last 24h)** now sum the allocated and idle CPUs per partition
  (`slurm_partition_alloc_cpus`, `slurm_partition_idle_cpus`), which add
  up to `sinfo -o %C`; Idle CPU-hours leaves out the CPUs of powered-down
  nodes. On PCS, the compatibility rules record the same two series.
  Earlier releases have this too.
- **AWS PCS: Total Nodes, Nodes Idle and CPUs Idle counted the login node,
  and a drained node as idle.** PCS registers the login node as a Slurm
  node without a partition, and Slurm's cluster-wide node counts include
  it; they also count a drained node both in its base state, usually idle,
  and as drained. The compatibility rules now sum Slurm's per-partition
  series and count a drained node as drain only, as `sinfo` does, also in
  the Slurm Detail **Partitions** table; Known issues lists the one case
  they get wrong. Earlier releases have this too.
- **ParallelCluster: every Cluster Logs panel failed** with `invalid input
  region __AWS_REGION__`. The installer substitutes the log group name and
  ARN in the dashboard's queries, and now the region too. Earlier releases
  have this too.
- **The Storage stat panels were empty.** cloudwatch-exporter stamped each
  sample with CloudWatch's time for it, about 11 minutes in the past
  because the exporter asks for data 10 minutes old by default, so
  Prometheus's instant queries, which look back 5 minutes, found nothing.
  `cloudwatch-exporter/config.yml` now sets `set_timestamp: false`, so
  Prometheus stamps the samples when it scrapes them. The values still lag
  CloudWatch by about 10 minutes, and the Storage graphs plot them at
  scrape time. Earlier releases have this too.
- **Single-value panels showed a second 0 next to their value.** A query
  of the form `X or vector(0)` returns X and a separate 0 whenever X has
  data. These panels now sum X first, so they show one value: on Cluster
  Summary, the node, CPU and job counts, **Idle CPU-hours (last 24h)** and
  **Jobs Over Time**; on Slurm Detail, **Failed (since boot)**,
  **Suspended**, **Controller Threads**, **DBD Agent Queue**, **Backfill
  Cycles (since boot)** and **Last Backfill Depth**.

### IAM
- **The least-privilege ParallelCluster policy didn't grant what the stack
  calls.** With only the rendered policy, cloudwatch-exporter couldn't read
  the FSx and EFS metrics of the Storage dashboard, the Cluster Logs
  dashboard couldn't run its Logs Insights queries, and the Grafana
  CloudWatch datasource health check failed (`cloudwatch:ListMetrics`
  denied). `iam/monitoring-head-node-policy.json` now grants
  `cloudwatch:ListMetrics` and `cloudwatch:GetMetricStatistics` in the
  cluster's region, `logs:StartQuery` and `logs:GetLogGroupFields` on the
  cluster's log groups, and `logs:DescribeLogGroups`, `logs:GetQueryResults`
  and `logs:StopQuery` in the cluster's region. It no longer grants what
  nothing calls: the CloudFormation and FSx statements,
  `ec2:DescribeVolumes`, `pricing:DescribeServices` and
  `logs:DescribeLogStreams`. If you created the policy from an earlier
  release, render it again and make the new document the default version:
  ```bash
  ./iam/render-policy.sh <cluster-name> <region> [account-id] [s3-bucket] > my-policy.json
  aws iam create-policy-version \
      --policy-arn arn:aws:iam::<account-id>:policy/pcluster-monitoring-<cluster-name> \
      --policy-document file://my-policy.json --set-as-default
  ```
  `iam/README.md` lists each statement and what calls it.
- **The PCS permission list was incomplete and too broad.** The README now
  lists what the login node calls: `pcs:GetCluster`,
  `ec2:DescribeInstances`, `ssm:GetParameter`, `ssm:PutParameter` and
  `ssm:AddTagsToResource` on `/pcs/<cluster-id>/grafana/*`, `kms:Decrypt`
  through SSM, `pricing:GetProducts`, and `cloudwatch:ListMetrics` and
  `cloudwatch:GetMetricStatistics`. Compute node groups need only
  `pcs:RegisterComputeNodeGroupInstance`, which PCS itself requires.
  `ec2:CreateTags`, `ec2:DescribeVolumes` and `pcs:ListComputeNodeGroups`
  are no longer listed. Check your login node role against the new list.

### Documentation
- README: the ParallelCluster Quickstart puts the monitoring policy on the
  `HeadNode` only. Option A opens port 443 to your own address range
  instead of `0.0.0.0/0`, and says that `/prometheus/` and `/pushgateway/`
  return 403 there. The Architecture diagram marks the localhost-only
  services and adds cloudwatch-exporter, the Security section describes the
  credentials file and the services that listen on the network, and the
  Components table matches the pins.
- `docs/imds-secured-design.md` gives the credentials-file permissions and
  the containers that read it, says how Grafana's CloudWatch plugin gets
  the file's path, and no longer says that the credentials are limited to
  the monitoring policy.
- `docs/public-access.md`: through the ALB, `/prometheus/` and
  `/pushgateway/` return 403; use SSM port forwarding for them.
- `docs/custom-ami.md`: the default dcgm-exporter image covers B300, and is
  about 49 MB to download and about 150 MB on disk; the Compose plugin is
  about 34 MB.
- `iam/README.md` describes the new policy, and keeps it on the `HeadNode`.
- `parallelcluster-setup/pcluster.yaml` pins `v2.12` and no longer needs
  the apache2 workaround on Ubuntu 24.04.

### Upgrade notes
- **New clusters:** pin `v2.12` in both the `Script` URL and `Args`, as in
  `parallelcluster-setup/pcluster.yaml`. `latest` resolves to v2.12 once
  it is released.
- **Running clusters:** re-run the installer from v2.12 on the
  ParallelCluster head node or the PCS login node:
  ```bash
  tmp="$(mktemp)" &&
      curl -fsSL https://raw.githubusercontent.com/aws-samples/aws-parallelcluster-monitoring/v2.12/post-install.sh -o "${tmp}" &&
      sudo bash "${tmp}" v2.12
  ```
  Then replace the compute nodes, so that they run node_exporter v1.13.1
  and dcgm-exporter 4.9.0. Until they do, the GPU panels stay empty for
  them, because the dashboards query the DCGM 4.7 names. If your cluster
  config or user data pins an older ref, change it to `v2.12` first.
  Custom AMIs built for v2.11 hold the v2.11 images; rebuild them
  (`docs/custom-ami.md`) so that nodes don't pull the new images at boot.
- **Least-privilege policy:** render it again and create a new default
  version, as shown under IAM.
- **`DCGM_EXPORTER_IMAGE`:** remove an old override. If you need one, it
  must be dcgm-exporter 4.9.0 or later.

### Known issues
Found while preparing v2.12.
- **AWS PCS: CPUs Idle also counts the free CPUs of drained and down
  nodes**, which `sinfo` reports as other, because Slurm's per-partition
  series count them as idle. Earlier releases count them too. A draining
  node, one with the drain flag and jobs still running, counts under Nodes
  Allocated, and under Nodes Busy in the Slurm Detail Partitions table, as
  in Slurm's own counts, while `sinfo` shows it as draining.
  A node in several partitions counts once in each in the node and CPU
  panels.
- **AWS PCS: a drained node that is also down can count as idle.**
  Slurm's per-partition series don't say whether a drained node is idle
  or down, and the compatibility rules take drained nodes out of the idle
  count first. So in a partition that also has idle nodes, each drained
  down node makes Nodes Idle read one low and Nodes Down/Drain one high,
  in Cluster Summary and in the Slurm Detail Partitions table.
- **AWS PCS: Idle node-hours and Idle CPU-hours (last 24h) count
  powered-down nodes**, which have no instance. Slurm counts a
  powered-down node as idle in its per-partition series, and the
  compatibility rules have no power state that the panels could leave out.
  Earlier releases count them too.
- **ParallelCluster: the job counts read 0.** Earlier releases have this
  too. Cluster Summary **Jobs
  Running**, **Jobs Pending**, **Jobs Completed**, **Jobs Failed** and
  **Jobs Over Time**, and Slurm Detail **Total Pending Now**, **Total
  Running Now**, **Failed (since boot)** and **Suspended** read the
  `slurm_jobs_*` series, which the ParallelCluster Slurm exporter (rivosinc
  `prometheus-slurm-exporter` 1.8.0) doesn't export. On AWS PCS, Slurm
  exports them and the panels show data. The Slurm Detail **Top Users** and
  **Top Accounts** panels show the running and pending jobs per user and
  account on both platforms.

Seen in the v2.12 validation. Earlier releases have these too, and they
will be fixed in a later release:
- **ParallelCluster: HeadNode Details can open on a compute node.** The
  dashboard selects the first instance ID, in sort order, of all the nodes
  that Prometheus scrapes, not the head node, and its instance selector is
  hidden. In the v2.12 validation it opened on the head node on two of four
  clusters. On the other two it opened on a compute node that no longer had
  an instance: 7 and 16 of its 17 panels showed "No data", and the others
  showed that node's last values under the HeadNode title. To see the head
  node, add `&var-instance_id=<head node instance ID>` to the dashboard's
  URL.
- **ParallelCluster: Compute Node Details can open on a node's previous
  instance.** When a node name moves to a new instance, as when
  ParallelCluster replaces a static node, the name maps to both instances
  for some time, and the dashboard selects the first instance ID in sort
  order. The link from the Compute Node List passes only the node name. If
  the old instance comes first, most panels show "No data": 23 of 25 in the
  v2.12 validation, 74 minutes after the replacement. Add
  `&var-node_instance_id=<instance ID>` to the dashboard's URL, with the
  Instance ID that the Compute Node List shows.
- **Idle node-hours and Idle CPU-hours (last 24h) overstate idle
  capacity.** Both panels average the idle count over the 5-minute steps of
  the last 24 hours that have a value, then multiply by 24. A step has no
  value when no node, or no CPU, is idle (ParallelCluster), and when
  Prometheus has no data for it yet, as in the first 24 hours of a head or
  login node (both platforms). Measured in the v2.12 validation: on the
  example cluster in `parallelcluster-setup/pcluster.yaml`, after one
  25-minute job, Idle node-hours read 24.0 where its 5-minute samples add
  up to 0.25, and Idle CPU-hours read 17.6 against 0.92. On AWS PCS, on a
  login node with 35 minutes of data, Idle node-hours read 10.3 against
  0.25.
- **Storage: the EFS Storage Bytes graph has no Infrequent Access line.**
  Its query asks for `storage_class="InfrequentAccess"`, but CloudWatch,
  and so cloudwatch-exporter, names that storage class `IA`. The Total and
  Standard lines are correct.

This release was validated live on 2026-11-26. On ParallelCluster 3.16.3:
Amazon Linux 2023 (the head node with `Imds.Secured`, and CPU and GPU
nodes, T4 and L4, on the stock AMI and on custom AMIs built with
`build-ami.sh`), Ubuntu 24.04 (the head node, and CPU and T4 GPU nodes),
the example cluster in `parallelcluster-setup/pcluster.yaml` with only the
least-privilege policy, and a v2.11 cluster upgraded in place as the
Upgrade notes say. On AWS PCS: Slurm 25.11 (login and CPU nodes on the
AL2023 sample AMI, and a GPU node on the PCS-ready DLAMI), upgraded in
place to 26.05, before and after the nodes were replaced.

## v2.11 — 2026-11-25

Keeps secrets out of the install trace, speeds up compute-node boot on
custom AMIs, and corrects the docs. Includes the #55 GPU metrics fix that
shipped in v2.10.3. No breaking changes; the install log is now readable by
root only. Problems that validation found, which earlier releases share, are
listed under Known issues at the end.

### Security
- **The install trace exposed the Grafana admin password and the Cognito
  client secret** (v2.0 through v2.10.3). `post-install.sh` runs the
  installer under `bash -x`. The trace printed the generated Grafana admin
  password and, when Cognito SSO is configured, the Cognito JSON with the
  app client secret. It was written to:
  - `/var/log/parallelcluster-monitoring-install.log`, which was
    world-readable (0644);
  - on ParallelCluster, the head node's `/var/log/cfn-init.log`, which ships
    to CloudWatch Logs by default and is shown in the cfn-init panel of the
    Cluster Logs dashboard;
  - on AWS PCS, the login node's `/var/log/cloud-init-output.log`.

  So local users on the head or login node, principals that can read the
  cluster's CloudWatch log group, and Grafana users who can open the Cluster
  Logs dashboard (v2.7 and later) could read both secrets. With Cognito SSO,
  that includes every user who can sign in, because Grafana creates an
  account on first sign-in. Deleting a ParallelCluster cluster doesn't end
  the exposure: ParallelCluster keeps the cluster's log group
  (`/aws/parallelcluster/<cluster>-<timestamp>`) unless the cluster config
  sets `DeletionPolicy: Delete` under `Monitoring/Logs/CloudWatch`, and the
  group keeps its events for 180 days by default. v2.11 suspends the trace
  around the code that handles the two secrets, keeps the rest of the
  trace, and makes the install log readable by root only, also when it
  replaces a log from an older version.

#### If you deployed v2.0 through v2.10.3

In these commands, `<platform>` is `parallelcluster` or `pcs`, and
`<cluster>` is the cluster name (on PCS, the cluster ID). Run the `aws`
commands with credentials for the cluster's account.

1. **Rotate the Grafana admin password.**
   ```bash
   aws ssm put-parameter --region <region> --overwrite --type SecureString \
       --name "/<platform>/<cluster>/grafana/admin-password" \
       --value "$(openssl rand -hex 16)"
   ```
   `grafana-password-refresh.timer` on the head/login node applies the new
   value within 5 minutes. To apply it at once, run
   `sudo systemctl start grafana-password-refresh.service` on that node.
   Also check Grafana's users and service accounts for entries you didn't
   create.
2. **If you use Cognito SSO, rotate the app client secret.** An app client
   holds at most two secrets, and Cognito accepts both until you delete
   one. So add a new secret, load it on every node that uses the app
   client, check each node, and only then delete the old secret.
   - **2a. Add a new secret and store it.** In `PARAMS`, list the Cognito
     parameter of every cluster that uses this app client, one quoted entry
     per cluster, so that they all get the same new secret. Fill in the
     first four lines, then run the block unchanged, in bash or zsh, with
     `aws`, `jq` and `sha256sum` installed:
     ```bash
     REGION=<region>
     POOL_ID=<pool-id>
     CLIENT_ID=<client-id>
     PARAMS=("/<platform>/<cluster>/grafana/cognito")
     (
         set -euo pipefail
         printf '' | sha256sum >/dev/null
         for p in "${PARAMS[@]}"; do
             json=$(aws ssm get-parameter --region "${REGION}" --name "${p}" \
                 --with-decryption --query Parameter.Value --output text)
             jq -e --arg c "${CLIENT_ID}" '.client_id == $c' <<<"${json}" >/dev/null ||
                 { echo "Stopped: ${p} is not for client ${CLIENT_ID}" >&2; exit 1; }
         done
         new_secret=$(aws cognito-idp add-user-pool-client-secret \
             --region "${REGION}" --user-pool-id "${POOL_ID}" \
             --client-id "${CLIENT_ID}" --output json |
             jq -er '.ClientSecretDescriptor.ClientSecretValue')
         [ -n "${new_secret}" ]
         new_hash=$(printf '%s' "${new_secret}" | sha256sum)
         echo "sha256 of the new secret: ${new_hash%% *}"
         for p in "${PARAMS[@]}"; do
             json=$(aws ssm get-parameter --region "${REGION}" --name "${p}" \
                 --with-decryption --query Parameter.Value --output text)
             json=$(jq -c --arg s "${new_secret}" '.client_secret = $s' <<<"${json}")
             aws ssm put-parameter --region "${REGION}" --overwrite \
                 --type SecureString --name "${p}" --value "${json}" >/dev/null
             echo "Stored the new secret in ${p}"
         done
     )
     ```
     The block checks the tools and every parameter before it changes
     anything, and stops at the first error. If it stops before it prints
     the hash, nothing has changed. If it stops after that, the app client
     has a new secret that no node uses yet: delete the newest secret with
     the commands in 2d, then run the block again. If
     `add-user-pool-client-secret` fails because the app client already has
     two secrets, finish or undo that earlier rotation first.
   - **2b. Load the new secret on each node.** On each head or login node
     of the clusters in `PARAMS`, re-run the installer, then restart
     Grafana, which reads the secret only when it starts. On v2.6.4 and
     later, the installer is in `/opt`:
     ```bash
     sudo bash /opt/aws-parallelcluster-monitoring/installer/install.sh
     sudo docker restart grafana
     ```
     On v2.0 through v2.6.3, it is in the home directory of the cluster's
     default OS user (`<cluster-user>`, such as `ec2-user` or `ubuntu`):
     ```bash
     sudo bash /home/<cluster-user>/aws-parallelcluster-monitoring/installer/install.sh
     sudo docker restart grafana
     ```
   - **2c. Check each node.** The installer must print
     `Cognito env file written to /run/grafana-secrets/cognito.env`, and
     this command must print the hash from 2a:
     ```bash
     sudo sha256sum /run/grafana-secrets/cognito-client-secret
     ```
     Signing in through Cognito doesn't show which secret a node uses,
     because Cognito accepts both secrets until you delete one.
   - **2d. Delete the old secret** once every node passes 2c. The list shows
     each secret's ID and creation date; delete the older one:
     ```bash
     aws cognito-idp list-user-pool-client-secrets --region <region> \
         --user-pool-id <pool-id> --client-id <client-id> \
         --query 'ClientSecrets[].[ClientSecretId,ClientSecretCreateDate]' --output text
     aws cognito-idp delete-user-pool-client-secret --region <region> \
         --user-pool-id <pool-id> --client-id <client-id> --client-secret-id <older-id>
     ```
3. **Remove the old copies of the trace.** After steps 1 and 2 they hold
   only revoked values.
   - On the head/login node, delete the old install log:
     `sudo rm -f /var/log/parallelcluster-monitoring-install.log`.
   - On ParallelCluster, the head node's `/var/log/cfn-init.log` and its
     `<hostname>.<instance-id>.cfn-init` stream in the cluster's log group
     (`/aws/parallelcluster/<cluster>-<timestamp>`) hold the trace. To
     remove the stream:
     ```bash
     aws logs describe-log-streams --region <region> --log-group-name <log-group> \
         --query "logStreams[?ends_with(logStreamName, '.cfn-init')].logStreamName" --output text
     aws logs delete-log-stream --region <region> --log-group-name <log-group> \
         --log-stream-name <stream>
     ```
   - On AWS PCS, the login node's `/var/log/cloud-init-output.log` holds the
     same trace.
4. **If you deleted a ParallelCluster cluster that ran v2.0 through
   v2.10.3, clean up what it left behind.** The installer's SSM parameters
   are not part of the cluster stack, so they survive the deletion. A new
   cluster with the same name reuses the stored admin password, also on
   v2.11.
   - Before you reuse the cluster name, delete the stored password, or
     rotate it with step 1 once the new cluster is running:
     ```bash
     aws ssm delete-parameter --region <region> \
         --name "/parallelcluster/<cluster>/grafana/admin-password"
     ```
   - If the cluster used Cognito SSO and its app client still exists,
     rotate the app client's secret once, with step 2. If a running cluster
     uses the app client, for example a new cluster with the same name, run
     all of step 2 once for that app client, with 2b and 2c on the running
     cluster; if you already did that in step 2, there is nothing more to do
     for the secret. If no running cluster uses the app client, run only
     2a, with the deleted cluster's Cognito parameter in `PARAMS`, and then
     2d.
   - Delete the retained log group, or only its head node's `*.cfn-init`
     stream with the commands in step 3. Each earlier cluster with that name
     left a group named `/aws/parallelcluster/<cluster>-<timestamp>`. The
     prefix also lists clusters whose names start with `<cluster>-`, so
     check the names before you delete:
     ```bash
     aws logs describe-log-groups --region <region> \
         --log-group-name-prefix "/aws/parallelcluster/<cluster>-" \
         --query 'logGroups[].logGroupName' --output text
     aws logs delete-log-group --region <region> --log-group-name <log-group>
     ```
5. **Use v2.11 from now on.** `latest` resolves to v2.11 once it is
   released; pin `v2.11` in both the `Script` URL and `Args` to make that
   explicit. If you re-run `post-install.sh` on an existing head or login
   node, use v2.11 or later: older versions write the Cognito secret to the
   trace again.

### Added
- **Custom compute-node AMI guide + tooling.** `docs/custom-ami.md` documents
  building a ParallelCluster custom AMI with the monitoring stack pre-baked so
  compute nodes stop doing network-bound install work on every scale-up.
  Ships `parallelcluster-setup/build-ami.sh` (resolves the official base AMI
  and runs `pcluster build-image`), the CPU/GPU build templates
  (`build-image-cpu.yaml` / `build-image-gpu.yaml`), and an EC2 Image Builder
  component (`build-custom-ami-component.sh`) that reuses the installer's own
  OS scripts and pre-pulls the pinned images from `compose/*.yml`.

### Changed
- **Idempotent Docker/Compose bootstrap.** The AL2/AL2023 OS scripts fetched
  the ~32 MB Docker Compose plugin binary with `curl` on every boot. The
  pinned version now lives in `installer/common.sh` and
  `install_compose_plugin_binary()` skips the download when that version is
  already present.
- **Pre-baked-AMI fast path.** `installer/install.sh` now skips the whole
  per-OS bootstrap when the docker daemon + compose plugin are already usable
  (custom AMI or a prior run), via `docker_runtime_ready()`. The skipped
  bootstrap is also what installs `jq` and `bc`, so on head and login nodes
  the fast path installs whichever of them is missing: the Cognito setup and
  the credential refresh need `jq`, and the cost metrics need `bc`. It makes no
  package-manager call when both are present, and compute nodes skip this
  step.

### Fixed
- **Two landing-page quick links pointed at missing dashboards.** The
  Compute Node List and Cluster Costs links used dashboard UIDs that no
  longer exist. They now open `compute-node-list` and `cluster-costs`.

### Documentation
- **The README's complete example now boots.** It linked
  `test-clusters/pc-cluster.yaml`, which downloads the installer from a
  branch that doesn't exist (HTTP 404). It now links
  `parallelcluster-setup/pcluster.yaml`, refreshed and pinned to `v2.11` in
  both `Script` and `Args`. The dead branch refs in `test-clusters/` now
  point at `main`.
- **Supported platforms.** ParallelCluster 3.14–3.16; 3.10–3.13 are past
  their AWS end-of-support date. Amazon Linux 2 only on 3.14–3.15, because
  ParallelCluster 3.16 dropped it. On AWS PCS, Ubuntu 22.04/24.04 PCS AMIs,
  such as the PCS-ready DLAMI (Ubuntu 24.04), are listed next to the AL2023
  sample AMIs.
- **Custom-AMI guide.** `docs/custom-ami.md` no longer claims that a custom
  AMI removes every per-boot network dependency. Nodes still download the
  installer from GitHub on every boot, and GPU nodes still contact the
  NVIDIA repository and restart Docker. The guide also states that the fast
  path needs v2.11 or later, and gives the measured `dcgm-exporter` image
  size (about 142 MB to download, about 410 MB on disk) instead of
  "~1.4 GB".

Validated live on ParallelCluster 3.15.1 / Amazon Linux 2023 (July 2026): a
custom-AMI CPU node reached `node-exporter`-running ~17 s faster than a
stock-AMI control with zero Compose downloads and zero image pulls at boot;
a GPU node served both `node-exporter` and the `dcgm-exporter` image (about
410 MB on disk) from the pre-baked cache with zero pulls. This release was
validated again on ParallelCluster 3.16.3 with Amazon Linux 2023 (the head
node, and CPU and GPU nodes on the stock AMI and on custom AMIs) and on AWS
PCS with Slurm 25.11 (login and CPU nodes on the AL2023 sample AMI, and a GPU
node on the PCS-ready DLAMI). Ubuntu 24.04 head nodes on ParallelCluster need
the workaround under Known issues.

### Known issues
These problems were found while validating v2.11. Earlier releases have them
too; none comes from a v2.11 change. They are planned to be fixed in the next
release (v2.12).

- **ParallelCluster, Ubuntu 24.04 head node: the landing page and Grafana
  are unreachable.** The official ParallelCluster Ubuntu 24.04 AMIs (checked
  on 3.15.1 and 3.16.3) run `apache2` on port 80. The monitoring `nginx`
  container uses the host network, so it can't bind port 80 and keeps
  restarting; `sudo docker logs nginx` shows
  `bind() to 0.0.0.0:80 failed (98: Address in use)`. The install still
  reports success and the cluster reaches `CREATE_COMPLETE`. Compute nodes
  aren't affected. On the head node, stop and disable `apache2`, then
  restart `nginx`:
  ```bash
  sudo systemctl disable --now apache2
  sudo docker restart nginx
  ```
  If you use `apache2` on the head node, move it off ports 80 and 443
  instead.
- **AWS PCS: a node's install can fail with "Cannot detect platform".** The
  platform check pipes the instance's tag list into `grep -q` under
  `set -o pipefail`. When `grep` exits early, `SIGPIPE` can kill the `echo`
  that feeds it, and the check fails even though the `aws:pcs:cluster-id`
  tag is present. This happened on 1 of 4 PCS node boots during validation.
  The install stops, so the node runs none of the monitoring containers, and
  `/var/log/parallelcluster-monitoring-install.log` ends with a
  `Cannot detect platform` error. Re-run the installer on that node:
  ```bash
  sudo bash /opt/aws-parallelcluster-monitoring/installer/install.sh
  ```
- **AWS PCS: the job-to-node mapping is empty.** On the login node,
  `slurm-job-nodes.service` fails on every run. It looks for `squeue` in
  `/opt/slurm/bin`, `/opt/aws-pcs/slurm/bin` and the service's `PATH`, but
  PCS installs it in `/opt/aws/pcs/scheduler/slurm-<version>/bin`. So the
  **Job ID** and **Job Name** columns of the Compute Node List stay empty on
  PCS. There is no tested workaround.
- **Some dashboard panels stay empty, because their queries don't match the
  metrics the stack collects:**
  - Compute Node List, on every platform: the **Load / Core** column. Its
    query divides `node_load1` by a per-node CPU count without
    `on(instance_id)`, so no series match.
  - ParallelCluster: Cluster Summary **Top users (CPUs allocated)**, and
    Slurm Detail **Top Users**, **Top Accounts** and the usage series of
    **Account Quota Utilization**. These panels filter on `alloc|mix` or on
    lowercase `running`/`pending`, but the Slurm exporter (rivosinc 1.8.0)
    reports uppercase job states such as `state="RUNNING"`. It also doesn't
    export the per-user and per-account memory series that some of them
    use.
  - ParallelCluster: Slurm Detail **Backfill Cycles per Minute**. The Slurm
    exporter reports no backfill metrics, so the other two backfill panels
    show 0.

## v2.10.3 — 2026-11-25

GPU metrics fix, backported from `main`. Patch on top of v2.10.2. No breaking
changes. Changes one file (`compose/compute.gpu.yml`).

### Fixed
- **GPU nodes exported zero `DCGM_*` metrics** (#55). The `nvidia-dcgm`
  container started but had no NVML inside: the runtime registered by
  `nvidia-ctk runtime configure` is not the default, and the Compose
  `deploy.resources` device reservation did not inject the GPU, so
  dcgm-exporter logged "NVML doesn't exist on this system" and every GPU
  panel stayed empty. `compose/compute.gpu.yml` now pins
  `runtime: nvidia` on the dcgm-exporter service.

## v2.10.2 — 2026-06-22

Prometheus EC2 service-discovery credential-rotation fix. No breaking changes.
Changes one file (`custom-metrics/refresh-ec2-credentials.sh`).

### Fixed
- **Prometheus EC2 service discovery silently stopped finding compute nodes
  ~6h after container start.** `refresh-ec2-credentials.sh` restarted
  `cloudwatch-exporter` and `grafana` on IMDS key rotation but not
  `prometheus`, on the (incorrect) assumption that Prometheus re-reads the
  credentials file on every `ec2_sd` refresh. It does not: the AWS SDK for
  Go used by `ec2_sd_config` reads `AWS_SHARED_CREDENTIALS_FILE` once at
  startup and caches the provider in-process, so when the IMDS role token
  expires (~every 6h) `DescribeInstances` fails with `RequestExpired`
  (misreported as a clock-skew error) and EC2 service discovery drops every
  EC2-discovered target — node-exporter (`:9100`) and dcgm-exporter
  (`:9400`) — so the node/GPU/EFA dashboards go blank. The login/head-local
  static targets (Slurm exporter, HeadNode node-exporter) keep reporting, so
  Grafana still looks healthy and the breakage is easy to miss. `prometheus`
  is now restarted alongside the other two AWS-credential consumers on key
  rotation, and only when the access key actually changed, so there is no
  extra churn (~4 restarts/day; ~1s downtime, TSDB persists via volume). (#53)

## v2.10.1 — 2026-06-12

RES tag correction. Patch on top of v2.10.

### Fixed
- **RES Node List: removed the always-empty Owner column.** v2.10 sourced an
  Owner column from a `res:Owner` instance tag, but RES does not tag VDI
  instances with the desktop owner — the owner is held in RES's database
  (`idea_session_owner`), not as an EC2 tag (confirmed against the RES source,
  `aws/res`). The column would have been permanently blank. The list now shows
  **Project** (`res:Project`, which RES propagates to every project instance)
  and **Desktop** (the instance `Name` tag) for attribution. The `res_owner`
  relabel was dropped from `prometheus/prometheus-res.yml`.

## v2.10 — 2026-06-12

Amazon RES (Research and Engineering Studio) VDI monitoring, plus a GPU
clock-unit fix. No breaking changes; RES support is fully opt-in and inert
unless `RES_ENVIRONMENT_NAME` is set.

### Added
- **RES desktop monitoring.** RES VDI desktops can now be monitored on the
  same stack already running on a ParallelCluster/PCS head node. They are
  treated as compute nodes (node_exporter + dcgm-exporter on GPU desktops)
  and discovered by their `res:EnvironmentName` tag. (#52)
  - `prometheus/prometheus-res.yml`: a `res_instances` scrape job
    (ports 9100 + 9400) that filters on `tag:res:EnvironmentName` and
    relabels matched instances with a constant `instance_name="RES"`, plus
    `res_project` / `res_owner` / `res_desktop` labels from RES tags. The
    installer appends and templates this snippet **only** when
    `RES_ENVIRONMENT_NAME` is set on the monitoring node — no-op otherwise.
  - `grafana/dashboards/res-node-list.json` (**RES Node List**): fleet table
    of RES desktops — Owner, Project, Instance Type, CPU/Mem/GPU/Disk gauges,
    network, uptime — with click-through to per-node details. GPU% surfaces
    idle GPU desktops for rightsizing (e.g. an overprovisioned g5.48xlarge).
  - `grafana/dashboards/res-node-details.json` (**RES Node Details**):
    per-desktop deep-dive, mirroring Compute Node Details.
  - `installer/platform/res.sh` + platform detection: `post-install.sh` can
    run on a RES desktop as a compute node, detected via `RES_ENVIRONMENT_NAME`
    or the `res:EnvironmentName` IMDS instance tag. Wire it into the RES
    project launch script.

### Fixed
- **GPU Details: SM/Memory clock graphs showed Hz instead of GHz.** The two
  timeseries clock panels applied Grafana's `hertz` unit to the raw DCGM
  value, which is reported in **MHz** — so ~1980 MHz rendered as ~2 kHz. They
  now scale by `1e6` before the unit is applied, matching the (already
  correct) stat panels. `dcgm/counters.csv` documents both clocks as MHz.

## v2.9.1 — 2026-06-04

GPU image configurability. No breaking changes. Contributed by
[@DaisukeMiyamoto](https://github.com/DaisukeMiyamoto), validated on real
B300 (p6-b300) and B200 (p6-b200) hardware.

### Added
- `compose/compute.gpu.yml`: the dcgm-exporter image is now overridable via
  the `DCGM_EXPORTER_IMAGE` environment variable, defaulting to the existing
  `4.2.0-4.1.0-ubuntu22.04` pin. The installer exports it (empty when unset,
  so the inline compose default applies) before the GPU `docker compose up`.
  Lets newer GPUs that need DCGM ≥ 4.4.0 (e.g. B300) run a capable build —
  supplied by digest to bypass the Docker 29.x OCI-index pull failure (#47) —
  without forking. Fully backward compatible. (#50 via #51)
- README: documented the `DCGM_EXPORTER_IMAGE` override (by-digest example)
  in the compute-node launch-template section.

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
