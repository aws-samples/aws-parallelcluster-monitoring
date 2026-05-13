#!/usr/bin/env bash
# Create the ParallelCluster test cluster.
set -euo pipefail

REGION="us-east-2"
CLUSTER="monitoring-test-pc"

# pcluster 3.15.0 ships incompatible code on Python 3.12+ (uses
# asyncio.get_event_loop() which was removed). Use a 3.11 venv if
# available, fall back to system pcluster otherwise.
if [[ -x /tmp/pcluster-venv/bin/pcluster ]]; then
    PCLUSTER=/tmp/pcluster-venv/bin/pcluster
else
    PCLUSTER="$(command -v pcluster)"
fi
echo "Using pcluster: $PCLUSTER"

mkdir -p .state

# pc-cluster.yaml is used as-is (no EFS substitution; ParallelCluster
# manages /home itself via HeadNode.SharedStorageType=efs)
CONFIG="pc-cluster.yaml"

# Existing?
existing=$("$PCLUSTER" list-clusters --region "$REGION" --query "clusters[?clusterName=='$CLUSTER'].clusterStatus | [0]" 2>/dev/null | tr -d '"' | grep -v '^null$' || echo "")
if [[ -n "$existing" && "$existing" != "None" && "$existing" != "null" && "$existing" != "" ]]; then
    echo "Cluster $CLUSTER already exists (status: $existing)"
    echo "Delete first with: $PCLUSTER delete-cluster -n $CLUSTER --region $REGION"
    exit 0
fi

echo "Creating cluster $CLUSTER (this takes 15-25 min)..."
"$PCLUSTER" create-cluster \
    --cluster-name "$CLUSTER" \
    --cluster-configuration "$CONFIG" \
    --region "$REGION"

echo
echo "Waiting for cluster to become ready..."
while true; do
    status=$("$PCLUSTER" describe-cluster -n "$CLUSTER" --region "$REGION" \
        --query 'clusterStatus' 2>/dev/null | tr -d '"' || echo "")
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
HEAD_ID=$("$PCLUSTER" describe-cluster-instances -n "$CLUSTER" --region "$REGION" \
    --query 'instances[?nodeType==`HeadNode`] | [0].instanceId' 2>/dev/null | tr -d '"')
echo "PC_CLUSTER=$CLUSTER" > .state/pc.env
echo "PC_HEAD_ID=$HEAD_ID" >> .state/pc.env
echo
echo "PC head node: $HEAD_ID"
cat .state/pc.env
