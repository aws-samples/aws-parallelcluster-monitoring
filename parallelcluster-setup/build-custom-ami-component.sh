#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# EC2 Image Builder component for aws-parallelcluster-monitoring.
#
# Reference this script as a `script` component from a ParallelCluster
# build-image configuration file (see docs/custom-ami.md). It runs ONCE, at
# AMI build time, and pre-installs everything the monitoring stack would
# otherwise install on every compute node boot:
#   - Docker + the Docker Compose v2 plugin
#   - the node-exporter container image
#   - (GPU AMIs only) the NVIDIA Container Toolkit and the dcgm-exporter image
#
# The goal is to move the slow, network-bound work out of the compute-node
# boot path and into the AMI, so at scale-up the node only starts already
# present containers from already cached images.
#
# It deliberately REUSES the repo's own installer (installer/os/*.sh and the
# pinned image tags in compose/*.yml) so the AMI never drifts from what the
# boot-time installer expects. Bump versions in those files, not here.
#
# Optional environment variables:
#   MONITORING_REF        Git tag/branch to build from (default: latest release)
#   MONITORING_REPO_SLUG  GitHub owner/repo (default: aws-samples/aws-parallelcluster-monitoring)
#   DCGM_EXPORTER_IMAGE   Override the dcgm-exporter image to pre-pull (GPU only)

set -euo pipefail

REF="${MONITORING_REF:-latest}"
REPO_SLUG="${MONITORING_REPO_SLUG:-aws-samples/aws-parallelcluster-monitoring}"
MONITORING_DIR_NAME="aws-parallelcluster-monitoring"
MONITORING_HOME="/opt/${MONITORING_DIR_NAME}"
TARBALL="/tmp/${MONITORING_DIR_NAME}-ami.tar.gz"

# Resolve "latest" to the newest release tag (same logic as post-install.sh).
if [[ "${REF}" == "latest" ]]; then
    RESOLVED=$(curl -fsSL "https://api.github.com/repos/${REPO_SLUG}/releases/latest" 2>/dev/null \
        | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4)
    if [[ -n "${RESOLVED}" ]]; then
        echo "[build-ami] Resolved 'latest' -> ${RESOLVED}"
        REF="${RESOLVED}"
    else
        echo "[build-ami] WARN: could not resolve 'latest', falling back to main" >&2
        REF="main"
    fi
fi

# Fetch the monitoring tree (tag first, then branch).
mkdir -p "${MONITORING_HOME}"
if curl -fsSL "https://github.com/${REPO_SLUG}/archive/refs/tags/${REF}.tar.gz" -o "${TARBALL}" 2>/dev/null; then
    echo "[build-ami] fetched tag ${REF}"
elif curl -fsSL "https://github.com/${REPO_SLUG}/archive/refs/heads/${REF}.tar.gz" -o "${TARBALL}" 2>/dev/null; then
    echo "[build-ami] fetched branch ${REF}"
else
    echo "[build-ami] ERROR: could not fetch ${REF} from ${REPO_SLUG}" >&2
    exit 1
fi
tar xzf "${TARBALL}" -C "${MONITORING_HOME}" --strip-components 1
rm -f "${TARBALL}"

INSTALLER_DIR="${MONITORING_HOME}/installer"

# The OS install scripts add the build user to the docker group; pick a sane
# default present on ParallelCluster base AMIs.
if [[ -z "${PLATFORM_USER:-}" ]]; then
    for u in ec2-user ubuntu; do
        if id "${u}" >/dev/null 2>&1; then PLATFORM_USER="${u}"; break; fi
    done
fi
export PLATFORM_USER="${PLATFORM_USER:-root}"

# Reuse the repo's shared helpers (log/die/detect_os/pick_os_script/...).
# shellcheck source=/dev/null
. "${INSTALLER_DIR}/common.sh"

# 1. Install Docker + the Compose v2 plugin using the repo's per-OS script.
detect_os
os_script="$(pick_os_script "${INSTALLER_DIR}/os")"
[[ -n "${os_script}" && -r "${os_script}" ]] \
    || die "Unsupported OS for AMI build: ${OS_ID} ${OS_VERSION_ID}"
log "Installing docker + compose plugin via ${os_script}"
# shellcheck source=/dev/null
. "${os_script}"
verify_docker

# 2. Pre-pull the compute-node image(s). `docker compose pull` reads the pinned
#    tags straight from the compose files, so this stays in sync automatically.
if has_nvidia_gpu; then
    log "NVIDIA GPU detected - installing NVIDIA Container Toolkit"
    # Mirrors installer/install.sh's compute branch (replaces nvidia-docker2).
    case "${OS_ID}" in
        amzn|rhel|rocky|almalinux|centos)
            curl -fsSL https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
                -o /etc/yum.repos.d/nvidia-container-toolkit.repo
            (dnf -y install nvidia-container-toolkit 2>/dev/null) \
                || yum -y install nvidia-container-toolkit
            ;;
        ubuntu|debian)
            curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
                -o /usr/share/keyrings/nvidia-container-toolkit-keyring.asc
            curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
                | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.asc] https://#g' \
                > /etc/apt/sources.list.d/nvidia-container-toolkit.list
            apt-get update
            DEBIAN_FRONTEND=noninteractive apt-get install -y nvidia-container-toolkit
            ;;
        *) warn "No NVIDIA toolkit install path for OS '${OS_ID}', skipping" ;;
    esac
    # NOTE: we deliberately do NOT run `nvidia-ctk runtime configure` or restart
    # Docker here. Configuring the nvidia runtime and restarting the daemon on
    # the build instance — where the GPU driver may not be active yet — can
    # leave Docker unable to come back up, which fails the whole build. Baking
    # the toolkit package is enough; installer/install.sh configures the runtime
    # and restarts Docker on the real GPU compute node at boot. Pulling images
    # does not require the nvidia runtime.

    log "Pre-pulling GPU compute images (node-exporter + dcgm-exporter)"
    export DCGM_EXPORTER_IMAGE="${DCGM_EXPORTER_IMAGE:-}"
    # compute.gpu.yml carries a __MONITORING_HOME__ bind-mount placeholder that
    # installer/install.sh substitutes at node boot. Left unsubstituted, Docker
    # Compose reads it as an undefined named volume and refuses to parse the
    # project, so `pull` fails before touching a registry. Substitute it here
    # (harmless: the node re-fetches and re-substitutes at boot).
    sed -i "s|__MONITORING_HOME__|${MONITORING_HOME}|g" "${MONITORING_HOME}/compose/compute.gpu.yml"
    docker compose -f "${MONITORING_HOME}/compose/compute.gpu.yml" pull 2>&1 \
        || die "docker compose pull failed for compose/compute.gpu.yml"
else
    log "No NVIDIA GPU detected - pre-pulling CPU compute image (node-exporter)"
    docker compose -f "${MONITORING_HOME}/compose/compute.yml" pull 2>&1 \
        || die "docker compose pull failed for compose/compute.yml"
fi

log "Custom AMI pre-installation complete"
