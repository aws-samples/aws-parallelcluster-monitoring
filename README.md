# Grafana Dashboard for AWS ParallelCluster

A zero-setup monitoring solution for HPC clusters built with
[AWS ParallelCluster](https://aws.amazon.com/hpc/parallelcluster/).
Deploys Prometheus, Grafana, node_exporter, NVIDIA DCGM exporter, and a
Slurm metrics exporter as containers — no manual configuration required.

## Features

- **Zero-setup**: add 5 lines to your pcluster config, create the cluster, done
- **Secure by default**: per-cluster random password in SSM, optional Cognito SSO
- **GPU-ready**: NVIDIA DCGM exporter auto-deploys on GPU instances
- **EFA-ready**: InfiniBand/EFA metrics collected automatically
- **Cost tracking**: real-time cost/hour estimates + accumulated total
- **Job mapping**: see which Slurm jobs run on which nodes
- **Works with `Imds.Secured=true`**: no IMDS workarounds needed

## Dashboards

| Dashboard | Description |
|-----------|-------------|
| **ParallelCluster Summary** | Cluster overview: Slurm states, CPU/memory aggregates, storage |
| **Compute Node List** | Fleet table with CPU/Mem/Disk gauges, job info, click-through |
| **Compute Node Details** | Per-node deep-dive (CPU, memory, disk, network, EFA) |
| **HeadNode Details** | Head node metrics |
| **GPU Nodes** | NVIDIA metrics: utilization, temperature, power, memory, NVLink, PCIe |
| **Cluster Costs** | Cost/hour breakdown (headnode, compute, EBS) + accumulated total |

## Quickstart

Add to your `pcluster.yaml` under **both** `HeadNode` and each `SlurmQueue`:

```yaml
CustomActions:
  OnNodeConfigured:
    Script: https://raw.githubusercontent.com/aws-samples/aws-parallelcluster-monitoring/v2.0/post-install.sh
    Args:
      - v2.0
Iam:
  AdditionalIamPolicies:
    - Policy: arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
    - Policy: arn:aws:iam::aws:policy/CloudWatchFullAccess
    - Policy: arn:aws:iam::aws:policy/AWSPriceListServiceFullAccess
    - Policy: arn:aws:iam::aws:policy/AmazonSSMFullAccess
    - Policy: arn:aws:iam::aws:policy/AWSCloudFormationReadOnlyAccess
```

> **Tip**: For production, replace the 4 AWS-managed policies with the
> [least-privilege policy](iam/README.md) included in this repo.

Full example: [parallelcluster-setup/pcluster.yaml](parallelcluster-setup/pcluster.yaml).

### Access Grafana

```bash
# SSM port-forward (recommended — no public exposure)
aws ssm start-session --target <head-node-instance-id> --region <region> \
    --document-name AWS-StartPortForwardingSession \
    --parameters 'portNumber=["443"],localPortNumber=["8443"]'
```

Browse: `https://localhost:8443/grafana/`

Retrieve your password:
```bash
aws ssm get-parameter --region <region> \
    --name /parallelcluster/<cluster-name>/grafana/admin-password \
    --with-decryption --query Parameter.Value --output text
```

For public access with a trusted certificate, see [docs/public-access.md](docs/public-access.md).

### Testing from a fork

```yaml
CustomActions:
  OnNodeConfigured:
    Script: https://raw.githubusercontent.com/<you>/aws-parallelcluster-monitoring/<branch>/post-install.sh
    Args:
      - <tag-or-branch>
      - <you>/aws-parallelcluster-monitoring
```

## Supported platforms

| OS | Status | Notes |
|---|---|---|
| Amazon Linux 2023 | ✅ recommended | ParallelCluster 3.8+ default |
| Amazon Linux 2 | ✅ | EOL June 2026 |
| Ubuntu 22.04 / 24.04 | ✅ | |
| RHEL / Rocky / Alma 9 | ✅ | Uses docker-ce upstream repo |

Supported ParallelCluster versions: **3.10 – 3.15**.

## Architecture

```
HeadNode                              Compute Nodes
┌─────────────────────────────┐       ┌──────────────────────┐
│ nginx (TLS)                 │       │ node_exporter :9100   │
│ grafana :3000               │       │ dcgm-exporter :9400   │
│ prometheus :9090            │◄──────│   (GPU nodes only)    │
│ pushgateway :9091           │       └──────────────────────┘
│ node_exporter :9100         │
│ slurm_exporter :9092        │
│ cost-metrics (cron)         │
│ slurm-job-nodes (timer)     │
└─────────────────────────────┘
```

Prometheus discovers compute nodes via EC2 service discovery. No static
configuration needed — nodes are scraped automatically as they join/leave.

## Components

| Component | Version |
|-----------|---------|
| Grafana | 11.2.2 |
| Prometheus | v3.1.0 |
| Pushgateway | v1.11.2 |
| Node Exporter | v1.9.0 |
| NGINX | 1.27-alpine |
| NVIDIA DCGM Exporter | 4.5.2-4.8.1-ubuntu22.04 |
| prometheus-slurm-exporter | 1.8.0 ([rivosinc](https://github.com/rivosinc/prometheus-slurm-exporter)) |
| Docker Compose v2 | 2.29.7 |

All images pinned — `latest` is never used.

## Security

- **Grafana password**: random 32-char hex, stored in SSM Parameter Store (SecureString)
- **Cognito SSO**: optional OAuth2 login — see [cognito/README.md](cognito/README.md)
- **IAM**: least-privilege policy available — see [iam/README.md](iam/README.md)
- **TLS**: self-signed cert with SANs (localhost, private IP, hostname)
- **IMDS**: works with `Imds.Secured=true` via credential sidecar
- **No secrets in code**: passwords, tokens, and keys are in SSM or tmpfs only

## Documentation

- [Public access (ALB + ACM)](docs/public-access.md)
- [IMDS credential sidecar design](docs/imds-secured-design.md)
- [Least-privilege IAM policy](iam/README.md)
- [Cognito SSO setup](cognito/README.md)

## Roadmap

- **Phase 4** (next): Amazon Managed Prometheus / Managed Grafana path,
  CDK module, Slurm 25.11 native `/metrics` endpoints, CI pipeline

## License

MIT-0 — see [LICENSE](LICENSE).
