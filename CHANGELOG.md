# Changelog

## v2.12 — 2026-11-26

Security fixes, the latest stable component versions, fixes for the AWS PCS
compatibility rules, three dashboard gaps and the Cluster Summary node
counts, and corrected IAM documents. It fixes the problems listed under
Known issues in v2.11; only the memory columns of the Slurm user and
account panels can stay empty (see Fixed). Known issues lists what is still
open.

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
- **Grafana 13.3.2.** The dashboards and datasources provision unchanged.
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
  `/opt/aws/pcs/scheduler/slurm-<version>/bin` if there is exactly one. The
  **Job ID** and **Job Name** columns of the Compute Node List fill in on
  PCS, and a node without Slurm exits quietly. Closes the v2.11 known issue
  "AWS PCS: the job-to-node mapping is empty".
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
    exporter now runs with `-slurm.collect-diags` (see Changed).
- **Cluster Summary: Nodes Allocated and Nodes Down/Drain always showed
  0.** Each added two series whose `state` labels differ, and such series
  never match. They now count the nodes in the `alloc` and `mix` states,
  and in the `down` and `drain` states.
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
  the containers that read it, and no longer says that the credentials are
  limited to the monitoring policy.
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
Found while preparing v2.12. Earlier releases have it too.
- **ParallelCluster: the job counts read 0.** Cluster Summary **Jobs
  Running**, **Jobs Pending**, **Jobs Completed**, **Jobs Failed** and
  **Jobs Over Time**, and Slurm Detail **Total Pending Now**, **Total
  Running Now**, **Failed (since boot)** and **Suspended** read the
  `slurm_jobs_*` series, which the ParallelCluster Slurm exporter (rivosinc
  `prometheus-slurm-exporter` 1.8.0) doesn't export. On AWS PCS, Slurm
  exports them and the panels show data. The Slurm Detail **Top Users** and
  **Top Accounts** panels show the running and pending jobs per user and
  account on both platforms.

The v2.12 validation results are added here before the release is
published: live on ParallelCluster 3.16.3 with Amazon Linux 2023 and
Ubuntu 24.04, and on AWS PCS with Slurm 25.11, upgraded in place to 26.05.

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
