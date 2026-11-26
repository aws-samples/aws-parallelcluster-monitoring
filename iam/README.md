# Least-privilege IAM policy

This directory contains a least-privilege IAM policy for the monitoring
stack on the ParallelCluster HeadNode. It replaces the **five**
AWS-managed policies of the README's quick-start alternative:

- `CloudWatchFullAccess` → only reads of CloudWatch metrics in the
  cluster's region, plus Logs reads and Logs Insights queries on the
  cluster's log groups
- `AWSPriceListServiceFullAccess` → only `pricing:GetProducts`
- `AmazonSSMFullAccess` → **very** overbroad; only the cluster's own Grafana
  parameters
- `AWSCloudFormationReadOnlyAccess` → not needed: the monitoring stack makes
  no CloudFormation calls
- `AmazonEC2ReadOnlyAccess` → only `ec2:DescribeInstances`,
  `ec2:DescribeVolumes` and `ec2:DescribeSpotPriceHistory`, in the
  cluster's region

Attach it to the **HeadNode only**. Compute nodes run only node_exporter
(and dcgm-exporter on GPU nodes) and make no AWS calls for monitoring.

The policy is resource-scoped to your cluster's name and region, so even
if someone compromises the HeadNode they can't pivot to other resources.

## What the policy grants

| Sid | Actions and what calls them | Resource scope |
|---|---|---|
| EC2DescribeForPrometheusSdAndCostMetrics | `ec2:DescribeInstances`: Prometheus EC2 service discovery and the cost collector (`cluster-cost-collector`). `ec2:DescribeVolumes` and `ec2:DescribeSpotPriceHistory`: the cost collector, for the EBS volumes of cluster instances and Spot prices | Region-scoped via condition |
| PricingReadForCostMetrics | `pricing:GetProducts`: the cost collector, for EC2, EBS and AWS PCS list prices | Global (pricing is a global service) |
| S3ReadPostInstallBucketOnly | `s3:GetObject`, `s3:GetObjectVersion`, `s3:ListBucket`: only if you host `post-install.sh` in your own S3 bucket. `render-policy.sh` adds this statement only when you pass the bucket | Only that bucket |
| SSMGrafanaPasswordForThisClusterOnly | `ssm:GetParameter`, `ssm:PutParameter`, `ssm:AddTagsToResource`: the installer stores the Grafana admin password and reads the optional Cognito config; the password refresh reads the password | Only `/parallelcluster/${ClusterName}/grafana/*` |
| KMSForSSMSecureString | `kms:Decrypt`: decrypt the SecureString parameters | Only via `ssm.${Region}.amazonaws.com` |
| CloudWatchMetricsReadForStorageDashboard | `cloudwatch:ListMetrics`, `cloudwatch:GetMetricStatistics`: cloudwatch-exporter (FSx and EFS metrics for the Storage dashboard) and the Grafana CloudWatch datasource health check | Region-scoped via condition |
| CloudWatchLogsReadForClusterLogs | `logs:StartQuery`, `logs:GetLogGroupFields`, `logs:FilterLogEvents`, `logs:GetLogEvents`: the Cluster Logs dashboard, which runs Logs Insights queries through the Grafana CloudWatch datasource | Only `/aws/parallelcluster/${ClusterName}*` |
| CloudWatchLogsQueryAndListForClusterLogs | `logs:DescribeLogGroups`, `logs:GetQueryResults`, `logs:StopQuery`: the log group list, Logs Insights results, and the datasource health check | Region-scoped via condition. `DescribeLogGroups` and `StopQuery` can't be scoped to a log group |

## What the policy does NOT grant

- Writing CloudWatch metrics or logs (Grafana and cloudwatch-exporter only
  read)
- EC2 actions besides DescribeInstances, DescribeVolumes and
  DescribeSpotPriceHistory
- CloudFormation or FSx API calls (FSx metrics come from CloudWatch)
- Any IAM action
- S3 access, unless you pass a bucket to `render-policy.sh`; then only read
  access to that bucket
- SSM parameters outside `/parallelcluster/${ClusterName}/grafana/*`
- Log events and Logs Insights queries outside
  `/aws/parallelcluster/${ClusterName}*` (`logs:DescribeLogGroups` lists the
  region's log group names, not their events)
- Access in other regions (every statement except pricing and the optional
  S3 bucket is limited to the cluster's region)

## How to use it

### Option 1: a managed policy in your pcluster.yaml

Render a concrete policy JSON:

```bash
./iam/render-policy.sh <cluster-name> <region> [account-id] [s3-bucket] > my-policy.json
```

Create an IAM managed policy from it:

```bash
aws iam create-policy \
    --policy-name pcluster-monitoring-<cluster-name> \
    --policy-document file://my-policy.json
```

If you created the policy from an earlier release, render it again and
make the new document the default version instead:

```bash
aws iam create-policy-version \
    --policy-arn arn:aws:iam::<account-id>:policy/pcluster-monitoring-<cluster-name> \
    --policy-document file://my-policy.json --set-as-default
```

A managed policy keeps at most five versions. If yours has five, delete an
old non-default version first with `aws iam delete-policy-version`.

Then in your `pcluster.yaml`, replace the 5 AWS-managed policies on the
HeadNode with the ARN you just got:

```yaml
HeadNode:
  Iam:
    AdditionalIamPolicies:
      - Policy: arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore   # keep
      # Replace these five:
      # - Policy: arn:aws:iam::aws:policy/AWSCloudFormationReadOnlyAccess
      # - Policy: arn:aws:iam::aws:policy/CloudWatchFullAccess
      # - Policy: arn:aws:iam::aws:policy/AWSPriceListServiceFullAccess
      # - Policy: arn:aws:iam::aws:policy/AmazonSSMFullAccess
      # - Policy: arn:aws:iam::aws:policy/AmazonEC2ReadOnlyAccess
      # With:
      - Policy: arn:aws:iam::<account-id>:policy/pcluster-monitoring-<cluster-name>
```

Don't add it to your `SlurmQueues`. Compute nodes make no AWS calls for
monitoring, and the policy can read and change the Grafana admin password,
so on a compute node any job could do that. Queues need only
`AmazonSSMManagedInstanceCore`, and only for SSM access to the nodes.

### Option 2: CloudFormation

Embed the template as an inline policy in a custom IAM role; pass that
role's ARN to pcluster via `HeadNode.Iam.InstanceRole`. See the AWS
ParallelCluster docs for inline custom roles.

## Validation

vNEXT checks the rendered policy in two ways:
- **IAM policy simulator** (`aws iam simulate-custom-policy`): every action
  in the table is allowed on its intended resource in the cluster's region,
  the cost collector's `ec2:DescribeVolumes` and
  `ec2:DescribeSpotPriceHistory` included. `ec2:DescribeInstances`,
  `ec2:DescribeVolumes` and `ec2:DescribeSpotPriceHistory` in another
  region, another cluster's SSM parameter and log group, and actions the
  policy doesn't list (such as `ec2:TerminateInstances`) are denied. The
  same simulator check covers the five managed policies of the README's
  quick start and of `parallelcluster-setup/pcluster.yaml`.
- **A live cluster whose HeadNode has only this policy** and
  `AmazonSSMManagedInstanceCore`: Prometheus EC2 service discovery finds
  the nodes, the Storage dashboard shows FSx and EFS metrics, the Cluster
  Logs panels return rows, the Grafana CloudWatch datasource passes its
  health check, and the cost collector reports every stage OK, with no
  AccessDenied error.
