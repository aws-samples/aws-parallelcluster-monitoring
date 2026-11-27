#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Monitoring install entrypoint. Works with AWS ParallelCluster (via
# OnNodeConfigured CustomActions) and AWS PCS (via a node lifecycle action,
# or launch template user data).
#
# Usage: post-install.sh [--role login|compute] [ref] [repo_slug]
#   --role     AWS PCS only: the node's role, for node groups whose instances
#              have no monitoring-role tag in IMDS (node lifecycle actions,
#              where the node group has no launch template). login runs the
#              monitoring stack, compute the exporters. Also --role=<role>;
#              allowed anywhere among the arguments.
#              Default: the monitoring-role instance tag, else compute.
#   ref        Git tag, branch, or "latest" (resolves to newest release).
#              Default: latest
#   repo_slug  GitHub "owner/repo" to download from.
#              Default: aws-samples/aws-parallelcluster-monitoring
#
set -euo pipefail

# Separate --role from the positional arguments, and check it before
# anything is downloaded.
role_given=0
role=""
positional=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --role)
            role_given=1
            role="${2:-}"
            shift
            [[ $# -gt 0 ]] && shift
            ;;
        --role=*)
            role_given=1
            role="${1#--role=}"
            shift
            ;;
        *)
            positional+=("$1")
            shift
            ;;
    esac
done
if [[ "${role_given}" == 1 ]]; then
    case "${role}" in
        login|compute) export MONITORING_ROLE="${role}" ;;
        *)
            echo "ERROR: --role must be login or compute, not '${role}'" >&2
            echo "Usage: post-install.sh [--role login|compute] [ref] [repo_slug]" >&2
            exit 2
            ;;
    esac
fi

REF="${positional[0]:-latest}"
REPO_SLUG="${positional[1]:-aws-samples/aws-parallelcluster-monitoring}"
MONITORING_DIR_NAME="aws-parallelcluster-monitoring"
# Download into a private directory that is removed on exit. A fixed /tmp
# path could be pre-created by another local user, who could then block the
# install or replace the tarball before root extracts it.
TARBALL_DIR="$(mktemp -d)"
trap 'rm -rf "${TARBALL_DIR}"' EXIT
TARBALL="${TARBALL_DIR}/${MONITORING_DIR_NAME}.tar.gz"
LOG_FILE="/var/log/parallelcluster-monitoring-install.log"

# Resolve "latest" to the most recent git tag via the GitHub API.
if [[ "${REF}" == "latest" ]]; then
    RESOLVED=$(curl -fsSL "https://api.github.com/repos/${REPO_SLUG}/releases/latest" 2>/dev/null \
        | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4)
    if [[ -n "${RESOLVED}" ]]; then
        echo "Resolved 'latest' → ${RESOLVED}"
        REF="${RESOLVED}"
    else
        echo "WARN: could not resolve 'latest', falling back to main branch" >&2
        REF="main"
    fi
fi

# Platform detection
if [[ -r /etc/parallelcluster/cfnconfig ]]; then
    # shellcheck disable=SC1091
    . /etc/parallelcluster/cfnconfig
    # shellcheck disable=SC2154  # cfn_cluster_user is set by cfnconfig
    CLUSTER_USER="${cfn_cluster_user}"
else
    # PCS / other: Detect the cluster user. Ubuntu-based AMIs use 'ubuntu',
    # Amazon Linux and RHEL use 'ec2-user', and Rocky Linux AMIs use 'rocky'
    # (they have no ec2-user).
    CLUSTER_USER="ec2-user"
    if [[ -d /home/ubuntu ]] && id ubuntu >/dev/null 2>&1; then
        CLUSTER_USER="ubuntu"
    elif ! id ec2-user >/dev/null 2>&1 && id rocky >/dev/null 2>&1; then
        CLUSTER_USER="rocky"
    fi
fi
# Install the monitoring tree on NODE-LOCAL disk, NOT under /home.
# On AWS PCS, /home is a SHARED filesystem (FSx for OpenZFS over NFS) mounted on
# every node. Extracting/owning/sed-ing the tree there makes the login node and
# every compute node race on the same files at first boot, which intermittently
# fails with "error reading input file: Stale file handle" (one node's tar
# replaces an inode another node is mid-read). /opt is local to each node, so
# each install is independent.
MONITORING_HOME="/opt/${MONITORING_DIR_NAME}"

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
rm -rf "${TARBALL_DIR}"

chown -R "${CLUSTER_USER}:${CLUSTER_USER}" "${MONITORING_HOME}"

# Hand off to the platform-aware installer.
# The log holds the full shell trace, so make it root-only. tee keeps an
# existing file's mode, so create the file first; this also tightens a
# world-readable log left by an older version.
install -m 0600 /dev/null "${LOG_FILE}"
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
