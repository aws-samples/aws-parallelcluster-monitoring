#!/usr/bin/env bash
# Create the PCS test cluster: cluster + 3 launch templates +
# 2 always-on compute node groups + queues.
#
# Reuses HPC-Prod-Network VPC, private subnet A, FSx Lustre, EFS.
set -euo pipefail

REGION="us-east-2"
VPC="vpc-0d442fab9e8cb8611"
PRIVATE_SUBNET="subnet-00643c21d668273ca"
PUBLIC_SUBNET="subnet-0c32d359f2a1f38bd"
FSX_DNS="fs-0473d212290c9a14d.fsx.us-east-2.amazonaws.com"
FSX_MOUNT="fixpjbmv"
KEY_NAME="Nicola-Ireland"
AMI_ID="ami-088cb0349ae714ee3"   # AL2023 x86_64 PCS sample, Slurm 25.11
CLUSTER="monitoring-test-pcs"

[[ -f .state/efs.env ]] || { echo "Run efs-create.sh first" >&2; exit 1; }
# shellcheck disable=SC1091
. .state/efs.env

mkdir -p .state

# ─── 1. Create the PCS cluster (slurm controller) ─────────────────────
existing_id=$(aws pcs list-clusters --region "$REGION" \
    --query "clusters[?name=='$CLUSTER'].id | [0]" --output text 2>/dev/null || echo "")

if [[ -n "$existing_id" && "$existing_id" != "None" ]]; then
    echo "PCS cluster already exists: $existing_id"
    CLUSTER_ID="$existing_id"
else
    echo "Creating PCS cluster $CLUSTER..."
    CLUSTER_ID=$(aws pcs create-cluster --region "$REGION" \
        --cluster-name "$CLUSTER" \
        --scheduler 'type=SLURM,version=25.11' \
        --size SMALL \
        --networking "subnetIds=$PRIVATE_SUBNET" \
        --slurm-configuration 'slurmCustomSettings=[{parameterName=MetricsType,parameterValue=metrics/openmetrics},{parameterName=CommunicationParameters,parameterValue=enable_http}]' \
        --tags "aws-parallelcluster-monitoring-test=pcs" \
        --query 'cluster.id' --output text)
    echo "Created PCS cluster id=$CLUSTER_ID, waiting for ACTIVE..."
fi

# Wait for cluster ACTIVE
for _ in $(seq 1 60); do
    state=$(aws pcs get-cluster --region "$REGION" --cluster-identifier "$CLUSTER_ID" \
        --query 'cluster.status' --output text)
    [[ "$state" == "ACTIVE" ]] && break
    echo "  cluster: $state, waiting..."
    sleep 30
done
echo "Cluster ACTIVE"

# Capture slurmctld endpoint info
SLURM_INFO=$(aws pcs get-cluster --region "$REGION" --cluster-identifier "$CLUSTER_ID" \
    --query 'cluster.endpoints[?type==`SLURMCTLD`].privateIpAddress | [0]' --output text)
echo "slurmctld IP: $SLURM_INFO"

# ─── 2. Build user-data for each launch template ──────────────────────
# Mounts FSx + EFS, then runs the monitoring post-install with a tag/role hint.
make_userdata() {
    local role="$1"  # login|compute
    cat <<EOF | base64
#!/bin/bash
set -eux

# Wait for cloud-init network
sleep 10

# Mount FSx Lustre at /shared
dnf -y install lustre-client || amazon-linux-extras install -y lustre || true
mkdir -p /shared
echo "${FSX_DNS}@tcp:/${FSX_MOUNT} /shared lustre defaults,_netdev 0 0" >> /etc/fstab
mount /shared || true

# Mount EFS at /home (preserve existing /home contents)
dnf -y install amazon-efs-utils || true
mkdir -p /mnt/efs-home
echo "${EFS_ID}.efs.${REGION}.amazonaws.com:/ /mnt/efs-home nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,_netdev 0 0" >> /etc/fstab
mount /mnt/efs-home || true
# Move existing /home into EFS only on the login node and only once
if [[ "$role" == "login" ]] && [[ -d /home/ec2-user ]] && [[ ! -d /mnt/efs-home/ec2-user ]]; then
    rsync -a /home/ /mnt/efs-home/ || true
fi
mkdir -p /mnt/efs-home
mount --bind /mnt/efs-home /home || true

# Tag for monitoring identification (compose & dashboards expect Name=Compute or Name=HeadNode)
TOKEN=\$(curl -sS -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
INSTANCE_ID=\$(curl -sS -H "X-aws-ec2-metadata-token: \$TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
case "$role" in
    login)   aws ec2 create-tags --region $REGION --resources \$INSTANCE_ID --tags Key=Name,Value=HeadNode Key=monitoring-role,Value=login ;;
    compute) aws ec2 create-tags --region $REGION --resources \$INSTANCE_ID --tags Key=Name,Value=Compute ;;
esac

# Pull and run the monitoring post-install
curl -fsSL https://raw.githubusercontent.com/aws-samples/aws-parallelcluster-monitoring/v2.4/post-install.sh -o /tmp/post-install.sh
bash /tmp/post-install.sh v2.4 2>&1 | tee /var/log/monitoring-install.log || true
EOF
}

# ─── 3. IAM instance profile ──────────────────────────────────────────
# Need pcs:GetCluster, ec2:Describe*, ssm:GetParameter, fsx:Describe*, pricing:*
ROLE_NAME="monitoring-test-pcs-instance-role"
existing_role=$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text 2>/dev/null || echo "")
if [[ -z "$existing_role" || "$existing_role" == "None" ]]; then
    echo "Creating IAM role $ROLE_NAME..."
    aws iam create-role --role-name "$ROLE_NAME" \
        --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
        --tags "Key=aws-parallelcluster-monitoring-test,Value=pcs" >/dev/null
    for arn in \
        arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore \
        arn:aws:iam::aws:policy/AWSPriceListServiceFullAccess \
        arn:aws:iam::aws:policy/CloudWatchFullAccess \
        arn:aws:iam::aws:policy/AmazonSSMFullAccess \
        arn:aws:iam::aws:policy/AmazonEC2ReadOnlyAccess \
        arn:aws:iam::aws:policy/AWSCloudFormationReadOnlyAccess
    do
        aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$arn"
    done
    # Inline policy for PCS-specific actions (not in any managed policy)
    aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name pcs-describe \
        --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["pcs:GetCluster","pcs:ListClusters","pcs:ListComputeNodeGroups","pcs:GetComputeNodeGroup","pcs:ListQueues","pcs:GetQueue","fsx:DescribeFileSystems"],"Resource":"*"}]}'
    aws iam create-instance-profile --instance-profile-name "$ROLE_NAME" >/dev/null 2>&1 || true
    aws iam add-role-to-instance-profile --instance-profile-name "$ROLE_NAME" --role-name "$ROLE_NAME" 2>/dev/null || true
    sleep 10  # IAM propagation
fi
INSTANCE_PROFILE_ARN=$(aws iam get-instance-profile --instance-profile-name "$ROLE_NAME" --query 'InstanceProfile.Arn' --output text)
echo "Instance profile: $INSTANCE_PROFILE_ARN"

# ─── 4. Security group for PCS instances ──────────────────────────────
SG_NAME="monitoring-test-pcs-instances"
PCS_SG=$(aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC" "Name=group-name,Values=$SG_NAME" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")
if [[ "$PCS_SG" == "None" || -z "$PCS_SG" ]]; then
    PCS_SG=$(aws ec2 create-security-group --region "$REGION" \
        --group-name "$SG_NAME" --description "PCS monitoring test instances" --vpc-id "$VPC" \
        --tag-specifications "ResourceType=security-group,Tags=[{Key=aws-parallelcluster-monitoring-test,Value=pcs}]" \
        --query 'GroupId' --output text)
    # Allow self (intra-cluster slurm)
    aws ec2 authorize-security-group-ingress --region "$REGION" \
        --group-id "$PCS_SG" --source-group "$PCS_SG" --protocol -1 --port -1 >/dev/null
    # Allow access from the Lustre SG and EFS SG
    aws ec2 authorize-security-group-ingress --region "$REGION" \
        --group-id "$EFS_SG_ID" --source-group "$PCS_SG" --protocol tcp --port 2049 >/dev/null
fi
echo "PCS instance SG: $PCS_SG"

# Slurmctld port 6817 — needs to allow login node → controller
# (PCS cluster-scoped SG handles slurmctld→nodes by default, but the
# login node will scrape :6817/metrics/* which requires explicit ingress).
# The PCS-managed cluster security group allows traffic from members,
# so adding the login node's SG to the cluster network attachments
# happens automatically. No extra rule needed here.

# ─── 5. Launch templates ──────────────────────────────────────────────
make_lt() {
    local name="$1" instance_type="$2" role="$3"
    local lt_id

    lt_id=$(aws ec2 describe-launch-templates --region "$REGION" \
        --filters "Name=launch-template-name,Values=$name" \
        --query 'LaunchTemplates[0].LaunchTemplateId' --output text 2>/dev/null || echo "None")

    local userdata
    userdata=$(make_userdata "$role")

    local lt_data
    lt_data=$(cat <<JSON
{
  "ImageId": "$AMI_ID",
  "InstanceType": "$instance_type",
  "KeyName": "$KEY_NAME",
  "SecurityGroupIds": ["$PCS_SG"],
  "IamInstanceProfile": {"Arn": "$INSTANCE_PROFILE_ARN"},
  "MetadataOptions": {"HttpTokens":"required","HttpEndpoint":"enabled","InstanceMetadataTags":"enabled"},
  "UserData": "$userdata",
  "TagSpecifications": [
    {"ResourceType":"instance","Tags":[{"Key":"aws-parallelcluster-monitoring-test","Value":"pcs"}]}
  ]
}
JSON
)

    if [[ "$lt_id" == "None" || -z "$lt_id" ]]; then
        lt_id=$(aws ec2 create-launch-template --region "$REGION" \
            --launch-template-name "$name" \
            --launch-template-data "$lt_data" \
            --query 'LaunchTemplate.LaunchTemplateId' --output text)
        echo "Created LT $name = $lt_id"
    else
        # Update by creating a new version, then set as default
        local v
        v=$(aws ec2 create-launch-template-version --region "$REGION" \
            --launch-template-id "$lt_id" \
            --launch-template-data "$lt_data" \
            --query 'LaunchTemplateVersion.VersionNumber' --output text)
        aws ec2 modify-launch-template --region "$REGION" \
            --launch-template-id "$lt_id" --default-version "$v" >/dev/null
        echo "Updated LT $name to version $v"
    fi
    echo "$lt_id"
}

LT_LOGIN=$(make_lt "monitoring-test-pcs-login" "t3.medium" "login")
LT_CPU=$(make_lt "monitoring-test-pcs-compute-cpu" "t3.medium" "compute")
LT_GPU=$(make_lt "monitoring-test-pcs-compute-gpu" "g4dn.xlarge" "compute")

# ─── 6. Compute Node Groups (always-on) ───────────────────────────────
make_node_group() {
    local name="$1" lt_id="$2" min="$3" max="$4" purchase="$5"
    local existing
    existing=$(aws pcs list-compute-node-groups --region "$REGION" --cluster-identifier "$CLUSTER_ID" \
        --query "computeNodeGroups[?name=='$name'].id | [0]" --output text 2>/dev/null || echo "")
    if [[ -n "$existing" && "$existing" != "None" ]]; then
        echo "Node group $name already exists: $existing"
        echo "$existing"; return
    fi
    aws pcs create-compute-node-group --region "$REGION" \
        --cluster-identifier "$CLUSTER_ID" \
        --compute-node-group-name "$name" \
        --subnet-ids "$PRIVATE_SUBNET" \
        --custom-launch-template "id=$lt_id,version=\$Default" \
        --iam-instance-profile-arn "$INSTANCE_PROFILE_ARN" \
        --scaling-configuration "minInstanceCount=$min,maxInstanceCount=$max" \
        --instance-configs '[{"instanceType":"'"$( aws ec2 describe-launch-templates --region $REGION --launch-template-ids $lt_id --query 'LaunchTemplates[0].LaunchTemplateName' --output text | grep -o 'gpu\|cpu\|login' | sed -e s/cpu/t3.medium/ -e s/login/t3.medium/ -e s/gpu/g4dn.xlarge/ )"'"}]' \
        --purchase-option "$purchase" \
        --query 'computeNodeGroup.id' --output text
}

# Login: not a "compute node group" in PCS terms — we run it as a separate EC2
# instance attached to the cluster, using the login launch template directly.

# Compute (always on)
NG_CPU=$(make_node_group "compute-cpu" "$LT_CPU" 2 4 ON_DEMAND)
echo "CPU node group: $NG_CPU"
NG_GPU=$(make_node_group "compute-gpu" "$LT_GPU" 2 4 ON_DEMAND)
echo "GPU node group: $NG_GPU"

# Wait for node groups ACTIVE
for ng in "$NG_CPU" "$NG_GPU"; do
    for _ in $(seq 1 30); do
        s=$(aws pcs get-compute-node-group --region "$REGION" --cluster-identifier "$CLUSTER_ID" \
            --compute-node-group-identifier "$ng" --query 'computeNodeGroup.status' --output text)
        [[ "$s" == "ACTIVE" ]] && break
        echo "  ng $ng: $s"
        sleep 20
    done
done

# ─── 7. Queues ────────────────────────────────────────────────────────
make_queue() {
    local name="$1" ng_id="$2"
    local existing
    existing=$(aws pcs list-queues --region "$REGION" --cluster-identifier "$CLUSTER_ID" \
        --query "queues[?name=='$name'].id | [0]" --output text 2>/dev/null || echo "")
    if [[ -n "$existing" && "$existing" != "None" ]]; then
        echo "Queue $name already exists: $existing"; return
    fi
    aws pcs create-queue --region "$REGION" --cluster-identifier "$CLUSTER_ID" \
        --queue-name "$name" --compute-node-group-configurations "computeNodeGroupId=$ng_id" >/dev/null
    echo "Queue $name created"
}
make_queue "cpu" "$NG_CPU"
make_queue "gpu" "$NG_GPU"

# ─── 8. Login node (a plain EC2 instance running the login LT) ────────
# PCS doesn't have a managed login-node concept; you run a normal EC2
# instance with the cluster's slurm client installed (the sample AMI
# already has it) and add it to the cluster's security group network
# attachment so it can talk to slurmctld.
existing_login=$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=HeadNode" "Name=tag:aws-parallelcluster-monitoring-test,Values=pcs" "Name=instance-state-name,Values=running,pending" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null || echo "")

if [[ -n "$existing_login" && "$existing_login" != "None" ]]; then
    LOGIN_ID="$existing_login"
    echo "Login node already exists: $LOGIN_ID"
else
    echo "Launching login node..."
    LOGIN_ID=$(aws ec2 run-instances --region "$REGION" \
        --launch-template "LaunchTemplateId=$LT_LOGIN,Version=\$Default" \
        --subnet-id "$PUBLIC_SUBNET" \
        --query 'Instances[0].InstanceId' --output text)
    echo "Login node: $LOGIN_ID"
fi

# Grant the login node access to the PCS cluster's slurmctld (port 6817) by
# putting it in the same SG that PCS automatically adds to slurmctld's network
# attachment. We're already on $PCS_SG via the LT, which the cluster security
# group is configured to accept by default — no extra rule needed.

# ─── 9. Save state ────────────────────────────────────────────────────
{
    echo "PCS_CLUSTER=$CLUSTER"
    echo "PCS_CLUSTER_ID=$CLUSTER_ID"
    echo "PCS_SLURMCTLD_IP=$SLURM_INFO"
    echo "PCS_LOGIN_ID=$LOGIN_ID"
    echo "PCS_NG_CPU=$NG_CPU"
    echo "PCS_NG_GPU=$NG_GPU"
    echo "PCS_LT_LOGIN=$LT_LOGIN"
    echo "PCS_LT_CPU=$LT_CPU"
    echo "PCS_LT_GPU=$LT_GPU"
    echo "PCS_SG=$PCS_SG"
    echo "PCS_INSTANCE_PROFILE=$ROLE_NAME"
} > .state/pcs.env
echo
cat .state/pcs.env
echo
echo "PCS cluster created. Compute nodes will boot and run the monitoring"
echo "post-install over the next 5-10 minutes."
