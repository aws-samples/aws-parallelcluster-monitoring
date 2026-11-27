# Test clusters

Scripts that create a ParallelCluster cluster and an AWS PCS cluster for
end-to-end tests of the monitoring stack, and delete them afterwards. They
aren't part of the release; they keep the test setup reproducible.

## Parameters

Set the parameters in `config.env`, which git ignores (start from
`config.env.example`), or in the environment. [settings.sh](settings.sh)
describes each one. Only two are required:

- `REGION`: the AWS Region of the clusters.
- `SUBNET_ID`: the subnet of every node. It needs a route to the internet,
  through an internet gateway or a NAT gateway, for the installer's
  downloads and for SSM.

`REF` and `REPO` select what the nodes install: by default the `main` branch
of aws-samples/aws-parallelcluster-monitoring. To test a branch of a fork,
set `REPO=<you>/aws-parallelcluster-monitoring` and `REF=<branch>`.

## What gets created

| Resource | Cluster | On-Demand price in us-east-1 |
|---|---|---|
| Head node, t3.medium | ParallelCluster | $0.0416/hr |
| CPU compute node, t3.medium, always on | ParallelCluster | $0.0416/hr |
| GPU compute node, g4dn.xlarge, always on | ParallelCluster | $0.526/hr |
| EFS for `/home`, created and deleted by ParallelCluster | ParallelCluster | $0.30/GB-month |
| Login node, t3.medium | AWS PCS | $0.0416/hr, plus $0.08/hr for AWS PCS |
| CPU compute node, t3.medium, always on | AWS PCS | $0.0416/hr, plus $0.08/hr for AWS PCS |
| GPU compute node, g4dn.xlarge, always on | AWS PCS | $0.526/hr, plus $0.08/hr for AWS PCS |
| Slurm controller, size SMALL | AWS PCS | $0.5825/hr |
| EFS for `/home`, from `efs-create.sh` | AWS PCS | $0.30/GB-month |

**Total: about $0.61/hr for ParallelCluster and $1.43/hr for AWS PCS while
they run**, plus the EBS volumes and the data stored on EFS. Delete them with
`teardown.sh` when you're done.

## Files

- `settings.sh`: the parameters, read by every script.
- `config.env.example`: a starting point for `config.env`.
- `pc-create.sh`: writes the ParallelCluster configuration to
  `.state/pc-cluster.yaml` and creates the cluster. With `--dryrun`, it only
  validates the configuration with `pcluster create-cluster --dryrun true`.
- `efs-create.sh`: creates the EFS file system that the AWS PCS nodes mount
  at `/home`. ParallelCluster doesn't need it.
- `pcs-create.sh`: creates the AWS PCS cluster, its security group, IAM
  roles, launch templates, node groups and queues.
- `connect.sh`: prints the SSM commands that open Grafana and read its admin
  password.
- `teardown.sh`: deletes everything, in reverse dependency order.

`.state/`, which git ignores, holds the generated configuration and the IDs
that `connect.sh` and `teardown.sh` read.

## Usage

```bash
cd test-clusters/
cp config.env.example config.env   # then set REGION, SUBNET_ID and REF
bash pc-create.sh --dryrun         # validates the configuration, creates nothing
bash pc-create.sh                  # 15-25 minutes
bash efs-create.sh                 # about 1 minute, for AWS PCS only
bash pcs-create.sh                 # 10-20 minutes
bash connect.sh                    # prints the connection commands

# When done:
bash teardown.sh                   # blocks until everything is deleted
```

## Notes

- **ParallelCluster version.** `pc-create.sh` and `teardown.sh` run the
  pcluster CLI on `PATH`, or `$PCLUSTER`. The CLI's version is the cluster's
  version. pcluster 3.16 runs on Python 3.9-3.14, so it needs no separate
  Python environment.
- **AMIs.** ParallelCluster uses its official Amazon Linux 2023 AMI. On AWS
  PCS, the login and CPU nodes use the newest AWS PCS sample AMI for Amazon
  Linux 2023, and the GPU node the newest PCS-ready DLAMI (Ubuntu 24.04),
  which has the NVIDIA driver. The scripts look the AMIs up by name; set
  `AMI_ID` or `GPU_AMI_ID` to use others.
- **Slurm.** The AWS PCS cluster runs Slurm `SLURM_VERSION`, 26.05 by default
  (AWS PCS also offers 25.11), with the OpenMetrics settings of the README's
  AWS PCS quickstart. ParallelCluster brings its own Slurm.
- **AWS PCS node groups.** They use custom launch templates (launch template
  mode `CUSTOM`, the API's default), as in the README's AWS PCS quickstart:
  the user data runs `post-install.sh`, and the instance tags mark the login
  node. The login node is a node group in no queue. The scripts don't create
  node groups with standard configuration (`MANAGED`), the AWS PCS console's
  default.
- **IAM.** On ParallelCluster, the head node gets the README's quick-start
  managed policies, and the queues only `AmazonSSMManagedInstanceCore`. On
  AWS PCS, the login node gets the permissions that the README lists, the
  compute nodes only `pcs:RegisterComputeNodeGroupInstance`, and every node
  `AmazonSSMManagedInstanceCore`.
- **Access.** The scripts reach the nodes through SSM, so no key pair is
  needed, and the dry run's `KeyPairValidator` warning is expected. Set
  `KEY_NAME` to add one.
- **Storage.** `FSX_ID` mounts an existing FSx for Lustre file system at
  `/shared` on the ParallelCluster cluster. The Storage dashboard reads the
  CloudWatch metrics of every FSx and EFS file system in the Region, mounted
  or not.
- **Names and tags.** Resource names start with `PREFIX` (`monitoring-test`).
  Every resource has the tag `aws-parallelcluster-monitoring-test`, and the
  tags in `TAGS`.
