#!/bin/bash
# shellcheck disable=SC2154  # cfn_* / stack_name vars come from /etc/parallelcluster/cfnconfig
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# ParallelCluster OnNodeConfigured post-install entrypoint.
#
# Usage: post-install.sh [ref] [repo_slug]
#   ref        Git tag OR branch of the monitoring repo to install.
#              Default: v1.0
#   repo_slug  GitHub "owner/repo" to download from. Lets you test from a
#              fork without editing this script.
#              Default: aws-samples/aws-parallelcluster-monitoring
#
# Examples:
#   post-install.sh v1.0
#   post-install.sh v1.0-rc1 nicolaven/aws-parallelcluster-monitoring
#   post-install.sh phase1-modernization nicolaven/aws-parallelcluster-monitoring
#
set -euo pipefail

# shellcheck disable=SC1091
. /etc/parallelcluster/cfnconfig

REF="${1:-v1.0}"
REPO_SLUG="${2:-aws-samples/aws-parallelcluster-monitoring}"
MONITORING_DIR_NAME="aws-parallelcluster-monitoring"
MONITORING_HOME="/home/${cfn_cluster_user}/${MONITORING_DIR_NAME}"
LOG_FILE="/var/log/parallelcluster-monitoring-install.log"
TARBALL="/tmp/${MONITORING_DIR_NAME}.tar.gz"

# GitHub serves tarballs for both tags and branches via the same /archive
# endpoint: /archive/refs/tags/<tag>.tar.gz or /archive/refs/heads/<branch>.tar.gz.
# Try the tag path first; if it 404s, fall back to the branch path. This lets
# the same Args: - <ref> work whether <ref> is a tag or a branch.
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
    echo "ERROR: could not fetch ${REF} from ${REPO_SLUG} (tried tag and branch)" >&2
    return 1
}

mkdir -p "${MONITORING_HOME}"
fetch_tarball
tar xzf "${TARBALL}" -C "${MONITORING_HOME}" --strip-components 1
rm -f "${TARBALL}"

chown -R "${cfn_cluster_user}:${cfn_cluster_user}" "${MONITORING_HOME}"

# Hand off to the OS-aware installer.
# tee so output goes to BOTH the log file (persistent on disk) AND stdout
# (captured by cfn-init -> CloudWatch Logs, so it survives head-node
# teardown on failure).
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
