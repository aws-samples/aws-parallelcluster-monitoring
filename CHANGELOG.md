# Changelog

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
