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
# SHA-256 of the release tarballs, as published in the release's
# prometheus-slurm-exporter_1.8.0_checksums.txt. Update them with the version.
readonly SLURM_EXPORTER_SHA256_AMD64="903b8e3c9684a6c43e433b36a3582c1c93d461a1a9516a666d2ef0a8886c2046"
readonly SLURM_EXPORTER_SHA256_ARM64="38adb5e51df1c4fa43709e4f03cc9eb67264bd923eff63ef357b13a96d1a09ef"

# Docker Compose v2 plugin. Amazon Linux 2 / 2023 install this binary directly
# (their repos don't ship docker-compose-plugin); RHEL/Ubuntu get it from the
# docker-ce repo. Kept here as the single source of truth so the OS scripts and
# the idempotency check below agree on one version.
readonly COMPOSE_PLUGIN_VERSION="v5.8.0"
readonly COMPOSE_PLUGIN_PATH="/usr/libexec/docker/cli-plugins/docker-compose"

# System group that may read the instance-role credentials in
# /run/prometheus-ec2-creds. custom-metrics/refresh-ec2-credentials.sh uses
# the same name.
# shellcheck disable=SC2034  # used by installer/install.sh
readonly CREDS_GROUP="monitoring-creds"

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
# Keep secrets out of the shell trace. post-install.sh runs the installer
# under `bash -x`; the trace goes to the install log and, on ParallelCluster,
# to cfn-init.log and from there to CloudWatch Logs. Wrap code that handles a
# secret in:
#   { xtrace_suspend; } 2>/dev/null
#   ...
#   { xtrace_restore; } 2>/dev/null
# The braces and 2>/dev/null keep the toggles themselves out of the trace.
# xtrace_restore only turns tracing back on if it was on before, so a plain
# `sudo bash install.sh` re-run stays untraced.
# ---------------------------------------------------------------------------
xtrace_suspend() {
    if [[ $- == *x* ]]; then _XTRACE_WAS_ON=1; else _XTRACE_WAS_ON=0; fi
    set +x
}
xtrace_restore() {
    if [[ "${_XTRACE_WAS_ON:-0}" == 1 ]]; then set -x; fi
}

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
    grep -qi 'nvidia' <<<"$(lspci 2>/dev/null)" && return 0
    [[ -e /dev/nvidia0 ]] && return 0
    return 1
}


# ---------------------------------------------------------------------------
# Install rivosinc prometheus-slurm-exporter from a prebuilt release.
# Replaces the old "go build from vpenso fork" flow.
# ---------------------------------------------------------------------------
install_slurm_exporter() {
    local arch tmpdir asset url expected
    arch="$(detect_arch)"
    case "${arch}" in
        amd64) expected="${SLURM_EXPORTER_SHA256_AMD64}" ;;
        arm64) expected="${SLURM_EXPORTER_SHA256_ARM64}" ;;
    esac
    asset="prometheus-slurm-exporter_linux_${arch}.tar.gz"
    url="https://github.com/${SLURM_EXPORTER_REPO}/releases/download/v${SLURM_EXPORTER_VERSION}/${asset}"
    tmpdir="$(mktemp -d)"
    log "Downloading prometheus-slurm-exporter ${SLURM_EXPORTER_VERSION} (${arch})"
    curl -fsSL "${url}" -o "${tmpdir}/${asset}"
    if ! (cd "${tmpdir}" && printf '%s  %s\n' "${expected}" "${asset}" | sha256sum -c --status -); then
        rm -rf "${tmpdir}"
        die "${asset}: SHA-256 mismatch with the published checksum, refusing to install"
    fi
    log "Verified SHA-256 of ${asset}"
    tar -xzf "${tmpdir}/${asset}" -C "${tmpdir}"
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
# (e.g. baked into a custom AMI, or a previous run), the ~34MB download is
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
# Head/login nodes need jq (Cognito config, refresh-ec2-credentials.sh) and
# bc (cost-metrics.sh). The per-OS bootstrap installs them, so when the fast
# path skips that bootstrap, install whichever is missing. No package-manager
# call when both are present, so a pre-baked AMI still makes no repo calls.
# Each tool is installed on its own, because dnf and apt-get fail the whole
# command when one package is unavailable.
# ---------------------------------------------------------------------------
ensure_jq_and_bc() {
    local tool missing=()
    for tool in jq bc; do
        command -v "${tool}" >/dev/null 2>&1 || missing+=("${tool}")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        log "Fast path: jq and bc already present"
        return 0
    fi
    log "Fast path: installing missing head/login tools: ${missing[*]}"
    if [[ "${OS_ID}" == "ubuntu" ]]; then
        DEBIAN_FRONTEND=noninteractive apt-get update || true
    fi
    for tool in "${missing[@]}"; do
        case "${OS_ID}:${OS_VERSION_ID}" in
            amzn:2)   yum -y install "${tool}" ;;
            ubuntu:*) DEBIAN_FRONTEND=noninteractive apt-get install -y "${tool}" ;;
            *)        dnf -y install "${tool}" ;;
        esac || warn "could not install ${tool}"
    done
    command -v jq >/dev/null 2>&1 || die "jq is required on head/login nodes and could not be installed"
    command -v bc >/dev/null 2>&1 || warn "bc not available, cost metrics will read 0"
}

# ---------------------------------------------------------------------------
# The official ParallelCluster Ubuntu 24.04 AMIs run apache2 on port 80. The
# monitoring nginx uses the host network and needs ports 80 and 443, so stop
# and disable apache2 when it holds either port. apache2 on other ports is
# left alone.
# ---------------------------------------------------------------------------
release_web_ports_from_apache2() {
    local listeners
    listeners="$(ss -Hltnp '( sport = :80 or sport = :443 )' 2>/dev/null)" || listeners=""
    if grep -q '"apache2"' <<<"${listeners}"; then
        log "apache2 holds port 80 or 443, which the monitoring nginx needs: stopping and disabling apache2"
        systemctl disable --now apache2
    fi
}

# ---------------------------------------------------------------------------
# docker compose up succeeds even when nginx then fails to bind and
# restarts in a loop, so check that it serves the landing page.
# ---------------------------------------------------------------------------
wait_for_nginx() {
    local _ body
    for _ in $(seq 1 30); do
        body="$(curl -fsk --max-time 5 https://127.0.0.1/ 2>/dev/null)" || body=""
        if grep -q '<title>HPC Cluster Monitoring</title>' <<<"${body}"; then
            log "nginx is serving https://127.0.0.1/"
            return 0
        fi
        sleep 2
    done
    warn "listeners on ports 80 and 443:"
    ss -Hltnp '( sport = :80 or sport = :443 )' >&2 || true
    docker logs --tail 20 nginx >&2 || true
    die "nginx is not serving https://127.0.0.1/ after 60 s; another process may hold port 80 or 443"
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
