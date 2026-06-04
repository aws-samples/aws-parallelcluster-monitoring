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
    || die "Unsupported OS: ${OS_ID} ${OS_VERSION_ID}. Supported: amzn2, amzn2023, ubuntu 22.04/24.04, rhel/rocky/alma/centos-stream 9.x"

log "Running OS bootstrap: ${os_script}"
# shellcheck disable=SC1090
. "${os_script}"

verify_docker

# ---------------------------------------------------------------------------
# 2. Node-type-specific configuration.
# ---------------------------------------------------------------------------
case "${PLATFORM_NODE_TYPE}" in

    head|login)
        log "Configuring HeadNode"

        # Extract context from chef dna.json and CloudFormation.

        chown "${PLATFORM_USER}:${PLATFORM_USER}" -R "${MONITORING_HOME}"
        chmod +x "${MONITORING_HOME}/custom-metrics/"*

        cp -rp "${MONITORING_HOME}/custom-metrics/"* /usr/local/bin/

        # Cost estimator cron (every minute). Single unified script replaces
        # the old 1m + 1h split. Runs as root (needs EC2 describe + pricing API).
        crontab -l 2>/dev/null > /tmp/crontab.tmp || true
        {
            echo 'MAILTO=""'
            grep -v -E 'MAILTO|cost-metrics' /tmp/crontab.tmp || true
            echo '* * * * * /usr/local/bin/cost-metrics.sh >/dev/null 2>&1'
        } | crontab -
        rm -f /tmp/crontab.tmp

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
            # Login node instance ID for the static scrape target.
            login_id=$(curl -sf -H "X-aws-ec2-metadata-token: $(curl -sf -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)" \
                http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo "unknown")
            sed -i "s|__LOGIN_INSTANCE_ID__|${login_id}|g"         "${MONITORING_HOME}/prometheus/prometheus.yml"
        else
            sed -i "s|__PC_CLUSTER_NAME__|${PLATFORM_CLUSTER_NAME}|g" "${MONITORING_HOME}/prometheus/prometheus.yml"
        fi
        sed -i "s/__AWS_REGION__/${PLATFORM_REGION}/g"        "${MONITORING_HOME}/prometheus/prometheus.yml"
        sed -i "s/__AWS_REGION__/${PLATFORM_REGION}/g"        "${MONITORING_HOME}/cloudwatch-exporter/config.yml"
        sed -i "s/__AWS_REGION__/${PLATFORM_REGION}/g"        "${MONITORING_HOME}/grafana/datasources/datasource.yml"
        sed -i "s|__MONITORING_HOME__|${MONITORING_HOME}|g" "${MONITORING_HOME}/compose/head.yml"

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
        # CloudWatch log group name + ARN so the Logs panels resolve.
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
            GRAFANA_PASSWORD=$(openssl rand -hex 16)
            aws ssm put-parameter --region "${PLATFORM_REGION}" \
                --name "${GRAFANA_SSM_PARAM}" \
                --type SecureString \
                --value "${GRAFANA_PASSWORD}" \
                --tags "Key=${PLATFORM}:cluster-name,Value=${PLATFORM_CLUSTER_NAME}" \
                --no-overwrite >/dev/null
            unset GRAFANA_PASSWORD
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
                # -------------------------------------------------------------
        # Optional: Cognito SSO for Grafana (Phase 2b.2).
        # Users populate /parallelcluster/<cluster>/grafana/cognito with a
        # JSON blob (SecureString):
        #   {
        #     "user_pool_id":   "us-east-2_ABC123",
        #     "client_id":      "xxxxxxxxxxxxxxxx",
        #     "client_secret":  "yyyyyyyyyyyyyyyy",
        #     "domain":         "my-pool-auth",
        #     "region":         "us-east-2",
        #     "allowed_domains": "amazon.com"
        #   }
        # When present, Grafana is configured to use Cognito OAuth2.
        # When absent, local admin/password login is the only auth path.
        # -------------------------------------------------------------
        COGNITO_SSM_PARAM="/${PLATFORM}/${PLATFORM_CLUSTER_NAME}/grafana/cognito"
        cognito_json=$(aws ssm get-parameter --region "${PLATFORM_REGION}" \
            --name "${COGNITO_SSM_PARAM}" --with-decryption \
            --query 'Parameter.Value' --output text 2>/dev/null) || cognito_json=""

        if [[ -n "${cognito_json}" ]]; then
            log "Cognito SSO config found in ${COGNITO_SSM_PARAM}; enabling OAuth2"
            # Extract fields via jq and write to a Grafana env-file that
            # docker-compose loads. Put it under /run so nothing persistent
            # on disk.
            mkdir -p /run/grafana-secrets
            chmod 0750 /run/grafana-secrets
            chown 472:472 /run/grafana-secrets

            cog_client=$(echo "${cognito_json}"  | jq -r .client_id)
            cog_secret=$(echo "${cognito_json}"  | jq -r .client_secret)
            cog_domain=$(echo "${cognito_json}"  | jq -r .domain)
            cog_region=$(echo "${cognito_json}"  | jq -r '.region // "'"${PLATFORM_REGION}"'"')
            cog_allowed=$(echo "${cognito_json}" | jq -r '.allowed_domains // ""')

            cat > /run/grafana-secrets/cognito.env <<COGENV
# Grafana OAuth2 config — loaded by compose at container start
GF_AUTH_GENERIC_OAUTH_ENABLED=true
GF_AUTH_GENERIC_OAUTH_NAME=Cognito
GF_AUTH_GENERIC_OAUTH_ALLOW_SIGN_UP=true
GF_AUTH_GENERIC_OAUTH_CLIENT_ID=${cog_client}
GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET__FILE=/run/grafana-secrets/cognito-client-secret
GF_AUTH_GENERIC_OAUTH_SCOPES=openid email profile
GF_AUTH_GENERIC_OAUTH_AUTH_URL=https://${cog_domain}.auth.${cog_region}.amazoncognito.com/oauth2/authorize
GF_AUTH_GENERIC_OAUTH_TOKEN_URL=https://${cog_domain}.auth.${cog_region}.amazoncognito.com/oauth2/token
GF_AUTH_GENERIC_OAUTH_API_URL=https://${cog_domain}.auth.${cog_region}.amazoncognito.com/oauth2/userInfo
GF_AUTH_GENERIC_OAUTH_ALLOWED_DOMAINS=${cog_allowed}
GF_AUTH_SIGNOUT_REDIRECT_URL=https://${cog_domain}.auth.${cog_region}.amazoncognito.com/logout?client_id=${cog_client}
COGENV
            # Write the client secret to its own file so it doesn't appear in
            # docker inspect / `env` output. Grafana's __FILE env var variant
            # reads it at startup.
            umask 0077
            printf '%s' "${cog_secret}" > /run/grafana-secrets/cognito-client-secret
            chmod 0640 /run/grafana-secrets/cognito-client-secret
            chown 472:472 /run/grafana-secrets/cognito-client-secret
            umask 0022
            chmod 0640 /run/grafana-secrets/cognito.env
            chown 472:472 /run/grafana-secrets/cognito.env
            log "Cognito env file written to /run/grafana-secrets/cognito.env"
            unset cog_secret cog_client
        else
            log "No Cognito config in ${COGNITO_SSM_PARAM}; using local admin login only"
            # Create an empty file so docker-compose doesn't complain about
            # a missing env_file.
            mkdir -p /run/grafana-secrets
            : > /run/grafana-secrets/cognito.env
            chmod 0640 /run/grafana-secrets/cognito.env
        fi

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
        docker compose \
            -f "${MONITORING_HOME}/compose/head.yml" \
            -p monitoring-head up -d

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
        #   - ParallelCluster: rivosinc/prometheus-slurm-exporter (port 9092)
        #   - PCS: native OpenMetrics on slurmctld:6817 (no extra process)
        if [[ "${PLATFORM}" == "parallelcluster" ]]; then
            install_slurm_exporter
        fi
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
                    apt-get update
                    DEBIAN_FRONTEND=noninteractive apt-get install -y nvidia-container-toolkit
                    ;;
            esac
            nvidia-ctk runtime configure --runtime=docker
            systemctl restart docker

            # compute.gpu.yml's dcgm counters bind-mount resolves via the
            # __MONITORING_HOME__ substitution (node-local /opt path).
            sed -i "s|__MONITORING_HOME__|${MONITORING_HOME}|g" \
                "${MONITORING_HOME}/compose/compute.gpu.yml"
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
    log "    --name /parallelcluster/${PLATFORM_CLUSTER_NAME}/grafana/admin-password \\"
    log "    --with-decryption --query Parameter.Value --output text"
    log "==========================================================="
fi
log "Done."
