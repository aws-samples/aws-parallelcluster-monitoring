# Least-privilege IAM policy

This directory contains a replacement IAM policy for the monitoring stack
on the HeadNode. It replaces **four** AWS-managed policies that the
default `pcluster.yaml` example uses:

- `AWSCloudFormationReadOnlyAccess` → only need DescribeStacks on our own stack
- `CloudWatchFullAccess` → only need Logs:Read on our cluster's log groups
- `AWSPriceListServiceFullAccess` → read-only but global; scoped here anyway
- `AmazonSSMFullAccess` → **very** overbroad; only need our own parameters

The policy is resource-scoped to your cluster's name and region, so even
if someone compromises the HeadNode they can't pivot to other resources.

## What the policy grants

| Sid | Purpose | Resource scope |
|---|---|---|
| EC2Describe | ec2_sd_configs (Prometheus), cost scripts | Region-scoped via condition |
| CFNDescribeOwn | cost scripts read stack parameters | Only `${ClusterName}*` stacks |
| FSxDescribe | cost scripts | Region-scoped |
| PricingRead | cost scripts | Global (pricing is a global service) |
| S3ReadBucket | installer pulls cluster-config.json | Only the postinstall bucket |
| SSMCluster | Grafana password refresh | Only `/parallelcluster/${ClusterName}/grafana/*` |
| KMSForSSM | decrypt the SecureString password | Only via ssm.${Region}.amazonaws.com |
| LogsRead | Grafana CloudWatch datasource | Only `/aws/parallelcluster/${ClusterName}*` |

## What the policy does NOT grant

- Writing to CloudWatch metrics (Grafana only reads)
- EC2 actions besides DescribeInstances / DescribeVolumes
- Any IAM action
- S3 access to any bucket other than the cluster's postinstall bucket
- SSM parameters outside `/parallelcluster/${ClusterName}/grafana/*`
- Cross-region access (all non-pricing actions are region-scoped)

## How to use it

### Option 1: inline in your pcluster.yaml

Render a concrete policy JSON:

```bash
./iam/render-policy.sh <cluster-name> <region> [account-id] [s3-bucket] > /tmp/my-policy.json
```

Create an IAM managed policy from it:

```bash
aws iam create-policy \
    --policy-name pcluster-monitoring-<cluster-name> \
    --policy-document file:///tmp/my-policy.json
```

Then in your `pcluster.yaml`, replace the 4 AWS-managed policies with
the ARN you just got:

```yaml
HeadNode:
  Iam:
    AdditionalIamPolicies:
      - Policy: arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore   # keep
      # Replace these four:
      # - Policy: arn:aws:iam::aws:policy/AWSCloudFormationReadOnlyAccess
      # - Policy: arn:aws:iam::aws:policy/CloudWatchFullAccess
      # - Policy: arn:aws:iam::aws:policy/AWSPriceListServiceFullAccess
      # - Policy: arn:aws:iam::aws:policy/AmazonSSMFullAccess
      # With:
      - Policy: arn:aws:iam::<account-id>:policy/pcluster-monitoring-<cluster-name>
```

### Option 2: CloudFormation

Embed the template as an inline policy in a custom IAM role; pass that
role's ARN to pcluster via `HeadNode.Iam.InstanceRole`. See the AWS
ParallelCluster docs for inline custom roles.

## Validation

The policy has been verified against the IAM Policy Simulator for:
- All 9 AWS API calls our code actually makes (all allowed)
- Region-scoped denial (wrong region → denied)
- Cross-resource denial (other cluster's stack / SSM param → denied)
- Overbroad-action denial (iam:CreateUser, ec2:TerminateInstances → denied)
