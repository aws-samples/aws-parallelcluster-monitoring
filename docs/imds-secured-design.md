# Prometheus EC2 service discovery under `Imds.Secured=true`

## The problem

ParallelCluster 3.x defaults to `Imds.Secured: true` on the HeadNode, which
uses `iptables` (`owner` match) to restrict outgoing traffic to IMDS
(169.254.169.254) to a small allowlist of UIDs — typically `root` and a
few pcluster-internal users.

Prometheus runs inside a container as UID 65534 (`nobody`). That UID is
not on the allowlist, so any direct call to IMDS from inside the
container returns empty / times out. Prometheus's `ec2_sd_configs`
therefore fails with `NoCredentialProviders`.

Many customers (including AWS internal) cannot change `Imds.Secured`;
it's enforced by policy scanners and flipping it off generates tickets.
So we need the monitoring stack to work with `Imds.Secured: true` on
by default.

## The solution we chose

A systemd timer on the host (running as root — which IS on the IMDS
allowlist) fetches temporary role credentials via IMDSv2 every 5 minutes
and writes them to a file. The Prometheus container mounts that file
read-only and reads it via the standard AWS SDK credentials-file path.

```
┌────────────────────────────────────────────────────────────┐
│  HeadNode (host)                                           │
│                                                            │
│  systemd timer (as root, every 5 min):                     │
│    refresh-ec2-credentials.sh                              │
│     └─ IMDSv2 PUT /api/token   (root UID — allowed)        │
│     └─ IMDSv2 GET .../security-credentials/<role>          │
│     └─ writes /run/prometheus-ec2-creds/credentials        │
│                      (file 0640, directory 0750, both      │
│                       root:monitoring-creds; tmpfs,        │
│                       atomic rename)                       │
│                                                            │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Prometheus container (UID 65534)                    │  │
│  │  group_add: GID of monitoring-creds                  │  │
│  │                                                      │  │
│  │  AWS_SHARED_CREDENTIALS_FILE=                        │  │
│  │    /run/prometheus-ec2-creds/credentials             │  │
│  │    (bind-mounted read-only from host tmpfs)          │  │
│  │                                                      │  │
│  │  ec2_sd_configs uses these creds → DescribeInstances │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                            │
│  cloudwatch-exporter (UID 65534 since v0.18) and grafana   │
│  (UID 472) mount the same file with the same group_add.    │
└────────────────────────────────────────────────────────────┘
```

## Alternatives we considered and rejected

### Disabling `Imds.Secured`

Not acceptable — customer policy scanners would flag and page.

### `credential_process` in ~/.aws/config

A credential helper script prints JSON on stdin, Prometheus parses it.
Would be cleaner if the script could reach IMDS itself — but the script
runs AS THE PROMETHEUS PROCESS, which is the `nobody` UID that can't
reach IMDS. So the script would still need a file populated by a root
helper, meaning we'd have all the complexity of a credential_process
handler AND all the complexity of the current sidecar. Net cost
without net benefit.

### `credential_process` with SUID wrapper

A tiny C binary marked setuid-root that fetches IMDS on demand. Would
work but introduces setuid-root attack surface for marginal benefit
over a tmpfs file with tight ACLs.

### IMDS proxy on the host

Run a proxy (socat / nginx / custom Go) on the host that listens on
an internal IP and forwards IMDS requests to 169.254.169.254 when
they come from a container. Would let containers call IMDS "directly".
But requires iptables DNAT rules, network-namespace awareness, and
adds a new service to maintain. Complex for minimal gain.

### Container runs as root

Simplest but violates container-isolation principles. node_exporter
and prometheus both publish non-root as the recommended default.

## Security properties of the chosen solution

1. **No persistent-disk exposure**: `/run/` is tmpfs. Credentials never
   touch the EBS root volume and disappear on reboot.
2. **ACL-restricted**: `0750` directory, `0640` file, owner `root`,
   group `monitoring-creds`. Only root and the containers that get the
   group through `group_add` can read the file: prometheus (UID 65534),
   cloudwatch-exporter (UID 65534 since v0.18) and grafana (UID 472).
   Cluster users get "Permission denied".
3. **Short-lived**: IAM role credentials from IMDSv2 expire every
   ~6 hours automatically. We refresh every 5 minutes, so the window
   of exposure for any specific credential pair is ~5 minutes.
4. **Atomic updates**: `tmp + rename` means Prometheus never sees a
   half-written file even mid-refresh.
5. **Same access as IMDS**: the file holds the instance role's
   credentials. On ParallelCluster those include everything
   ParallelCluster attaches to the head node's role plus the cluster's
   `AdditionalIamPolicies`, not only the monitoring policy. Reading the
   file is therefore equivalent to IMDS access, which `Imds.Secured`
   limits to root, so the file is restricted the same way (property 2).

## What would change this design

- **Slurm 25.11 native /metrics endpoints**: much of what we use EC2 SD
  for (discovering compute nodes) could be replaced by querying Slurm
  directly. If we drop `ec2_sd_configs`, Prometheus no longer needs AWS
  credentials at all. This is Phase 3 territory.
- **Container networking redesign**: if we ever move off `network_mode:
  host`, we could add a sidecar in the same pod/network namespace that
  runs privileged and proxies IMDS. But `network_mode: host` is
  deliberate for scraping host metrics, so this isn't on the near-term
  roadmap.
