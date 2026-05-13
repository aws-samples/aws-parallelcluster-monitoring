# Test clusters

Two clusters in `us-east-2` for end-to-end testing of the monitoring stack.
**Not** part of the published release artifacts — this directory exists
only so that the test setup is reproducible.

## What gets created

| Resource | Cost (~) | Reused or new |
|---|---|---|
| 1× t3.medium head node (PC) | $0.042/hr | new |
| 1× t3.medium login node (PCS) | $0.042/hr | new |
| 2× t3.medium compute, always-on | $0.084/hr × 2 = $0.17/hr | new (× 2 clusters) |
| 2× g4dn.xlarge GPU compute, always-on | $0.526/hr × 2 = $1.05/hr | new (× 2 clusters) |
| PCS controller | $0.59/hr | new (PCS only) |
| FSx Lustre `/shared` (12 TB persistent_2) | $0/hr | **reused** (`fs-0473d212290c9a14d`) |
| EFS for `/home` on PCS | $0.30/GB-mo | new (PCS only) |
| Managed EFS for `/home` on PC | included in PC pricing | created by ParallelCluster (`HeadNode.SharedStorageType: Efs`) |

**Total: ~$2.95/hr running.** Tear down with `./teardown.sh` when done.

## Files

- `pc-cluster.yaml` — pcluster 3.15 config (`HeadNode.SharedStorageType: Efs` lets PC manage `/home` itself)
- `pcs-create.sh` — PCS cluster + queues + launch templates create script (uses our own EFS for `/home`)
- `efs-create.sh` — creates the EFS used by PCS for `/home`. Not needed by PC.
- `teardown.sh` — destroys everything in reverse dependency order
- `connect.sh` — prints SSM port-forward commands

## Usage

```bash
cd test-clusters/
bash efs-create.sh         # ~30 seconds (only needed for PCS)
bash pc-create.sh          # ~15-25 minutes (PC creates its own EFS)
bash pcs-create.sh         # ~10-15 minutes (uses the EFS from efs-create.sh)
bash connect.sh            # prints connection commands

# When done:
bash teardown.sh           # blocks until everything is deleted
```

## Notes

- Shared resources from the existing `HPC-Prod-Network` VPC:
  - VPC `vpc-0d442fab9e8cb8611`
  - Private subnet A `subnet-00643c21d668273ca` (us-east-2b)
  - FSx Lustre `fs-0473d212290c9a14d` (12 TB, mount name `fixpjbmv`)
- Both clusters use the `Nicola-Ireland` SSH key and the AL2023 base AMI.
- ParallelCluster runs the v2.4-tagged post-install from the public repo;
  to test a branch instead, edit `pc-cluster.yaml` and re-run `pcluster update-cluster`.
- PCS uses the AWS-supplied PCS sample AMI for AL2023 x86_64 Slurm 25.11.
