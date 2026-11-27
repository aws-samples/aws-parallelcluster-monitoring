#!/usr/bin/env bash
# Create the ParallelCluster test cluster, ${PREFIX}-pc, from the
# configuration this script writes to .state/pc-cluster.yaml.
#
# Usage: pc-create.sh [--dryrun]
#   --dryrun  Write the configuration and validate it with
#             `pcluster create-cluster --dryrun true`. Creates nothing.
#
# Runs the pcluster CLI on PATH, or $PCLUSTER. Its version is the version of
# the cluster it creates.
set -euo pipefail
# shellcheck source=test-clusters/settings.sh
. "$(dirname "$0")/settings.sh"

DRYRUN=false
case "${1:-}" in
    "") ;;
    --dryrun) DRYRUN=true ;;
    *) echo "Usage: $0 [--dryrun]" >&2; exit 1 ;;
esac
require SUBNET_ID "the ID of the subnet for the cluster's nodes"
PCLUSTER="${PCLUSTER:-pcluster}"
echo "Using pcluster $("${PCLUSTER}" version | jq -r .version) ($(command -v "${PCLUSTER}"))"

# Optional parts of the configuration, each starting on a new line.
SSH=""
if [[ -n "${KEY_NAME}" ]]; then
    SSH="
  Ssh:
    KeyName: ${KEY_NAME}"
fi
SHARED_STORAGE=""
if [[ -n "${FSX_ID}" ]]; then
    SHARED_STORAGE="
SharedStorage:
  - Name: fsx-shared
    StorageType: FsxLustre
    MountDir: /shared
    FsxLustreSettings:
      FileSystemId: ${FSX_ID}"
fi
TAGS_YAML="$(tag_list pc | jq -r '.[] | "  - Key: \(.Key | tojson)\n    Value: \(.Value | tojson)"')"

mkdir -p "${STATE}"
CONFIG="${STATE}/pc-cluster.yaml"
cat > "${CONFIG}" <<EOF
# Written by test-clusters/pc-create.sh: the ParallelCluster test cluster
# ${PC_CLUSTER}. A head node and two always-on compute nodes, one CPU and
# one GPU, install the monitoring stack from ${REPO} at ${REF}.
# /home is on an EFS file system that ParallelCluster creates and deletes.
Region: ${REGION}
Image:
  Os: alinux2023
HeadNode:
  InstanceType: t3.medium
  Networking:
    SubnetId: ${SUBNET_ID}${SSH}
  LocalStorage:
    RootVolume:
      VolumeType: gp3
      Size: 50
  SharedStorageType: Efs
  CustomActions:
    OnNodeConfigured:
      Script: ${POST_INSTALL_URL}
      Args:
        - ${REF}
        - ${REPO}
  Iam:
    AdditionalIamPolicies:
      # The README's quick-start policies, on the head node only.
      - Policy: arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
      - Policy: arn:aws:iam::aws:policy/CloudWatchFullAccess
      - Policy: arn:aws:iam::aws:policy/AWSPriceListServiceFullAccess
      - Policy: arn:aws:iam::aws:policy/AmazonSSMFullAccess
      - Policy: arn:aws:iam::aws:policy/AWSCloudFormationReadOnlyAccess
      - Policy: arn:aws:iam::aws:policy/AmazonEC2ReadOnlyAccess
Scheduling:
  Scheduler: slurm
  SlurmQueues:
    - Name: cpu
      ComputeResources:
        - Name: t3medium
          Instances:
            - InstanceType: t3.medium
          MinCount: 1
          MaxCount: 2
      Networking:
        SubnetIds:
          - ${SUBNET_ID}
      ComputeSettings:
        LocalStorage:
          RootVolume:
            VolumeType: gp3
      CustomActions:
        OnNodeConfigured:
          Script: ${POST_INSTALL_URL}
          Args:
            - ${REF}
            - ${REPO}
      Iam:
        AdditionalIamPolicies:
          - Policy: arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
    - Name: gpu
      ComputeResources:
        - Name: g4dnxlarge
          Instances:
            - InstanceType: g4dn.xlarge
          MinCount: 1
          MaxCount: 1
      Networking:
        SubnetIds:
          - ${SUBNET_ID}
      ComputeSettings:
        LocalStorage:
          RootVolume:
            VolumeType: gp3
      CustomActions:
        OnNodeConfigured:
          Script: ${POST_INSTALL_URL}
          Args:
            - ${REF}
            - ${REPO}
      Iam:
        AdditionalIamPolicies:
          - Policy: arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore${SHARED_STORAGE}
Tags:
${TAGS_YAML}
Monitoring:
  Logs:
    CloudWatch:
      Enabled: true
      RetentionInDays: 7
EOF
echo "Wrote ${CONFIG}"

if [[ "${DRYRUN}" == true ]]; then
    # pcluster exits 1 even when the dry run succeeds; its message tells.
    out="$("${PCLUSTER}" create-cluster --cluster-name "${PC_CLUSTER}" --cluster-configuration "${CONFIG}" \
        --region "${REGION}" --dryrun true 2>&1)" || true
    printf '%s\n' "${out}"
    if jq -e '.message == "Request would have succeeded, but DryRun flag is set."' <<<"${out}" >/dev/null 2>&1; then
        echo "Dry run passed"
        exit 0
    fi
    echo "Dry run failed" >&2
    exit 1
fi

if status="$("${PCLUSTER}" describe-cluster -n "${PC_CLUSTER}" --region "${REGION}" --query clusterStatus 2>/dev/null)"; then
    echo "Cluster ${PC_CLUSTER} already exists (${status//\"/}). Delete it first with teardown.sh." >&2
    exit 1
fi

echo "Creating cluster ${PC_CLUSTER} (15-25 minutes)..."
"${PCLUSTER}" create-cluster --cluster-name "${PC_CLUSTER}" --cluster-configuration "${CONFIG}" --region "${REGION}"
while true; do
    status="$("${PCLUSTER}" describe-cluster -n "${PC_CLUSTER}" --region "${REGION}" --query clusterStatus | tr -d '"')"
    case "${status}" in
        CREATE_COMPLETE) echo "Cluster ready"; break ;;
        CREATE_IN_PROGRESS) echo "  status: ${status}"; sleep 30 ;;
        *) echo "Cluster ${PC_CLUSTER} failed: ${status}" >&2; exit 1 ;;
    esac
done

# For connect.sh.
HEAD_ID="$("${PCLUSTER}" describe-cluster-instances -n "${PC_CLUSTER}" --region "${REGION}" \
    --node-type HeadNode --query 'instances[0].instanceId' | tr -d '"')"
printf 'PC_HEAD_ID=%s\n' "${HEAD_ID}" > "${STATE}/pc.env"
echo "Head node: ${HEAD_ID}"
