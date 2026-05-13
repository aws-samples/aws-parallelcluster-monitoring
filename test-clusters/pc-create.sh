#!/usr/bin/env bash
# Create the ParallelCluster test cluster.
set -euo pipefail

REGION="us-east-2"
CLUSTER="monitoring-test-pc"

mkdir -p .state

# pc-cluster.yaml is used as-is (no EFS substitution; ParallelCluster
# manages /home itself via HeadNode.SharedStorageType=efs)
CONFIG="pc-cluster.yaml"

# Existing?
existing=$(pcluster list-clusters --region "$REGION" \
    --query "clusters[?clusterName=='$CLUSTER'].clusterStatus | [0]" \
    --output text 2>/dev/null || echo "")
if [[ -n "$existing" && "$existing" != "None" ]]; then
    echo "Cluster $CLUSTER already exists (status: $existing)"
    echo "Delete first with: pcluster delete-cluster -n $CLUSTER --region $REGION"
    exit 0
fi

echo "Creating cluster $CLUSTER (this takes 15-25 min)..."
pcluster create-cluster \
    --cluster-name "$CLUSTER" \
    --cluster-configuration "$CONFIG" \
    --region "$REGION"

echo
echo "Waiting for cluster to become ready..."
while true; do
    status=$(pcluster describe-cluster -n "$CLUSTER" --region "$REGION" \
        --query 'clusterStatus' --output text 2>/dev/null || echo "")
    case "$status" in
        CREATE_COMPLETE) echo "Cluster ready"; break ;;
        CREATE_IN_PROGRESS) echo "  status: $status"; sleep 30 ;;
        CREATE_FAILED|UPDATE_FAILED|*FAILED|"")
            echo "Cluster failed (status=$status)" >&2
            exit 1
            ;;
        *) echo "  status: $status"; sleep 30 ;;
    esac
done

# Capture key info for connect.sh
HEAD_ID=$(pcluster describe-cluster-instances -n "$CLUSTER" --region "$REGION" \
    --node-type HeadNode --query 'instances[0].instanceId' --output text)
echo "PC_CLUSTER=$CLUSTER" > .state/pc.env
echo "PC_HEAD_ID=$HEAD_ID" >> .state/pc.env
echo
echo "PC head node: $HEAD_ID"
cat .state/pc.env
