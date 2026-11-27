#!/usr/bin/env bash
# Delete everything that pc-create.sh, efs-create.sh and pcs-create.sh
# created, in reverse dependency order: the ParallelCluster cluster (its
# deletion runs while the AWS PCS resources are deleted), the AWS PCS
# resources, then the EFS file system. Blocks until they're gone.
#
# Safe to re-run: it skips what is already gone. Uses the pcluster CLI on
# PATH, or $PCLUSTER, of the cluster's version.
set -uo pipefail
# shellcheck source=test-clusters/settings.sh
. "$(dirname "$0")/settings.sh"
PCLUSTER="${PCLUSTER:-pcluster}"

wait_until() { # wait_until TRIES SECONDS DESCRIPTION COMMAND...: until COMMAND succeeds
    local tries="$1" seconds="$2" what="$3" _
    shift 3
    for _ in $(seq 1 "${tries}"); do
        "$@" && return 0
        echo "    waiting for ${what}..."
        sleep "${seconds}"
    done
    echo "WARNING: timed out waiting for ${what}" >&2
    return 1
}
delete_sg() { aws ec2 delete-security-group --group-id "$1" 2>/dev/null; }

# ─── ParallelCluster ─────────────────────────────────────────────────────
echo "[1/3] ParallelCluster ${PC_CLUSTER}"
pc_exists() { "${PCLUSTER}" describe-cluster -n "${PC_CLUSTER}" --region "${REGION}" >/dev/null 2>&1; }
pc_gone() { ! pc_exists; }
if ! command -v "${PCLUSTER}" >/dev/null; then
    echo "  WARNING: no pcluster CLI; skipping ParallelCluster" >&2
elif pc_exists; then
    "${PCLUSTER}" delete-cluster -n "${PC_CLUSTER}" --region "${REGION}" >/dev/null && echo "  deleting"
else
    echo "  not found"
fi

# ─── AWS PCS ─────────────────────────────────────────────────────────────
echo "[2/3] AWS PCS ${PCS_CLUSTER}"
if [[ -f "${STATE}/pcs.env" ]]; then
    # shellcheck source=/dev/null
    . "${STATE}/pcs.env"
    if [[ -f "${STATE}/efs.env" ]]; then
        # shellcheck source=/dev/null
        . "${STATE}/efs.env"
    fi
    id="${PCS_CLUSTER_ID:-}"
    queues() { aws pcs list-queues --cluster-identifier "${id}" --query 'queues[].id' --output text; }
    node_groups() {
        aws pcs list-compute-node-groups --cluster-identifier "${id}" --query 'computeNodeGroups[].id' --output text
    }
    no_queues() { [[ -z "$(queues 2>/dev/null)" ]]; }
    no_node_groups() { [[ -z "$(node_groups 2>/dev/null)" ]]; }
    cluster_gone() { ! aws pcs get-cluster --cluster-identifier "${id}" >/dev/null 2>&1; }
    if [[ -n "${id}" ]] && ! cluster_gone; then
        for q in $(queues); do
            echo "  deleting queue ${q}"
            aws pcs delete-queue --cluster-identifier "${id}" --queue-identifier "${q}" >/dev/null
        done
        wait_until 30 10 "the queues" no_queues
        for ng in $(node_groups); do
            echo "  deleting node group ${ng}"
            aws pcs delete-compute-node-group --cluster-identifier "${id}" --compute-node-group-identifier "${ng}" >/dev/null
        done
        wait_until 60 15 "the node groups" no_node_groups
        echo "  deleting cluster ${id}"
        aws pcs delete-cluster --cluster-identifier "${id}" >/dev/null
        wait_until 60 15 "cluster ${id}" cluster_gone
    fi
    for lt in "${PCS_LT_LOGIN:-}" "${PCS_LT_CPU:-}" "${PCS_LT_GPU:-}"; do
        [[ -n "${lt}" ]] || continue
        aws ec2 delete-launch-template --launch-template-id "${lt}" >/dev/null 2>&1 && echo "  deleted launch template ${lt}"
    done
    for role in "${PCS_ROLE_LOGIN:-}" "${PCS_ROLE_COMPUTE:-}"; do
        [[ -n "${role}" ]] || continue
        aws iam remove-role-from-instance-profile --instance-profile-name "${role}" --role-name "${role}" 2>/dev/null
        aws iam delete-instance-profile --instance-profile-name "${role}" 2>/dev/null
        for arn in $(aws iam list-attached-role-policies --role-name "${role}" --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
            aws iam detach-role-policy --role-name "${role}" --policy-arn "${arn}"
        done
        for policy in $(aws iam list-role-policies --role-name "${role}" --query 'PolicyNames[]' --output text 2>/dev/null); do
            aws iam delete-role-policy --role-name "${role}" --policy-name "${policy}"
        done
        aws iam delete-role --role-name "${role}" 2>/dev/null && echo "  deleted role and instance profile ${role}"
    done
    if [[ -n "${PCS_SG:-}" ]]; then
        if [[ -n "${EFS_SG_ID:-}" ]]; then
            aws ec2 revoke-security-group-ingress --group-id "${EFS_SG_ID}" --protocol tcp --port 2049 \
                --source-group "${PCS_SG}" >/dev/null 2>&1
        fi
        # The cluster's network interfaces may take a few minutes to go.
        wait_until 20 15 "security group ${PCS_SG} to be released" delete_sg "${PCS_SG}" \
            && echo "  deleted security group ${PCS_SG}"
    fi
    rm -f "${STATE}/pcs.env"
else
    echo "  no .state/pcs.env"
fi

# ─── EFS, once the ParallelCluster cluster is gone ───────────────────────
if command -v "${PCLUSTER}" >/dev/null; then
    wait_until 60 30 "ParallelCluster ${PC_CLUSTER} to be deleted" pc_gone
fi
echo "[3/3] EFS"
if [[ -f "${STATE}/efs.env" ]]; then
    # shellcheck source=/dev/null
    . "${STATE}/efs.env"
    mount_targets() { aws efs describe-mount-targets --file-system-id "${EFS_ID}" --query 'MountTargets[].MountTargetId' --output text 2>/dev/null; }
    no_mount_targets() { [[ -z "$(mount_targets)" ]]; }
    for mt in $(mount_targets); do
        echo "  deleting mount target ${mt}"
        aws efs delete-mount-target --mount-target-id "${mt}"
    done
    wait_until 30 10 "the mount targets" no_mount_targets
    aws efs delete-file-system --file-system-id "${EFS_ID}" 2>/dev/null && echo "  deleted EFS ${EFS_ID}"
    wait_until 20 15 "security group ${EFS_SG_ID} to be released" delete_sg "${EFS_SG_ID}" \
        && echo "  deleted security group ${EFS_SG_ID}"
    rm -f "${STATE}/efs.env"
else
    echo "  no .state/efs.env"
fi

rm -f "${STATE}/pc-cluster.yaml"
rmdir "${STATE}" 2>/dev/null
echo
echo "Teardown complete."
