#!/bin/bash
# Platform detection (parallelcluster|pcs) in installer/platform/platform.sh
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Main OS-aware installer. Sourced/run by post-install.sh on the
# ParallelCluster HeadNode and ComputeFleet nodes.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=installer/common.sh
. "${SCRIPT_DIR}/common.sh"

# Detect platform (ParallelCluster or PCS) and source its config.
# shellcheck source=installer/platform/platform.sh
. "${SCRIPT_DIR}/platform/platform.sh"
detect_platform

MONITORING_DIR_NAME="aws-parallelcluster-monitoring"
# Derive MONITORING_HOME from where this script actually lives (the extracted
# tree's installer/ dir → its parent), so it works wherever post-install.sh
# extracted it. NOTE: this must be node-LOCAL (post-install.sh uses /opt), never
# the shared /home — see the Stale-file-handle note in post-install.sh.
MONITORING_HOME="$(cd "${SCRIPT_DIR}/.." && pwd)"
export MONITORING_HOME MONITORING_DIR_NAME

log "Platform: ${PLATFORM}"
log "Node type: ${PLATFORM_NODE_TYPE}"
log "Cluster: ${PLATFORM_CLUSTER_NAME}"
log "Monitoring home: ${MONITORING_HOME}"

# ---------------------------------------------------------------------------
# 1. Install docker + compose plugin for this OS.
# ---------------------------------------------------------------------------
detect_os
os_script="$(pick_os_script "${SCRIPT_DIR}/os")"
[[ -n "${os_script}" && -r "${os_script}" ]] \
    || die "Unsupported OS: ${OS_ID} ${OS_VERSION_ID}. Supported: amzn2, amzn2023, ubuntu 22.04/24.04/26.04, rhel/rocky/alma/centos-stream 9.x/10.x"

# Fast path: if the container runtime is already baked into the AMI (custom
# AMI) or was installed by a previous run, skip the whole per-OS bootstrap
# (docker install + ~34MB compose plugin download). This is the bulk of a
# compute node's monitoring boot overhead on a pre-baked AMI. `docker start`
# is a no-op if it's already running; it fails harmlessly (|| true) when
# docker isn't installed yet, and we fall through to the OS bootstrap.
systemctl start docker >/dev/null 2>&1 || true
if docker_runtime_ready; then
    log "Container runtime already present (pre-baked AMI) — skipping OS bootstrap (${os_script##*/})"
    systemctl enable docker >/dev/null 2>&1 || true
    usermod -a -G docker "${PLATFORM_USER}" 2>/dev/null || true
    # The skipped OS bootstrap is what installs jq and bc on head/login nodes.
    case "${PLATFORM_NODE_TYPE}" in head|login) ensure_jq_and_bc ;; esac
else
    log "Running OS bootstrap: ${os_script}"
    # shellcheck disable=SC1090
    . "${os_script}"
fi

verify_docker

# The cost collector needs python3 3.7 or later: fail before the stack is
# configured when the node can't provide it.
case "${PLATFORM_NODE_TYPE}" in head|login) require_python3 ;; esac

# ---------------------------------------------------------------------------
# 2. Node-type-specific configuration.
# ---------------------------------------------------------------------------
case "${PLATFORM_NODE_TYPE}" in

    head|login)
        log "Configuring HeadNode"

        # Only root and the containers that call AWS (prometheus, grafana,
        # cloudwatch-exporter) may read the instance-role credentials. They get
        # this group through group_add in compose/head.yml, which needs the
        # numeric GID because the group name doesn't exist inside them.
        getent group "${CREDS_GROUP}" >/dev/null || groupadd --system "${CREDS_GROUP}"
        creds_gid="$(getent group "${CREDS_GROUP}" | cut -d: -f3)"
        [[ -n "${creds_gid}" ]] || die "Could not resolve the GID of group ${CREDS_GROUP}"

        # Extract context from chef dna.json and CloudFormation.

        chown "${PLATFORM_USER}:${PLATFORM_USER}" -R "${MONITORING_HOME}"
        chmod +x "${MONITORING_HOME}/custom-metrics/"*

        cp -rp "${MONITORING_HOME}/custom-metrics/"* /usr/local/bin/

        # The cost collector runs from a systemd timer (install_cost_collector,
        # below). Remove the cron entry and script of older releases. This must
        # follow the cp above, which copies back an old script left in the tree.
        remove_legacy_cost_collector

        # Token replacement in dashboards/config. (Phase 3 will replace all
        # of this with Grafana template variables.)
        # Dashboards now use Grafana template variables (Phase 3a) — no
        # sed token replacement needed. Variables auto-resolve from
        # Prometheus labels (head_instance_id) or user input (fsx_id,
        # s3_bucket). Only prometheus.yml still needs region substitution.
        # Pick the right Prometheus config for the platform.
        if [[ "${PLATFORM}" == "pcs" ]]; then
            cp "${MONITORING_HOME}/prometheus/prometheus-pcs.yml" \
               "${MONITORING_HOME}/prometheus/prometheus.yml"
            sed -i "s|__SLURMCTLD_IP__|${PCS_SLURMCTLD_IP}|g"     "${MONITORING_HOME}/prometheus/prometheus.yml"
            sed -i "s|__PCS_CLUSTER_ID__|${PCS_CLUSTER_ID}|g"     "${MONITORING_HOME}/prometheus/prometheus.yml"
            # This login node's compute node group, whose instances EC2 service
            # discovery drops; 'none' (no group) for a login node launched
            # outside PCS. pcs.sh validated the ID.
            sed -i "s|__PCS_LOGIN_NODE_GROUP_ID__|${PCS_NODE_GROUP_ID:-none}|g" "${MONITORING_HOME}/prometheus/prometheus.yml"
            # Login node instance ID for the static scrape target.
            login_id=$(curl -sf -H "X-aws-ec2-metadata-token: $(curl -sf -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)" \
                http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo "unknown")
            sed -i "s|__LOGIN_INSTANCE_ID__|${login_id}|g"         "${MONITORING_HOME}/prometheus/prometheus.yml"
        else
            sed -i "s|__PC_CLUSTER_NAME__|${PLATFORM_CLUSTER_NAME}|g" "${MONITORING_HOME}/prometheus/prometheus.yml"
            # Slurm metrics: slurmctld's native metrics when it serves them,
            # the Slurm exporter otherwise (docs/slurm-metrics.md). Written
            # before the stack starts; re-runs load it at the Prometheus
            # restart at the end.
            detect_slurm_metrics_source
            set_slurm_scrape_jobs "${MONITORING_HOME}/prometheus/prometheus.yml" \
                "${MONITORING_HOME}/prometheus/scrape-slurm-${SLURM_METRICS_SOURCE}.yml" || {
                warn "${MONITORING_HOME}/prometheus/prometheus.yml has no slurm_metrics block (the lines '  # BEGIN slurm_metrics' and '  # END slurm_metrics'): keeping its Slurm scrape jobs as they are and using the Slurm exporter"
                SLURM_METRICS_SOURCE=exporter
            }
        fi
        # Amazon RES (Research and Engineering Studio) VDI discovery — opt-in.
        # Only when RES_ENVIRONMENT_NAME is set on this monitoring node do we
        # append the res_instances scrape job, so RES desktops populate the
        # RES Node List / Details dashboards. No-op otherwise; non-RES
        # deployments are completely unchanged. Appended BEFORE the region
        # substitution below so the snippet's __AWS_REGION__ is resolved too.
        if [[ -n "${RES_ENVIRONMENT_NAME:-}" && -f "${MONITORING_HOME}/prometheus/prometheus-res.yml" ]]; then
            log "RES discovery enabled for environment: ${RES_ENVIRONMENT_NAME}"
            sed "s|__RES_ENVIRONMENT_NAME__|${RES_ENVIRONMENT_NAME}|g" \
                "${MONITORING_HOME}/prometheus/prometheus-res.yml" \
                >> "${MONITORING_HOME}/prometheus/prometheus.yml"
        fi
        sed -i "s/__AWS_REGION__/${PLATFORM_REGION}/g"        "${MONITORING_HOME}/prometheus/prometheus.yml"
        sed -i "s/__AWS_REGION__/${PLATFORM_REGION}/g"        "${MONITORING_HOME}/cloudwatch-exporter/config.yml"
        sed -i "s/__AWS_REGION__/${PLATFORM_REGION}/g"        "${MONITORING_HOME}/grafana/datasources/datasource.yml"
        sed -i "s|__MONITORING_HOME__|${MONITORING_HOME}|g" "${MONITORING_HOME}/compose/head.yml"
        sed -i "s|__CREDS_GID__|${creds_gid}|g" "${MONITORING_HOME}/compose/head.yml"

        # Deploy platform-specific dashboards alongside the shared ones.
        # The per-platform subdir is named 'pcluster' / 'pcs' (NOT the
        # ${PLATFORM} value, which is 'parallelcluster' / 'pcs') — map it.
        # Grafana provisions the dashboards directory RECURSIVELY, so after
        # copying the platform's *.json up to the root we must remove BOTH
        # the pcs/ and pcluster/ subdirectories — otherwise the copied
        # dashboards exist twice (root + subdir), Grafana sees duplicate
        # UIDs, and refuses to save ANY provisioned dashboard ("the same UID
        # is used more than once" → "no database write permissions because
        # of duplicates"). Removing the active platform's subdir is just as
        # important as removing the other platform's.
        case "${PLATFORM}" in
            parallelcluster) dash_subdir="pcluster" ;;
            pcs)             dash_subdir="pcs" ;;
            *)               dash_subdir="" ;;
        esac
        if [[ -n "${dash_subdir}" && -d "${MONITORING_HOME}/grafana/dashboards/${dash_subdir}" ]]; then
            cp -f "${MONITORING_HOME}/grafana/dashboards/${dash_subdir}/"*.json \
                  "${MONITORING_HOME}/grafana/dashboards/" 2>/dev/null || true
        fi
        rm -rf "${MONITORING_HOME}/grafana/dashboards/pcs" \
               "${MONITORING_HOME}/grafana/dashboards/pcluster"

        # Cluster Logs dashboard (ParallelCluster only): substitute the
        # CloudWatch log group name + ARN and the region so the Logs panels
        # resolve.
        # PC writes the cluster log group into /etc/chef/dna.json as
        # cluster.log_group_name (it carries a creation-timestamp suffix,
        # so it can't be hardcoded). The Grafana CloudWatch logs query
        # requires the log group ARN, which we derive from name + region +
        # account id (from STS). If logging is disabled or the file is
        # absent, drop the Logs dashboard so it doesn't ship broken.
        logs_dash="${MONITORING_HOME}/grafana/dashboards/logs.json"
        if [[ "${PLATFORM}" == "parallelcluster" && -f "${logs_dash}" ]]; then
            lg_name=""
            if [[ -r /etc/chef/dna.json ]]; then
                lg_name=$(python3 -c 'import json,sys
try:
    d=json.load(open("/etc/chef/dna.json"))
    print(d.get("cluster",{}).get("log_group_name",""))
except Exception:
    pass' 2>/dev/null || echo "")
            fi
            acct_id=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")
            if [[ -n "${lg_name}" && -n "${acct_id}" ]]; then
                lg_arn="arn:aws:logs:${PLATFORM_REGION}:${acct_id}:log-group:${lg_name}:*"
                sed -i "s|__LOG_GROUP_NAME__|${lg_name}|g" "${logs_dash}"
                sed -i "s|__LOG_GROUP_ARN__|${lg_arn}|g"   "${logs_dash}"
                # Each Logs panel query names its region too.
                sed -i "s/__AWS_REGION__/${PLATFORM_REGION}/g" "${logs_dash}"
                log "Cluster Logs dashboard wired to ${lg_name}"
            else
                log "WARN: could not resolve PC log group (logging disabled?); removing Logs dashboard"
                rm -f "${logs_dash}"
            fi
        fi

        # Self-signed TLS cert for nginx.
        # Includes multiple SANs so the cert is valid for:
        #   - localhost (SSM port-forward)
        #   - private IP (direct VPC access)
        #   - private hostname (Slurm node name)
        # Validity: 10 years. Users who want a trusted cert should put an
        # ALB with ACM in front — see docs/public-access.md.
        nginx_dir="${MONITORING_HOME}/nginx"
        nginx_ssl_dir="${nginx_dir}/ssl"
        mkdir -p "${nginx_ssl_dir}"

        private_ip=$(hostname -I 2>/dev/null | awk '{print $1}') || private_ip=""
        private_hostname=$(hostname -f 2>/dev/null) || private_hostname=""

        {
            echo ""
            echo "DNS.1=localhost"
            [[ -n "${private_hostname}" ]] && echo "DNS.2=${private_hostname}"
            echo "IP.1=127.0.0.1"
            [[ -n "${private_ip}" ]] && echo "IP.2=${private_ip}"
        } >> "${nginx_dir}/openssl.cnf"

        log "TLS cert SANs: localhost, ${private_hostname:-n/a}, 127.0.0.1, ${private_ip:-n/a}"
        openssl req -new -x509 -nodes -newkey rsa:4096 -days 3650 \
            -keyout "${nginx_ssl_dir}/nginx.key" \
            -out "${nginx_ssl_dir}/nginx.crt" \
            -config "${nginx_dir}/openssl.cnf" >/dev/null 2>&1
        chown -R "${PLATFORM_USER}:${PLATFORM_USER}" "${nginx_ssl_dir}"

        # -------------------------------------------------------------
        # Grafana admin password: generate random, write to SSM SecureString,
        # set up a systemd timer to materialize it into a mounted file.
        # Idempotent: if the SSM parameter already exists we reuse it (so
        # subsequent runs / updates don't break existing logins).
        # -------------------------------------------------------------
        GRAFANA_SSM_PARAM="/${PLATFORM}/${PLATFORM_CLUSTER_NAME}/grafana/admin-password"
        if aws ssm get-parameter --region "${PLATFORM_REGION}" --name "${GRAFANA_SSM_PARAM}" --with-decryption >/dev/null 2>&1; then
            log "Reusing existing Grafana password in ${GRAFANA_SSM_PARAM}"
        else
            log "Generating new Grafana admin password, storing in ${GRAFANA_SSM_PARAM}"
            # Keep the password out of the trace (see xtrace_suspend).
            { xtrace_suspend; } 2>/dev/null
            GRAFANA_PASSWORD=$(openssl rand -hex 16)
            aws ssm put-parameter --region "${PLATFORM_REGION}" \
                --name "${GRAFANA_SSM_PARAM}" \
                --type SecureString \
                --value "${GRAFANA_PASSWORD}" \
                --tags "Key=${PLATFORM}:cluster-name,Value=${PLATFORM_CLUSTER_NAME}" \
                --no-overwrite >/dev/null
            unset GRAFANA_PASSWORD
            { xtrace_restore; } 2>/dev/null
        fi

        # Install the Grafana password refresh timer.
        install -m 0755 "${MONITORING_HOME}/custom-metrics/refresh-grafana-password.sh" /usr/local/bin/
        install -m 0644 "${MONITORING_HOME}/systemd/grafana-password-refresh.service" /etc/systemd/system/
        install -m 0644 "${MONITORING_HOME}/systemd/grafana-password-refresh.timer" /etc/systemd/system/
        systemctl daemon-reload
        # Run once immediately so the file exists before Grafana starts.
        /usr/local/bin/refresh-grafana-password.sh
        systemctl enable --now grafana-password-refresh.timer
        log "Grafana password refresh timer active"

        # -------------------------------------------------------------
        # Grafana's root URL and the optional Cognito SSO (cognito/README.md).
        # refresh-grafana-cognito.sh reads the SecureString JSON in
        # /<platform>/<cluster>/grafana/cognito and writes
        # /run/grafana-secrets/cognito.env and, with Cognito, the client
        # secret file. /run is tmpfs, so grafana-cognito-refresh.service
        # runs the script again at every boot. Without the parameter, or
        # when it can't be read, Grafana offers only the local admin login.
        # The script runs in its own process, so the client secret stays out
        # of this script's trace.
        # -------------------------------------------------------------
        COGNITO_SSM_PARAM="/${PLATFORM}/${PLATFORM_CLUSTER_NAME}/grafana/cognito"
        cognito_unit_env=/etc/aws-parallelcluster-monitoring/grafana-cognito.env
        install -d -m 0755 /etc/aws-parallelcluster-monitoring
        printf 'COGNITO_SSM_PARAM=%s\nREGION=%s\n' "${COGNITO_SSM_PARAM}" "${PLATFORM_REGION}" \
            > "${cognito_unit_env}.new"
        chmod 0644 "${cognito_unit_env}.new"
        mv -f "${cognito_unit_env}.new" "${cognito_unit_env}"
        install -m 0755 "${MONITORING_HOME}/custom-metrics/refresh-grafana-cognito.sh" /usr/local/bin/
        install -m 0644 "${MONITORING_HOME}/systemd/grafana-cognito-refresh.service" /etc/systemd/system/
        systemctl daemon-reload
        systemctl enable grafana-cognito-refresh.service
        COGNITO_SSM_PARAM="${COGNITO_SSM_PARAM}" REGION="${PLATFORM_REGION}" \
            /usr/local/bin/refresh-grafana-cognito.sh \
            || warn "Cognito SSO is off: see the error above. Grafana offers only the local admin login"

                # Set up credential refresh for Prometheus ec2_sd_configs.
        # ParallelCluster's Imds.Secured=true blocks IMDS from non-root
        # processes (including containers). This timer runs as root on the
        # host, fetches role creds from IMDS, and writes them to a file
        # that's bind-mounted into the Prometheus container.
        install -m 0755 "${MONITORING_HOME}/custom-metrics/refresh-ec2-credentials.sh" /usr/local/bin/
        install -m 0644 "${MONITORING_HOME}/systemd/prometheus-creds-refresh.service" /etc/systemd/system/
        install -m 0644 "${MONITORING_HOME}/systemd/prometheus-creds-refresh.timer" /etc/systemd/system/
        systemctl daemon-reload
        # Run once immediately so creds exist before Prometheus starts.
        /usr/local/bin/refresh-ec2-credentials.sh
        systemctl enable --now prometheus-creds-refresh.timer
        log "EC2 credential refresh timer active"

        # Start the monitoring stack. Bind-mount source paths are resolved by
        # the __MONITORING_HOME__ substitution above (node-local /opt path), so
        # no compose env file is needed.
        cd "${MONITORING_HOME}"
        release_web_ports_from_apache2
        release_prometheus_port_from_cockpit
        docker compose \
            -f "${MONITORING_HOME}/compose/head.yml" \
            -p monitoring-head up -d
        wait_for_nginx
        wait_for_prometheus

        # Slurm job-to-node textfile collector (Phase 3c).
        # Runs every 30s, writes /var/lib/prometheus/node-exporter/slurm_jobs.prom
        # which node_exporter scrapes via --collector.textfile.
        mkdir -p /var/lib/prometheus/node-exporter
        install -m 0755 "${MONITORING_HOME}/custom-metrics/slurm-job-nodes.sh" /usr/local/bin/
        install -m 0644 "${MONITORING_HOME}/systemd/slurm-job-nodes.service" /etc/systemd/system/
        install -m 0644 "${MONITORING_HOME}/systemd/slurm-job-nodes.timer" /etc/systemd/system/
        systemctl daemon-reload
        /usr/local/bin/slurm-job-nodes.sh || true  # first run (may fail if slurmctld not ready)
        systemctl enable --now slurm-job-nodes.timer
        log "Slurm job-node textfile collector active"

        # EFA hw_counters collector (head/login may itself be EFA-capable,
        # and this keeps the collector list consistent across node types).
        install_efa_collector

        # Slurm metrics source:
        #   - ParallelCluster: native OpenMetrics on slurmctld:6820 when
        #     slurmctld serves them (detect_slurm_metrics_source above); the
        #     Slurm exporter is then removed. Otherwise
        #     rivosinc/prometheus-slurm-exporter (port 9092)
        #   - PCS: native OpenMetrics on slurmctld:6817 (no extra process)
        if [[ "${PLATFORM}" == "parallelcluster" ]]; then
            if [[ "${SLURM_METRICS_SOURCE:-exporter}" == native ]]; then
                remove_slurm_exporter
            else
                install_slurm_exporter
            fi
        fi

        # Cluster cost collector (systemd timer, every 60 s). Prometheus is
        # restarted last so that it loads rules/cost.yml and the Pushgateway
        # drop rule; a failure there leaves every collector installed.
        delete_legacy_cost_push_group
        install_cost_collector
        restart_prometheus_load_rules
        ;;

    compute)
        log "Configuring ComputeFleet node"

        # EFA hw_counters textfile collector. Compute nodes are where EFA
        # hardware lives (p4/p5/hpc6a/c5n/...); writes an empty file and is
        # a no-op on non-EFA instances.
        install_efa_collector

        if has_nvidia_gpu; then
            log "NVIDIA GPU detected — installing nvidia-container-toolkit"
            # Replaces deprecated nvidia-docker2.
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
                    # Wait for the apt and dpkg locks instead of failing:
                    # unattended-upgrades can hold them in the first minutes
                    # after boot (apt-daily-upgrade.timer is Persistent).
                    apt-get -o DPkg::Lock::Timeout=600 update
                    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y nvidia-container-toolkit
                    ;;
            esac
            nvidia-ctk runtime configure --runtime=docker
            systemctl restart docker

            # compute.gpu.yml's dcgm counters bind-mount resolves via the
            # __MONITORING_HOME__ substitution (node-local /opt path).
            sed -i "s|__MONITORING_HOME__|${MONITORING_HOME}|g" \
                "${MONITORING_HOME}/compose/compute.gpu.yml"
            # Allow the dcgm-exporter image to be overridden. dcgm/counters.csv
            # uses the DCGM 4.7 field names, so an override must be
            # dcgm-exporter 4.9.0 or later. When this is unset or empty,
            # compute.gpu.yml uses its default, 4.9.0, which covers B300. See #50.
            export DCGM_EXPORTER_IMAGE="${DCGM_EXPORTER_IMAGE:-}"
            docker compose \
                -f "${MONITORING_HOME}/compose/compute.gpu.yml" \
                -p monitoring-compute up -d
        else
            docker compose -f "${MONITORING_HOME}/compose/compute.yml" \
                -p monitoring-compute up -d
        fi
        ;;

    *)
        warn "Unknown node type: ${PLATFORM_NODE_TYPE}, skipping"
        ;;
esac

# Final summary: surface the Grafana password location so users know
# how to retrieve it. This is printed AFTER everything is up so it's
# the last thing in the log.
if [[ "${PLATFORM_NODE_TYPE}" == "head" || "${PLATFORM_NODE_TYPE}" == "login" ]]; then
    log "==========================================================="
    log "Grafana admin password is in SSM Parameter Store:"
    log "  ${GRAFANA_SSM_PARAM:-/${PLATFORM}/${PLATFORM_CLUSTER_NAME}/grafana/admin-password}"
    log "Retrieve with:"
    log "  aws ssm get-parameter --region ${PLATFORM_REGION} \\"
    log "    --name /${PLATFORM}/${PLATFORM_CLUSTER_NAME}/grafana/admin-password \\"
    log "    --with-decryption --query Parameter.Value --output text"
    log "==========================================================="
fi
log "Done."
