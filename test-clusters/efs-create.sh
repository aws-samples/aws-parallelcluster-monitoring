#!/usr/bin/env bash
# Create the EFS file system that the AWS PCS test cluster mounts at /home,
# with a security group and a mount target in SUBNET_ID. pcs-create.sh lets
# the cluster's nodes reach it. ParallelCluster creates its own.
#
# Re-running it reuses the file system, which it finds by its tag.
set -euo pipefail
# shellcheck source=test-clusters/settings.sh
. "$(dirname "$0")/settings.sh"
require SUBNET_ID "the ID of the subnet for the cluster's nodes"

VPC_ID="$(aws ec2 describe-subnets --subnet-ids "${SUBNET_ID}" --query 'Subnets[0].VpcId' --output text)"
NAME="${PREFIX}-home"

EFS_ID="$(aws efs describe-file-systems \
    --query "FileSystems[?Tags[?Key=='${TAG_KEY}' && Value=='${NAME}']].FileSystemId | [0]" --output text)"
if [[ "${EFS_ID}" != None ]]; then
    echo "Reusing EFS ${EFS_ID}"
else
    EFS_ID="$(aws efs create-file-system --creation-token "${NAME}-$(date +%s)" \
        --performance-mode generalPurpose --throughput-mode bursting --encrypted \
        --tags "$(tag_list "${NAME}" | jq -c --arg name "${NAME}" '. + [{Key: "Name", Value: $name}]')" \
        --query FileSystemId --output text)"
    echo "Created EFS ${EFS_ID}"
    for _ in $(seq 1 30); do
        state="$(aws efs describe-file-systems --file-system-id "${EFS_ID}" \
            --query 'FileSystems[0].LifeCycleState' --output text)"
        [[ "${state}" == available ]] && break
        echo "  EFS ${state}, waiting..."
        sleep 5
    done
fi

# The NFS rule from the cluster's nodes is added by pcs-create.sh.
EFS_SG_ID="$(aws ec2 describe-security-groups \
    --filters "Name=vpc-id,Values=${VPC_ID}" "Name=group-name,Values=${NAME}" \
    --query 'SecurityGroups[0].GroupId' --output text)"
if [[ "${EFS_SG_ID}" == None ]]; then
    EFS_SG_ID="$(aws ec2 create-security-group --group-name "${NAME}" --vpc-id "${VPC_ID}" \
        --description "EFS mount target of ${PCS_CLUSTER}" \
        --tag-specifications "$(jq -n -c --argjson t "$(tag_list "${NAME}")" '[{ResourceType: "security-group", Tags: $t}]')" \
        --query GroupId --output text)"
    echo "Created security group ${EFS_SG_ID}"
else
    echo "Reusing security group ${EFS_SG_ID}"
fi

MT_ID="$(aws efs describe-mount-targets --file-system-id "${EFS_ID}" \
    --query "MountTargets[?SubnetId=='${SUBNET_ID}'].MountTargetId | [0]" --output text)"
if [[ "${MT_ID}" == None ]]; then
    aws efs create-mount-target --file-system-id "${EFS_ID}" --subnet-id "${SUBNET_ID}" \
        --security-groups "${EFS_SG_ID}" >/dev/null
    echo "Created a mount target in ${SUBNET_ID}"
else
    echo "Reusing mount target ${MT_ID}"
fi

mkdir -p "${STATE}"
printf 'EFS_ID=%s\nEFS_SG_ID=%s\n' "${EFS_ID}" "${EFS_SG_ID}" > "${STATE}/efs.env"
echo "Wrote ${STATE}/efs.env"
