---
inclusion: auto
---

# Project Status — aws-parallelcluster-monitoring

Last updated: 2026-05-14

## Current Release

**v2.6** (tagged, released on GitHub)
- Commit: `7bc0df5` on `main`
- Post-v2.6 commits on main: README improvements (`fc519a1`)
- GitHub: https://github.com/aws-samples/aws-parallelcluster-monitoring/releases/tag/v2.6

## What's Been Shipped (v2.0 → v2.6)

### v2.0 — Ground-up rewrite
- OS-aware installer (AL2, AL2023, Ubuntu 22/24, RHEL 9)
- Docker Compose v2, all images pinned
- rivosinc/prometheus-slurm-exporter (replaces vpenso GPLv3 fork)
- Imds.Secured=true credential sidecar
- Per-cluster Grafana password in SSM SecureString
- Optional Cognito SSO
- Least-privilege IAM policy (iam/render-policy.sh)
- Unified cost-metrics.sh
- Self-signed TLS with multi-SAN

### v2.1 — AWS PCS support
- Dual-platform installer (parallelcluster.sh / pcs.sh)
- Slurm 25.11 native OpenMetrics scraping on PCS
- PCS compat recording rules (pcs-compat.yml)
- Login Node List dashboard (PCS-only)
- GPU Node List dashboard

### v2.2 — Bug fixes
- post-install.sh default ref bump
- prometheus.yml: tag-based EC2 SD (replaces instance-type allowlist)
- slurm-job-nodes.sh PCS path detection
- CHANGELOG rewrite

### v2.3 — Slurm Detail dashboard (Phase 3.1)
- 25-panel Slurm Detail dashboard (partitions, users, accounts, scheduler, licenses)
- Cluster Summary: idle node-hours, top users/partitions
- PCS compat rules expanded to 16

### v2.4 — GPU Health + DCP profiling (Phase 3.2)
- 24-panel GPU Health dashboard (XID, throttle, ECC, NVLink, retired pages)
- GPU Node Details: Compute Pipeline Activity row (SM, tensor, FP64/32/16)
- Custom DCGM counters file (dcgm/counters.csv)
- Multiple patch releases (v2.4.1–v2.4.6) fixing PCS login detection, cost-metrics region bug, dashboard UX

### v2.5 — Dashboard UX overhaul (integration-tested)
- All dashboards: consistent navigation links
- Compute Node List: Queue column, correct click-through
- GPU Node List: Hostname-based click-through
- GPU Node Details: System Metrics row (CPU/mem/disk/net)
- Slurm Detail: fixed partition duplication (idle/idle~ states)
- PCS: login-node static scrape, PCS controller cost ($0.59/hr)

### v2.6 — Storage dashboard (Phase 3.3)
- cloudwatch-exporter container (prom/cloudwatch-exporter:v0.16.0)
- FSx Lustre metrics: throughput, IOPS, metadata ops, free capacity, CPU/disk utilization
- EFS metrics: throughput, IO limit, connections, permitted throughput, storage by class
- 18-panel Storage (FSx + EFS) dashboard
- Auto-discovers all file systems in the region via CloudWatch dimensions

## Test Clusters (STILL RUNNING — ~$3/hr)

| Cluster | Type | Head/Login Instance | Region |
|---|---|---|---|
| monitoring-test-pc | ParallelCluster 3.15 | i-07d407faeb5101e09 | us-east-2 |
| monitoring-test-pcs | AWS PCS (Slurm 25.11) | i-03c95c70ae3f925cb | us-east-2 |

**Teardown command**: `cd test-clusters && bash teardown.sh`

**Access**:
- PC: `aws ssm start-session --target i-07d407faeb5101e09 --region us-east-2 --document-name AWS-StartPortForwardingSession --parameters 'portNumber=["443"],localPortNumber=["8443"]'` → https://localhost:8443/grafana/
- PCS: same with `i-03c95c70ae3f925cb` and localPort 8444 → https://localhost:8444/grafana/
- PC password: `aws ssm get-parameter --region us-east-2 --name /parallelcluster/monitoring-test-pc/grafana/admin-password --with-decryption --query Parameter.Value --output text`
- PCS password: `aws ssm get-parameter --region us-east-2 --name /pcs/pcs_8ces3z8kz9/grafana/admin-password --with-decryption --query Parameter.Value --output text`

## Pending Phases

### Phase 3.4 — Cost-by-job + idle cost (POSTPONED)
- Extend cost-metrics.sh: idle cost/hr, cost per user, cost per account
- costs.json additions: idle cost stat, cost-by-user table, cost-by-account table
- Spot vs On-Demand split if discoverable from EC2 InstanceLifecycle

### Phase 3.5 — EFA errors + login-node-list parity (POSTPONED)
- EFA error panels in compute-node-details.json
- Login-node-list: mirror compute-node-list structure (gauges, click-through, network/uptime)

### v3.0 tag — deferred until traction confirmed

## Open Issues

- **#13**: Fork/Branch for AWS Managed Grafana/Prometheus (feature request, on Roadmap)

## Known Issues / Tech Debt

1. **Race condition on compute nodes**: node_exporter sometimes doesn't start on first boot (docker not ready when install.sh runs during cloud-init). Workaround: re-run install.sh. Fix: add a retry/wait loop after `systemctl start docker`.
2. **post-install.sh `local` keyword**: `installer/install.sh` uses `local` inside the `head|login` case block which is technically invalid (not inside a function). Works in bash but shellcheck would flag it.
3. **pcluster-template.config**: legacy PC 2.x INI format file still in the repo. Should be removed or clearly marked as deprecated.
4. **Least-privilege IAM policy**: doesn't include `cloudwatch:GetMetricStatistics` / `cloudwatch:ListMetrics` needed by the new cloudwatch-exporter. Users on the least-privilege policy won't get storage metrics until the policy template is updated.
5. **PCS compute nodes**: the `pc_queue` label shows the node-group ID (e.g. `pcs_g6lw6znqqe`) instead of a human-readable queue name. Would need a recording rule or relabel to map ng-id → queue-name.

## Architecture Note (from user feedback)

> "We should consider following the same approach as for PCS. Instead of using the headnode, lets install the dashboard with all the components in a login node, so we do not interfere with headnode which is a single point of failure."

This is a Phase 4 consideration — moving the monitoring stack from HeadNode to a LoginNode on ParallelCluster. Requires:
- PC 3.8+ LoginNodes section in pcluster.yaml
- Platform detection to identify LoginNode vs HeadNode vs Compute
- The login node needs the same IAM permissions as the current head node
- Slurm exporter needs access to slurmctld (login nodes have it)

## Repo Hygiene

- `development` branch: deleted (merged into main via squash)
- All open issues except #13: closed with "fixed in v2.0+" comment
- Social preview: `docs/social-preview.png` generated locally (not committed), upload manually to GitHub Settings
- LinkedIn post: drafted, ready to publish
