#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Monitoring install entrypoint. Works with both AWS ParallelCluster
# (via OnNodeConfigured CustomActions) and AWS PCS (via launch template
# user data).
#
# Usage: post-install.sh [ref] [repo_slug]
#   ref        Git tag OR branch of the monitoring repo to install.
#              Default: v2.1
#   repo_slug  GitHub "owner/repo" to download from.
#              Default: aws-samples/aws-parallelcluster-monitoring
#
set -euo pipefail

REF="${1:-v2.1}"
REPO_SLUG="${2:-aws-samples/aws-parallelcluster-monitoring}"
MONITORING_DIR_NAME="aws-parallelcluster-monitoring"
TARBALL="/tmp/${MONITORING_DIR_NAME}.tar.gz"
LOG_FILE="/var/log/parallelcluster-monitoring-install.log"

# Platform detection
if [[ -r /etc/parallelcluster/cfnconfig ]]; then
    # shellcheck disable=SC1091
    . /etc/parallelcluster/cfnconfig
    # shellcheck disable=SC2154  # cfn_cluster_user is set by cfnconfig
    CLUSTER_USER="${cfn_cluster_user}"
else
    # PCS / other: sample AMIs use ec2-user
    CLUSTER_USER="ec2-user"
fi
MONITORING_HOME="/home/${CLUSTER_USER}/${MONITORING_DIR_NAME}"

# Fetch the tarball. /archive supports both tag and branch paths.
fetch_tarball() {
    local tag_url="https://github.com/${REPO_SLUG}/archive/refs/tags/${REF}.tar.gz"
    local branch_url="https://github.com/${REPO_SLUG}/archive/refs/heads/${REF}.tar.gz"
    if curl -fsSL "${tag_url}" -o "${TARBALL}" 2>/dev/null; then
        echo "fetched tag: ${tag_url}"
        return 0
    fi
    if curl -fsSL "${branch_url}" -o "${TARBALL}" 2>/dev/null; then
        echo "fetched branch: ${branch_url}"
        return 0
    fi
    echo "ERROR: could not fetch ${REF} from ${REPO_SLUG}" >&2
    return 1
}

mkdir -p "${MONITORING_HOME}"
fetch_tarball
tar xzf "${TARBALL}" -C "${MONITORING_HOME}" --strip-components 1
rm -f "${TARBALL}"

chown -R "${CLUSTER_USER}:${CLUSTER_USER}" "${MONITORING_HOME}"

# Hand off to the platform-aware installer.
set +e
bash -x "${MONITORING_HOME}/installer/install.sh" 2>&1 | tee "${LOG_FILE}"
rc=${PIPESTATUS[0]}
set -e
if [[ ${rc} -ne 0 ]]; then
    echo "=============================================================" >&2
    echo "monitoring install FAILED with exit ${rc}" >&2
    echo "last 80 lines of ${LOG_FILE}:" >&2
    echo "=============================================================" >&2
    tail -n 80 "${LOG_FILE}" >&2 || true
fi
exit "${rc}"
