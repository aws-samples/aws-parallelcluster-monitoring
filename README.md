# Grafana Dashboard for AWS ParallelCluster 

This is a sample solution based on Grafana for monitoring various component of an HPC cluster built with AWS ParallelCluster.
There are 6 dashboards that can be used as they are or customized as you need.
* [ParallelCluster Summary](https://github.com/aws-samples/aws-parallelcluster-monitoring/blob/main/grafana/dashboards/ParallelCluster.json) - this is the main dashboard that shows general monitoring info and metrics for the whole cluster. It includes Slurm metrics and Storage performance metrics.
* [HeadNode Details](https://github.com/aws-samples/aws-parallelcluster-monitoring/blob/main/grafana/dashboards/master-node-details.json) - this dashboard shows detailed metric for the HeadNode, including CPU, Memory, Network and Storage usage.
* [Compute Node List](https://github.com/aws-samples/aws-parallelcluster-monitoring/blob/main/grafana/dashboards/compute-node-list.json) - this dashboard show the list of the available compute nodes. Each entry is a link to a more detailed page.
* [Compute Node Details](https://github.com/aws-samples/aws-parallelcluster-monitoring/blob/main/grafana/dashboards/compute-node-details.json) - similarly to the HeadNode details this dashboard show the same metric for the compute nodes.
* [GPU Nodes Details](https://github.com/aws-samples/aws-parallelcluster-monitoring/blob/main/grafana/dashboards/gpu.json) - This dashboard shows GPUs releated metrics collected using nvidia-dcgm container.
* [Cluster Logs](https://github.com/aws-samples/aws-parallelcluster-monitoring/blob/main/grafana/dashboards/logs.json) - This dashboard shows all the logs of your HPC Cluster. The logs are pushed by AWS ParallelCluster to AWS ClowdWatch Logs and finally reported here.
* [Cluster Costs](https://github.com/aws-samples/aws-parallelcluster-monitoring/blob/main/grafana/dashboards/costs.json)(beta / in developemnt) - This dashboard shows the cost associated to AWS Service utilized by your cluster. It includes: [EC2](https://aws.amazon.com/ec2/pricing/), [EBS](https://aws.amazon.com/ebs/pricing/), [FSx](https://aws.amazon.com/fsx/lustre/pricing/), [S3](https://aws.amazon.com/s3/pricing/), [EFS](https://aws.amazon.com/efs/pricing/).

## Quickstart
Create a cluster using [AWS ParallelCluster](https://www.hpcworkshops.com/03-hpc-aws-parallelcluster-workshop.html) and include the following configuration:

### PC 3.X

Update your cluster's config by adding the following snippet in the `HeadNode` and `Scheduling` section:

```yaml
CustomActions:
  OnNodeConfigured:
    Script: https://raw.githubusercontent.com/aws-samples/aws-parallelcluster-monitoring/main/post-install.sh
    Args:
      - v0.9
Iam:
  AdditionalIamPolicies:
    - Policy: arn:aws:iam::aws:policy/CloudWatchFullAccess
    - Policy: arn:aws:iam::aws:policy/AWSPriceListServiceFullAccess
    - Policy: arn:aws:iam::aws:policy/AmazonSSMFullAccess
    - Policy: arn:aws:iam::aws:policy/AWSCloudFormationReadOnlyAccess
Tags:
  - Key: 'Grafana'
    Value: 'true'
```

See the complete example config: [pcluster.yaml](parallelcluster-setup/pcluster.yaml).

## AWS ParallelCluster
**AWS ParallelCluster** is an AWS supported Open Source cluster management tool that makes it easy for you to deploy and
manage High Performance Computing (HPC) clusters in the AWS cloud.
It automatically sets up the required compute resources and a shared filesystem and offers a variety of batch schedulers such as AWS Batch, SGE, Torque, and Slurm.
* More info on: https://aws.amazon.com/hpc/parallelcluster/
* Source Code on Git-Hub: https://github.com/aws/aws-parallelcluster
* Official Documentation: https://docs.aws.amazon.com/parallelcluster/


## Solution components
This project is build with the following components:

* **Grafana** is an [open-source](https://github.com/grafana/grafana) platform for monitoring and observability. Grafana allows you to query, visualize, alert on and understand your metrics as well as create, explore, and share dashboards fostering a data driven culture. 
* **Prometheus** [open-source](https://github.com/prometheus/prometheus/) project for systems and service monitoring from the [Cloud Native Computing Foundation](https://cncf.io/). It collects metrics from configured targets at given intervals, evaluates rule expressions, displays the results, and can trigger alerts if some condition is observed to be true.  
* The **Prometheus Pushgateway** is on [open-source](https://github.com/prometheus/pushgateway/) tool that allows ephemeral and batch jobs to expose their metrics to Prometheus.
* **[Nginx](http://nginx.org/)** is an HTTP and reverse proxy server, a mail proxy server, and a generic TCP/UDP proxy server.
* **[Prometheus-Slurm-Exporter](https://github.com/vpenso/prometheus-slurm-exporter/)** is a Prometheus collector and exporter for metrics extracted from the [Slurm](https://slurm.schedmd.com/overview.html) resource scheduling system.
* **[Node_exporter](https://github.com/prometheus/node_exporter)** is a Prometheus exporter for hardware and OS metrics exposed by \*NIX kernels, written in Go with pluggable metric collectors.

Note: *while almost all components are under the Apache2 license, only **[Prometheus-Slurm-Exporter is licensed under GPLv3](https://github.com/vpenso/prometheus-slurm-exporter/blob/master/LICENSE)**, you need to be aware of it and accept the license terms before proceeding and installing this component.*


## Example Dashboards

#### Cluster Overview

![ParallelCluster](docs/ParallelCluster.png?raw=true "AWS ParallelCluster")

#### HeadNode Dashboard

![Head Node](docs/HeadNode.png?raw=true "Head Node")

#### ComputeNodes Dashboard

![Compute Node List](docs/List.png?raw=true "Compute Node List")

#### Logs

![Logs](docs/Logs.png?raw=true "AWS ParallelCluster Logs")

#### Cluster Cost

![Costs](docs/Costs.png?raw=true "Best - AWS ParallelCluster Costs")


## Quickstart

1. Create a Security Group that allows you to access the `HeadNode` on Port 80 and 443. In the following example we open the security group up to `0.0.0.0/0` however we highly advise restricting this down further. More information on how to create your security groups can be found [here](https://docs.aws.amazon.com/cli/latest/userguide/cli-services-ec2-sg.html#creating-a-security-group)

```bash
read -p "Please enter the vpc id of your cluster: " vpc_id
echo -e "creating a security group with $vpc_id..."
security_group=$(aws ec2 create-security-group --group-name grafana-sg --description "Open HTTP/HTTPS ports" --vpc-id ${vpc_id} --output text)
aws ec2 authorize-security-group-ingress --group-id ${security_group} --protocol tcp --port 443 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --group-id ${security_group} --protocol tcp --port 80 —-cidr 0.0.0.0/0
```

2. Create a cluster with the post install script [post-install.sh](https://github.com/aws-samples/aws-parallelcluster-monitoring/blob/main/post-install.sh), the Security Group you created above as [AdditionalSecurityGroup](https://docs.aws.amazon.com/parallelcluster/latest/ug/Scheduling-v3.html#yaml-Scheduling-SlurmQueues-Networking-AdditionalSecurityGroups) on the HeadNode, and a few additional IAM Policies. You can find a complete AWS ParallelCluster template [here](parallelcluster-setup/pcluster.yaml). Please note that, at the moment, the installation script has only been tested using [Amazon Linux 2](https://aws.amazon.com/amazon-linux-2/).

```yaml
CustomActions:
  OnNodeConfigured:
    Script: https://raw.githubusercontent.com/aws-samples/aws-parallelcluster-monitoring/main/post-install.sh
    Args:
      - v0.9
Iam:
  AdditionalIamPolicies:
    - Policy: arn:aws:iam::aws:policy/CloudWatchFullAccess
    - Policy: arn:aws:iam::aws:policy/AWSPriceListServiceFullAccess
    - Policy: arn:aws:iam::aws:policy/AmazonSSMFullAccess
    - Policy: arn:aws:iam::aws:policy/AWSCloudFormationReadOnlyAccess
Tags:
  - Key: 'Grafana'
    Value: 'true'
```

3. Connect to `https://headnode_public_ip` or `http://headnode_public_ip` (all `http` connections will be automatically redirected to `https`) and authenticate with the username `admin` and default [Grafana password](https://github.com/aws-samples/aws-parallelcluster-monitoring/blob/main/docker-compose/docker-compose.headnode.yml#L37). A landing page will be presented to you with links to the Prometheus database service and the Grafana dashboards.

![Login Screen](docs/Login1.png?raw=true "Login Screen")
![Login Screen](docs/Login2.png?raw=true "Login Screen")

Note: *Because of the higher volume of network traffic due to the compute nodes continuously pushing metrics to the HeadNode, in case you expect to run a large scale cluster (hundreds of instances), we would recommend to use an instance type slightly bigger than what you planned for your master node.*

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](https://github.com/aws-samples/aws-parallelcluster-monitoring/blob/main/LICENSE) file.
