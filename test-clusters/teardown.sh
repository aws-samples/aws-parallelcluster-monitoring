#!/usr/bin/env bash
# Tear down everything created by efs-create.sh / pc-create.sh / pcs-create.sh.
# Reverse dependency order: PCS resources → PC cluster → EFS.
#
# Safe to re-run; skips anything already gone.
set -uo pipefail

REGION="us-east-2"

# ─── ParallelCluster ──────────────────────────────────────────────────
PC_CLUSTER="monitoring-test-pc"
echo "[1/3] Deleting ParallelCluster $PC_CLUSTER..."
pcluster delete-cluster -n "$PC_CLUSTER" --region "$REGION" 2>/dev/null || true
# Wait a bit before we delete EFS (PC's compute nodes may still hold mounts)
echo "  (giving pcluster CFN delete a head start...)"
sleep 30

# ─── PCS ──────────────────────────────────────────────────────────────
echo "[2/3] Deleting PCS resources..."
if [[ -f .state/pcs.env ]]; then
    # shellcheck disable=SC1091
    . .state/pcs.env

    # Terminate login instance
    if [[ -n "${PCS_LOGIN_ID:-}" ]]; then
        echo "  terminating login: $PCS_LOGIN_ID"
        aws ec2 terminate-instances --region "$REGION" --instance-ids "$PCS_LOGIN_ID" >/dev/null 2>&1 || true
    fi

    # Delete queues first
    if [[ -n "${PCS_CLUSTER_ID:-}" ]]; then
        for q in $(aws pcs list-queues --region "$REGION" --cluster-identifier "$PCS_CLUSTER_ID" --query 'queues[].id' --output text 2>/dev/null || true); do
            echo "  deleting queue $q"
            aws pcs delete-queue --region "$REGION" --cluster-identifier "$PCS_CLUSTER_ID" --queue-identifier "$q" >/dev/null 2>&1 || true
        done

        # Wait for queues gone before deleting node groups (dependency)
        for _ in $(seq 1 30); do
            n=$(aws pcs list-queues --region "$REGION" --cluster-identifier "$PCS_CLUSTER_ID" --query 'length(queues)' --output text 2>/dev/null || echo "0")
            [[ "$n" == "0" ]] && break
            echo "    waiting for $n queues to delete..."
            sleep 10
        done

        # Delete compute node groups
        for ng in $(aws pcs list-compute-node-groups --region "$REGION" --cluster-identifier "$PCS_CLUSTER_ID" --query 'computeNodeGroups[].id' --output text 2>/dev/null || true); do
            echo "  deleting compute node group $ng"
            aws pcs delete-compute-node-group --region "$REGION" --cluster-identifier "$PCS_CLUSTER_ID" --compute-node-group-identifier "$ng" >/dev/null 2>&1 || true
        done

        # Wait for node groups gone
        for _ in $(seq 1 60); do
            n=$(aws pcs list-compute-node-groups --region "$REGION" --cluster-identifier "$PCS_CLUSTER_ID" --query 'length(computeNodeGroups)' --output text 2>/dev/null || echo "0")
            [[ "$n" == "0" ]] && break
            echo "    waiting for $n node groups to delete..."
            sleep 15
        done

        # Delete cluster
        echo "  deleting PCS cluster $PCS_CLUSTER_ID"
        aws pcs delete-cluster --region "$REGION" --cluster-identifier "$PCS_CLUSTER_ID" >/dev/null 2>&1 || true
    fi

    # Delete launch templates
    for lt in "${PCS_LT_LOGIN:-}" "${PCS_LT_CPU:-}" "${PCS_LT_GPU:-}"; do
        [[ -z "$lt" ]] && continue
        echo "  deleting LT $lt"
        aws ec2 delete-launch-template --region "$REGION" --launch-template-id "$lt" >/dev/null 2>&1 || true
    done

    # Delete IAM role + instance profile
    if [[ -n "${PCS_INSTANCE_PROFILE:-}" ]]; then
        echo "  removing IAM role $PCS_INSTANCE_PROFILE"
        aws iam remove-role-from-instance-profile --instance-profile-name "$PCS_INSTANCE_PROFILE" --role-name "$PCS_INSTANCE_PROFILE" 2>/dev/null || true
        aws iam delete-instance-profile --instance-profile-name "$PCS_INSTANCE_PROFILE" 2>/dev/null || true
        for arn in $(aws iam list-attached-role-policies --role-name "$PCS_INSTANCE_PROFILE" --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null || true); do
            aws iam detach-role-policy --role-name "$PCS_INSTANCE_PROFILE" --policy-arn "$arn" 2>/dev/null || true
        done
        for p in $(aws iam list-role-policies --role-name "$PCS_INSTANCE_PROFILE" --query 'PolicyNames[]' --output text 2>/dev/null || true); do
            aws iam delete-role-policy --role-name "$PCS_INSTANCE_PROFILE" --policy-name "$p" 2>/dev/null || true
        done
        aws iam delete-role --role-name "$PCS_INSTANCE_PROFILE" 2>/dev/null || true
    fi

    # Delete PCS instance SG (after cluster gone)
    if [[ -n "${PCS_SG:-}" ]]; then
        for _ in $(seq 1 20); do
            aws ec2 delete-security-group --region "$REGION" --group-id "$PCS_SG" 2>/dev/null && break
            echo "    waiting for SG $PCS_SG to release..."
            sleep 15
        done
    fi
fi

# ─── Wait for PC cluster fully gone before EFS ────────────────────────
echo "  waiting for PC cluster $PC_CLUSTER deletion..."
for _ in $(seq 1 60); do
    s=$(pcluster describe-cluster -n "$PC_CLUSTER" --region "$REGION" --query 'clusterStatus' --output text 2>/dev/null || echo "GONE")
    case "$s" in
        GONE|"") break ;;
        DELETE_IN_PROGRESS) sleep 30 ;;
        DELETE_FAILED) echo "PC delete failed — investigate manually"; break ;;
        *) sleep 20 ;;
    esac
done

# ─── EFS ──────────────────────────────────────────────────────────────
echo "[3/3] Deleting EFS..."
if [[ -f .state/efs.env ]]; then
    # shellcheck disable=SC1091
    . .state/efs.env
    if [[ -n "${EFS_ID:-}" ]]; then
        for mt in $(aws efs describe-mount-targets --region "$REGION" --file-system-id "$EFS_ID" --query 'MountTargets[].MountTargetId' --output text 2>/dev/null || true); do
            echo "  deleting mount target $mt"
            aws efs delete-mount-target --region "$REGION" --mount-target-id "$mt" 2>/dev/null || true
        done
        # Wait for mount targets gone
        for _ in $(seq 1 30); do
            n=$(aws efs describe-mount-targets --region "$REGION" --file-system-id "$EFS_ID" --query 'length(MountTargets)' --output text 2>/dev/null || echo "0")
            [[ "$n" == "0" ]] && break
            sleep 10
        done
        aws efs delete-file-system --region "$REGION" --file-system-id "$EFS_ID" 2>/dev/null || true
        echo "  deleted EFS $EFS_ID"
    fi
    if [[ -n "${EFS_SG_ID:-}" ]]; then
        for _ in $(seq 1 20); do
            aws ec2 delete-security-group --region "$REGION" --group-id "$EFS_SG_ID" 2>/dev/null && break
            sleep 15
        done
        echo "  deleted EFS SG $EFS_SG_ID"
    fi
fi

# Clean up state dir
rm -rf .state

echo
echo "Teardown complete. The shared FSx Lustre and HPC-Prod-Network VPC are untouched."
