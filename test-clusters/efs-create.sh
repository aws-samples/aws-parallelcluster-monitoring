#!/usr/bin/env bash
# Create one EFS used by both test clusters as /home.
# Idempotent — re-running detects the existing EFS by tag and reuses it.
set -euo pipefail

REGION="us-east-2"
VPC="vpc-0d442fab9e8cb8611"
PRIVATE_SUBNET="subnet-00643c21d668273ca"  # us-east-2b
TAG_KEY="aws-parallelcluster-monitoring-test"
TAG_VALUE="shared-home"

# ─── Create / discover EFS ────────────────────────────────────────────
existing=$(aws efs describe-file-systems --region "$REGION" \
    --query "FileSystems[?Tags[?Key=='$TAG_KEY' && Value=='$TAG_VALUE']].FileSystemId" \
    --output text 2>/dev/null || true)

if [[ -n "$existing" ]]; then
    echo "Reusing existing EFS: $existing"
    EFS_ID="$existing"
else
    echo "Creating new EFS..."
    EFS_ID=$(aws efs create-file-system --region "$REGION" \
        --creation-token "monitoring-test-home-$(date +%s)" \
        --performance-mode generalPurpose \
        --throughput-mode bursting \
        --tags "Key=$TAG_KEY,Value=$TAG_VALUE" "Key=Name,Value=monitoring-test-home" \
        --query 'FileSystemId' --output text)
    echo "Created EFS: $EFS_ID"

    # Wait for available
    for _ in $(seq 1 30); do
        state=$(aws efs describe-file-systems --region "$REGION" \
            --file-system-id "$EFS_ID" --query 'FileSystems[0].LifeCycleState' --output text)
        [[ "$state" == "available" ]] && break
        echo "EFS state: $state, waiting..."
        sleep 5
    done
fi

# ─── Security group ────────────────────────────────────────────────────
SG_NAME="monitoring-test-efs"
SG_ID=$(aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC" "Name=group-name,Values=$SG_NAME" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")

if [[ "$SG_ID" == "None" || -z "$SG_ID" ]]; then
    SG_ID=$(aws ec2 create-security-group --region "$REGION" \
        --group-name "$SG_NAME" --description "EFS access for monitoring test clusters" \
        --vpc-id "$VPC" \
        --tag-specifications "ResourceType=security-group,Tags=[{Key=$TAG_KEY,Value=$TAG_VALUE}]" \
        --query 'GroupId' --output text)
    aws ec2 authorize-security-group-ingress --region "$REGION" \
        --group-id "$SG_ID" --protocol tcp --port 2049 --cidr 10.3.0.0/16 >/dev/null
    echo "Created SG $SG_ID, allowing NFS from 10.3.0.0/16"
else
    echo "Reusing SG: $SG_ID"
fi

# ─── Mount target in private subnet ───────────────────────────────────
mt_existing=$(aws efs describe-mount-targets --region "$REGION" \
    --file-system-id "$EFS_ID" --query 'MountTargets[?SubnetId==`'$PRIVATE_SUBNET'`].MountTargetId' \
    --output text 2>/dev/null || true)

if [[ -z "$mt_existing" ]]; then
    aws efs create-mount-target --region "$REGION" \
        --file-system-id "$EFS_ID" --subnet-id "$PRIVATE_SUBNET" \
        --security-groups "$SG_ID" >/dev/null
    echo "Mount target created in $PRIVATE_SUBNET"
    sleep 5
else
    echo "Reusing mount target: $mt_existing"
fi

# ─── Output the values the cluster scripts need ───────────────────────
mkdir -p .state
{
    echo "EFS_ID=$EFS_ID"
    echo "EFS_SG_ID=$SG_ID"
    echo "EFS_DNS=${EFS_ID}.efs.${REGION}.amazonaws.com"
} > .state/efs.env
echo
echo "Wrote .state/efs.env:"
cat .state/efs.env
