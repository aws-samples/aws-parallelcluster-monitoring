#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Shared helpers and pinned versions for the monitoring installer.
# Sourced by installer/install.sh and installer/os/*.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Pinned upstream versions.
# Bump these deliberately; do not use floating tags like "latest".
# ---------------------------------------------------------------------------
readonly SLURM_EXPORTER_VERSION="1.8.0"
readonly SLURM_EXPORTER_REPO="rivosinc/prometheus-slurm-exporter"

# Container image tags are pinned in the compose files themselves
# (compose/{head,compute,compute.gpu}.yml). The installer does not
# rewrite them — keeping them in compose YAML means `cat compose/*.yml`
# is the single source of truth for what's running.

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
log()  { printf '[monitoring-install] %s\n' "$*"; }
warn() { printf '[monitoring-install][WARN] %s\n' "$*" >&2; }
die()  { printf '[monitoring-install][ERROR] %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# OS detection. Writes to global OS_ID and OS_VERSION_ID.
# ---------------------------------------------------------------------------
detect_os() {
    [[ -r /etc/os-release ]] || die "/etc/os-release not found, cannot detect OS"
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_VERSION_ID="${VERSION_ID:-unknown}"
    export OS_ID OS_VERSION_ID
    log "Detected OS: ${OS_ID} ${OS_VERSION_ID}"
}

# Pick the right OS-specific install script. Returns path or empty.
pick_os_script() {
    local dir="$1"
    case "${OS_ID}:${OS_VERSION_ID}" in
        amzn:2)            echo "${dir}/alinux2.sh" ;;
        amzn:2023)         echo "${dir}/alinux2023.sh" ;;
        ubuntu:22.04)      echo "${dir}/ubuntu22.sh" ;;
        ubuntu:24.04)      echo "${dir}/ubuntu24.sh" ;;
        rhel:9*|rocky:9*|almalinux:9*|centos:9*) echo "${dir}/rhel9.sh" ;;
        *) echo "" ;;
    esac
}

# ---------------------------------------------------------------------------
# Detect CPU arch for downloading the right exporter binary.
# ---------------------------------------------------------------------------
detect_arch() {
    case "$(uname -m)" in
        x86_64)  echo "amd64" ;;
        aarch64) echo "arm64" ;;
        *) die "Unsupported CPU arch: $(uname -m)" ;;
    esac
}

# ---------------------------------------------------------------------------
# Detect GPU instances by checking for an NVIDIA device, not by parsing the
# instance-type string. Works for current and future GPU families.
# ---------------------------------------------------------------------------
has_nvidia_gpu() {
    lspci 2>/dev/null | grep -qi 'nvidia' && return 0
    [[ -e /dev/nvidia0 ]] && return 0
    return 1
}


# ---------------------------------------------------------------------------
# Install rivosinc prometheus-slurm-exporter from a prebuilt release.
# Replaces the old "go build from vpenso fork" flow.
# ---------------------------------------------------------------------------
install_slurm_exporter() {
    local arch tmpdir url
    arch="$(detect_arch)"
    tmpdir="$(mktemp -d)"
    url="https://github.com/${SLURM_EXPORTER_REPO}/releases/download/v${SLURM_EXPORTER_VERSION}/prometheus-slurm-exporter_linux_${arch}.tar.gz"

    log "Downloading prometheus-slurm-exporter ${SLURM_EXPORTER_VERSION} (${arch})"
    curl -fsSL "${url}" -o "${tmpdir}/exporter.tar.gz"
    tar -xzf "${tmpdir}/exporter.tar.gz" -C "${tmpdir}"
    install -m 0755 "${tmpdir}/prometheus-slurm-exporter" /usr/bin/prometheus-slurm-exporter
    rm -rf "${tmpdir}"

    log "Installing slurm_exporter systemd unit"
    install -m 0644 "${MONITORING_HOME}/prometheus-slurm-exporter/slurm_exporter.service" \
        /etc/systemd/system/slurm_exporter.service
    systemctl daemon-reload
    systemctl enable slurm_exporter
    systemctl restart slurm_exporter
}

# ---------------------------------------------------------------------------
# Install the EFA hw_counters textfile collector + systemd timer.
# Safe on all nodes/instances: efa-metrics.sh writes an empty file when no
# EFA hardware is present. Called on head/login and compute nodes so EFA
# panels populate wherever EFA-capable instances run node_exporter.
# ---------------------------------------------------------------------------
install_efa_collector() {
    mkdir -p /var/lib/prometheus/node-exporter
    install -m 0755 "${MONITORING_HOME}/custom-metrics/efa-metrics.sh" /usr/local/bin/
    install -m 0644 "${MONITORING_HOME}/systemd/efa-metrics.service" /etc/systemd/system/
    install -m 0644 "${MONITORING_HOME}/systemd/efa-metrics.timer" /etc/systemd/system/
    systemctl daemon-reload
    /usr/local/bin/efa-metrics.sh || true   # first run (no-op without EFA)
    systemctl enable --now efa-metrics.timer
    log "EFA hw_counters textfile collector active"
}

# ---------------------------------------------------------------------------
# Verify that "docker" and "docker compose" (v2) both work.
# ---------------------------------------------------------------------------
verify_docker() {
    docker --version >/dev/null 2>&1 || die "docker not installed"
    docker compose version >/dev/null 2>&1 || die "docker compose plugin not installed"
    log "docker: $(docker --version)"
    log "compose: $(docker compose version --short)"
}
