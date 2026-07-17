#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Build a custom ParallelCluster compute-node AMI with the monitoring stack
# pre-baked (Docker + Compose + container images; NVIDIA toolkit + dcgm image
# for GPU). Resolves the official base AMI for your pcluster version + OS,
# fills in the build-image template, and runs `pcluster build-image`.
#
# Usage:
#   build-ami.sh [--type cpu|gpu] [--os <os>] [--region <region>]
#                [--image-id <name>] [--arch x86_64|arm64] [--wait]
#
# Defaults: --type cpu  --os alinux2023  --region ${AWS_DEFAULT_REGION:-us-east-1}
#           --arch x86_64  --image-id monitoring-compute-<type>-<os>
#
# Requires: pcluster CLI (same version as your cluster), python3, credentials.

set -euo pipefail

TYPE=cpu
OS=alinux2023
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
ARCH=x86_64
IMAGE_ID=""
WAIT=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --type)     TYPE="$2"; shift 2 ;;
        --os)       OS="$2"; shift 2 ;;
        --region)   REGION="$2"; shift 2 ;;
        --image-id) IMAGE_ID="$2"; shift 2 ;;
        --arch)     ARCH="$2"; shift 2 ;;
        --wait)     WAIT=1; shift ;;
        -h|--help)  grep '^#' "$0" | grep -vE 'bin/bash|Copyright|SPDX-' | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "${TYPE}" in
    cpu) TEMPLATE="${SCRIPT_DIR}/build-image-cpu.yaml" ;;
    gpu) TEMPLATE="${SCRIPT_DIR}/build-image-gpu.yaml" ;;
    *)   echo "--type must be 'cpu' or 'gpu'" >&2; exit 2 ;;
esac
[[ -r "${TEMPLATE}" ]] || { echo "template not found: ${TEMPLATE}" >&2; exit 1; }
command -v pcluster >/dev/null 2>&1 || { echo "pcluster CLI not found on PATH" >&2; exit 1; }
[[ -n "${IMAGE_ID}" ]] || IMAGE_ID="monitoring-compute-${TYPE}-${OS}"

echo "Resolving official ParallelCluster base AMI (os=${OS} arch=${ARCH} region=${REGION})..."
PARENT="$(pcluster list-official-images --os "${OS}" --region "${REGION}" 2>/dev/null \
    | python3 -c 'import json,sys
d=json.load(sys.stdin); arch=sys.argv[1]
ids=[i["amiId"] for i in d.get("images",[]) if i.get("architecture")==arch]
print(ids[0] if ids else "")' "${ARCH}")"
[[ -n "${PARENT}" ]] || { echo "could not resolve a base AMI for os=${OS} arch=${ARCH} in ${REGION}" >&2; exit 1; }
echo "Base AMI: ${PARENT}"

CONFIG="$(mktemp "${TMPDIR:-/tmp}/build-image-${TYPE}-XXXXXX.yaml")"
sed "s|__PARENT_IMAGE__|${PARENT}|g" "${TEMPLATE}" > "${CONFIG}"
echo "Generated build config: ${CONFIG}"
echo "Image id: ${IMAGE_ID}"

pcluster build-image --image-id "${IMAGE_ID}" --image-configuration "${CONFIG}" --region "${REGION}"

if [[ "${WAIT}" -eq 1 ]]; then
    echo "Waiting for the build to finish (EC2 Image Builder can take ~1h)..."
    while :; do
        S="$(pcluster describe-image --image-id "${IMAGE_ID}" --region "${REGION}" 2>/dev/null \
            | grep -o '"imageBuildStatus": *"[^"]*"' | cut -d'"' -f4)"
        echo "$(date +%H:%M:%S) ${S:-<pending>}"
        case "${S}" in
            BUILD_COMPLETE)
                pcluster describe-image --image-id "${IMAGE_ID}" --region "${REGION}" \
                    | grep -o '"amiId": *"[^"]*"'
                break ;;
            BUILD_FAILED) echo "build FAILED — see: pcluster get-image-log-events --image-id ${IMAGE_ID} --region ${REGION}" >&2; exit 1 ;;
        esac
        sleep 60
    done
fi
