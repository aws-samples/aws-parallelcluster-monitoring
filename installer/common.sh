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

# Docker Compose v2 plugin. Amazon Linux 2 / 2023 install this binary directly
# (their repos don't ship docker-compose-plugin); RHEL/Ubuntu get it from the
# docker-ce repo. Kept here as the single source of truth so the OS scripts and
# the idempotency check below agree on one version.
readonly COMPOSE_PLUGIN_VERSION="v5.1.4"
readonly COMPOSE_PLUGIN_PATH="/usr/libexec/docker/cli-plugins/docker-compose"

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
# Install the Docker Compose v2 plugin binary at the pinned version,
# idempotently. Used by the OS scripts whose repos don't ship the plugin as a
# package (Amazon Linux 2 / 2023). If the pinned version is already installed
# (e.g. baked into a custom AMI, or a previous run), the ~32MB download is
# skipped — this is what stops the plugin being re-fetched on every boot.
# ---------------------------------------------------------------------------
install_compose_plugin_binary() {
    local want="${COMPOSE_PLUGIN_VERSION#v}" have=""
    if [[ -x "${COMPOSE_PLUGIN_PATH}" ]]; then
        have="$(docker compose version --short 2>/dev/null | sed 's/^v//')"
    fi
    if [[ "${have}" == "${want}" ]]; then
        log "Docker Compose plugin v${want} already present — skipping download"
        return 0
    fi
    log "Installing Docker Compose plugin ${COMPOSE_PLUGIN_VERSION}"
    install -d -m 0755 "$(dirname "${COMPOSE_PLUGIN_PATH}")"
    curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_PLUGIN_VERSION}/docker-compose-linux-$(uname -m)" \
        -o "${COMPOSE_PLUGIN_PATH}"
    chmod +x "${COMPOSE_PLUGIN_PATH}"
}

# ---------------------------------------------------------------------------
# True when the container runtime is already usable: the docker daemon
# responds and the compose v2 plugin works. On a pre-baked custom AMI this
# lets install.sh skip the whole per-OS bootstrap (docker install + compose
# download), which is the bulk of a compute node's monitoring boot overhead.
# ---------------------------------------------------------------------------
docker_runtime_ready() {
    docker info    >/dev/null 2>&1 || return 1
    docker compose version >/dev/null 2>&1 || return 1
    return 0
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
