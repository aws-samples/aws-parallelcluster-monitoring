#!/usr/bin/env bash
# Create the AWS PCS test cluster, ${PREFIX}-pcs, the way the README's AWS PCS
# quickstart describes: Slurm OpenMetrics on, and compute node groups with
# custom launch templates (launch template mode CUSTOM, the API default)
# whose user data runs post-install.sh and whose instance tags mark the login
# node. It creates:
#   - a security group for the cluster and its nodes;
#   - IAM roles and instance profiles for the login node and the compute
#     nodes, with the permissions the README lists;
#   - three launch templates and three node groups of one node each: login
#     (t3.medium, in no queue), cpu (t3.medium) and gpu (g4dn.xlarge);
#   - the queues cpu and gpu.
# /home is on the EFS file system that efs-create.sh creates; run it first.
#
# AMIs: the newest AWS PCS sample AMI (Amazon Linux 2023) on the login and CPU
# nodes, and the newest PCS-ready DLAMI (Ubuntu 24.04, NVIDIA driver
# included) on the GPU node. Set AMI_ID or GPU_AMI_ID to use other AMIs.
#
# Re-running it reuses what exists. IDs go to .state/pcs.env for connect.sh
# and teardown.sh.
set -euo pipefail
# shellcheck source=test-clusters/settings.sh
. "$(dirname "$0")/settings.sh"
require SUBNET_ID "the ID of the subnet for the cluster and its nodes"
if [[ ! -f "${STATE}/efs.env" ]]; then
    echo "Run efs-create.sh first" >&2
    exit 1
fi
# shellcheck source=/dev/null
. "${STATE}/efs.env"

save() { # save NAME VALUE: record an ID in .state/pcs.env
    echo "$1=$2" >> "${STATE}/pcs.env"
    echo "$1=$2"
}

latest_ami() { # latest_ami NAME-PATTERN: the newest Amazon-owned AMI with that name
    local ami
    ami="$(aws ec2 describe-images --owners amazon --filters "Name=name,Values=$1" Name=state,Values=available \
        --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text)"
    if [[ "${ami}" != ami-* ]]; then
        echo "No AMI named $1 in ${REGION}" >&2
        return 1
    fi
    echo "${ami}"
}

if [[ -z "${AMI_ID}" ]]; then
    AMI_ID="$(latest_ami 'aws-pcs-sample_ami-al2023-x86_64-*')"
fi
if [[ -z "${GPU_AMI_ID}" ]]; then
    GPU_AMI_ID="$(latest_ami 'aws-pcs-ready-dlami-base-ubuntu2404-x86_64-*')"
fi
echo "AMIs: ${AMI_ID} (login and CPU nodes), ${GPU_AMI_ID} (GPU node)"
VPC_ID="$(aws ec2 describe-subnets --subnet-ids "${SUBNET_ID}" --query 'Subnets[0].VpcId' --output text)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
mkdir -p "${STATE}"

# ─── Security group ──────────────────────────────────────────────────────
PCS_SG="$(aws ec2 describe-security-groups \
    --filters "Name=vpc-id,Values=${VPC_ID}" "Name=group-name,Values=${PCS_CLUSTER}" \
    --query 'SecurityGroups[0].GroupId' --output text)"
if [[ "${PCS_SG}" == None ]]; then
    PCS_SG="$(aws ec2 create-security-group --group-name "${PCS_CLUSTER}" --vpc-id "${VPC_ID}" \
        --description "Slurm controller and nodes of ${PCS_CLUSTER}" \
        --tag-specifications "$(jq -n -c --argjson t "$(tag_list pcs)" '[{ResourceType: "security-group", Tags: $t}]')" \
        --query GroupId --output text)"
    # Slurm between the controller and the nodes, and the login node's
    # scrapes of the controller's metrics on port 6817.
    aws ec2 authorize-security-group-ingress --group-id "${PCS_SG}" --source-group "${PCS_SG}" --protocol -1 >/dev/null
    # NFS to the EFS mount target.
    aws ec2 authorize-security-group-ingress --group-id "${EFS_SG_ID}" --source-group "${PCS_SG}" \
        --protocol tcp --port 2049 >/dev/null
fi
save PCS_SG "${PCS_SG}"

# ─── IAM roles and instance profiles ────────────────────────────────────
# PCS requires the path /aws-pcs/ (or a name starting with AWSPCS).
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
REGISTER='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"pcs:RegisterComputeNodeGroupInstance","Resource":"*"}]}'
make_role() { # make_role NAME: a role and an instance profile of that name
    if ! aws iam get-role --role-name "$1" >/dev/null 2>&1; then
        aws iam create-role --path /aws-pcs/ --role-name "$1" --assume-role-policy-document "${TRUST}" \
            --tags "$(tag_list pcs)" >/dev/null
    fi
    # SSM Session Manager, for connect.sh.
    aws iam attach-role-policy --role-name "$1" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
    aws iam put-role-policy --role-name "$1" --policy-name pcs-register --policy-document "${REGISTER}"
    if ! aws iam get-instance-profile --instance-profile-name "$1" >/dev/null 2>&1; then
        aws iam create-instance-profile --path /aws-pcs/ --instance-profile-name "$1" --tags "$(tag_list pcs)" >/dev/null
        aws iam add-role-to-instance-profile --instance-profile-name "$1" --role-name "$1"
    fi
}
ROLE_LOGIN="${PCS_CLUSTER}-login"
ROLE_COMPUTE="${PCS_CLUSTER}-compute"
make_role "${ROLE_LOGIN}"
save PCS_ROLE_LOGIN "${ROLE_LOGIN}"
make_role "${ROLE_COMPUTE}"
save PCS_ROLE_COMPUTE "${ROLE_COMPUTE}"
PROFILE_LOGIN="$(aws iam get-instance-profile --instance-profile-name "${ROLE_LOGIN}" --query InstanceProfile.Arn --output text)"
PROFILE_COMPUTE="$(aws iam get-instance-profile --instance-profile-name "${ROLE_COMPUTE}" --query InstanceProfile.Arn --output text)"

# ─── Cluster ─────────────────────────────────────────────────────────────
CLUSTER_ID="$(aws pcs list-clusters --query "clusters[?name=='${PCS_CLUSTER}'].id | [0]" --output text)"
if [[ "${CLUSTER_ID}" == None ]]; then
    echo "Creating PCS cluster ${PCS_CLUSTER} (Slurm ${SLURM_VERSION})..."
    CLUSTER_ID="$(aws pcs create-cluster --cluster-name "${PCS_CLUSTER}" \
        --scheduler "type=SLURM,version=${SLURM_VERSION}" --size SMALL \
        --networking "subnetIds=${SUBNET_ID},securityGroupIds=${PCS_SG}" \
        --slurm-configuration '{"slurmCustomSettings":[{"parameterName":"MetricsType","parameterValue":"metrics/openmetrics"},{"parameterName":"CommunicationParameters","parameterValue":"enable_http"}]}' \
        --tags "$(tag_map pcs)" --query cluster.id --output text)"
fi
save PCS_CLUSTER_ID "${CLUSTER_ID}"
status=""
for _ in $(seq 1 90); do
    status="$(aws pcs get-cluster --cluster-identifier "${CLUSTER_ID}" --query cluster.status --output text)"
    [[ "${status}" == ACTIVE || "${status}" == *FAILED ]] && break
    echo "  cluster: ${status}"
    sleep 20
done
if [[ "${status}" != ACTIVE ]]; then
    echo "Cluster ${CLUSTER_ID}: ${status}" >&2
    exit 1
fi
echo "Cluster ${CLUSTER_ID} is ACTIVE"

# The login node's permissions from the README's AWS PCS quickstart, step 3.
LOGIN_POLICY="$(jq -n -c --arg ssm "arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter/pcs/${CLUSTER_ID}/grafana/*" \
    --arg via "ssm.${REGION}.amazonaws.com" '{Version: "2012-10-17", Statement: [
        {Effect: "Allow", Action: ["pcs:GetCluster", "pcs:ListComputeNodeGroups"], Resource: "*"},
        {Effect: "Allow", Action: ["ec2:DescribeInstances", "ec2:DescribeVolumes", "ec2:DescribeSpotPriceHistory"], Resource: "*"},
        {Effect: "Allow", Action: ["ssm:GetParameter", "ssm:PutParameter", "ssm:AddTagsToResource"], Resource: $ssm},
        {Effect: "Allow", Action: "kms:Decrypt", Resource: "*", Condition: {StringEquals: {"kms:ViaService": $via}}},
        {Effect: "Allow", Action: "pricing:GetProducts", Resource: "*"},
        {Effect: "Allow", Action: ["cloudwatch:ListMetrics", "cloudwatch:GetMetricStatistics"], Resource: "*"}]}')"
aws iam put-role-policy --role-name "${ROLE_LOGIN}" --policy-name monitoring --policy-document "${LOGIN_POLICY}"

# ─── Launch templates ────────────────────────────────────────────────────
# PCS needs MIME multipart user data. Every node mounts /home and runs the
# installer, as the README's user data does.
USER_DATA="$(base64 <<EOF | tr -d '\n'
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="==BOUNDARY=="

--==BOUNDARY==
Content-Type: text/x-shellscript; charset="us-ascii"

#!/bin/bash
set -euxo pipefail
# /home on ${EFS_ID}, with the AMI's home directories copied over.
command -v mount.efs >/dev/null || dnf -y install amazon-efs-utils
cp -a /home /tmp/home-copy
echo "${EFS_ID}:/ /home efs _netdev,tls 0 0" >> /etc/fstab
mount /home
cp -an /tmp/home-copy/. /home/
rm -rf /tmp/home-copy
tmp="\$(mktemp)" &&
    curl -fsSL ${POST_INSTALL_URL} -o "\${tmp}" &&
    bash "\${tmp}" ${REF} ${REPO}

--==BOUNDARY==--
EOF
)"
make_lt() { # make_lt NAME AMI INSTANCE-TAGS: create or update a launch template, print its ID
    local data id version
    data="$(jq -n -c --arg ami "$2" --arg sg "${PCS_SG}" --arg ud "${USER_DATA}" --arg key "${KEY_NAME}" \
        --argjson tags "$3" '{
            ImageId: $ami,
            SecurityGroupIds: [$sg],
            MetadataOptions: {HttpTokens: "required", HttpEndpoint: "enabled", InstanceMetadataTags: "enabled"},
            UserData: $ud,
            TagSpecifications: [{ResourceType: "instance", Tags: $tags}, {ResourceType: "volume", Tags: $tags}]
        } + (if $key == "" then {} else {KeyName: $key} end)')"
    id="$(aws ec2 describe-launch-templates --filters "Name=launch-template-name,Values=$1" \
        --query 'LaunchTemplates[0].LaunchTemplateId' --output text)"
    if [[ "${id}" == None ]]; then
        id="$(aws ec2 create-launch-template --launch-template-name "$1" --launch-template-data "${data}" \
            --tag-specifications "$(jq -n -c --argjson t "$(tag_list pcs)" '[{ResourceType: "launch-template", Tags: $t}]')" \
            --query LaunchTemplate.LaunchTemplateId --output text)"
    else
        version="$(aws ec2 create-launch-template-version --launch-template-id "${id}" --launch-template-data "${data}" \
            --query LaunchTemplateVersion.VersionNumber --output text)"
        aws ec2 modify-launch-template --launch-template-id "${id}" --default-version "${version}" >/dev/null
    fi
    echo "${id}"
}
# The README's tags: monitoring-role marks the login node, which also needs
# pcs-cluster-id.
LOGIN_TAGS="$(tag_list pcs | jq -c --arg id "${CLUSTER_ID}" \
    '. + [{Key: "monitoring-role", Value: "login"}, {Key: "pcs-cluster-id", Value: $id}]')"
COMPUTE_TAGS="$(tag_list pcs | jq -c '. + [{Key: "monitoring-role", Value: "compute"}]')"
LT_LOGIN="$(make_lt "${PCS_CLUSTER}-login" "${AMI_ID}" "${LOGIN_TAGS}")"
save PCS_LT_LOGIN "${LT_LOGIN}"
LT_CPU="$(make_lt "${PCS_CLUSTER}-cpu" "${AMI_ID}" "${COMPUTE_TAGS}")"
save PCS_LT_CPU "${LT_CPU}"
LT_GPU="$(make_lt "${PCS_CLUSTER}-gpu" "${GPU_AMI_ID}" "${COMPUTE_TAGS}")"
save PCS_LT_GPU "${LT_GPU}"

# ─── Node groups ─────────────────────────────────────────────────────────
make_node_group() { # make_node_group NAME LAUNCH-TEMPLATE INSTANCE-TYPE PROFILE MAX: print its ID
    local id version
    id="$(aws pcs list-compute-node-groups --cluster-identifier "${CLUSTER_ID}" \
        --query "computeNodeGroups[?name=='$1'].id | [0]" --output text)"
    if [[ "${id}" == None ]]; then
        version="$(aws ec2 describe-launch-templates --launch-template-ids "$2" \
            --query 'LaunchTemplates[0].DefaultVersionNumber' --output text)"
        id="$(aws pcs create-compute-node-group --cluster-identifier "${CLUSTER_ID}" \
            --compute-node-group-name "$1" --subnet-ids "${SUBNET_ID}" \
            --custom-launch-template "id=$2,version=${version}" \
            --iam-instance-profile-arn "$4" \
            --scaling-configuration "minInstanceCount=1,maxInstanceCount=$5" \
            --instance-configs "instanceType=$3" \
            --tags "$(tag_map pcs)" --query computeNodeGroup.id --output text)"
    fi
    echo "${id}"
}
NG_LOGIN="$(make_node_group login "${LT_LOGIN}" t3.medium "${PROFILE_LOGIN}" 1)"
save PCS_NG_LOGIN "${NG_LOGIN}"
NG_CPU="$(make_node_group cpu "${LT_CPU}" t3.medium "${PROFILE_COMPUTE}" 2)"
save PCS_NG_CPU "${NG_CPU}"
NG_GPU="$(make_node_group gpu "${LT_GPU}" g4dn.xlarge "${PROFILE_COMPUTE}" 1)"
save PCS_NG_GPU "${NG_GPU}"
for ng in "${NG_LOGIN}" "${NG_CPU}" "${NG_GPU}"; do
    status=""
    for _ in $(seq 1 60); do
        status="$(aws pcs get-compute-node-group --cluster-identifier "${CLUSTER_ID}" \
            --compute-node-group-identifier "${ng}" --query computeNodeGroup.status --output text)"
        [[ "${status}" == ACTIVE || "${status}" == *FAILED ]] && break
        echo "  node group ${ng}: ${status}"
        sleep 20
    done
    if [[ "${status}" != ACTIVE ]]; then
        echo "Node group ${ng}: ${status}" >&2
        exit 1
    fi
done
echo "Node groups are ACTIVE"

# ─── Queues ──────────────────────────────────────────────────────────────
make_queue() { # make_queue NAME NODE-GROUP
    if [[ "$(aws pcs list-queues --cluster-identifier "${CLUSTER_ID}" \
        --query "queues[?name=='$1'].id | [0]" --output text)" == None ]]; then
        aws pcs create-queue --cluster-identifier "${CLUSTER_ID}" --queue-name "$1" \
            --compute-node-group-configurations "computeNodeGroupId=$2" --tags "$(tag_map pcs)" >/dev/null
        echo "Created queue $1"
    fi
}
make_queue cpu "${NG_CPU}"
make_queue gpu "${NG_GPU}"

echo
echo "PCS cluster ${CLUSTER_ID} created. Its nodes boot and install the"
echo "monitoring stack over the next 5-10 minutes; then run connect.sh."
