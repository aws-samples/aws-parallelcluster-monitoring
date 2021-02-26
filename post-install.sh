[global]
update_check = true
sanity_check = true
cluster_template = w1cluster

[aws]
aws_region_name = us-east-1
aws_access_key_id = AKIAINWVAZD6X7LDAZAA
aws_secret_access_key = 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

[cluster w1cluster]
vpc_settings = odyvpc
placement_group = DYNAMIC
placement = compute
key_name = llave_i3
master_instance_type = t3.micro
compute_instance_type = c5.large
cluster_type = spot
disable_hyperthreading = true
initial_queue_size = 2
max_queue_size = 2
maintain_initial_size = true
scheduler = slurm
base_os = centos8
post_install = s3://odyhpc.bucket102/post_install.sh
post_install_args = https://github.com/coderodyhpc/aws-parallelcluster-monitoring/tarball/main,coderodyhpc-aws-parallelcluster-monitoring,install-monitoring.sh
additional_iam_policies = arn:aws:iam::aws:policy/CloudWatchFullAccess,arn:aws:iam::aws:policy/AWSPriceListServiceFullAccess,arn:aws:iam::aws:policy/AmazonSSMFullAccess,arn:aws:iam::aws:policy/AWSCloudFormationReadOnlyAccess
tags = {"Grafana" : "true"}

[vpc odyvpc]
master_subnet_id = subnet-b9ec6be5
vpc_id = vpc-a73ee9dd

[aliases]
ssh = ssh {CFN_USER}@{MASTER_IP} {ARGS}

