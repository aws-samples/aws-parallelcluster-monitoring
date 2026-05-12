#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Fetches the per-cluster Grafana admin password from SSM Parameter Store
# and applies it to the Grafana container.
#
# Why: GF_SECURITY_ADMIN_PASSWORD__FILE only sets the default on FIRST boot.
# After Grafana's DB is initialized, the env var is ignored — we need to
# call 'grafana cli admin reset-admin-password' to change the admin user's
# password in the DB.
#
# Behavior:
#   1. Fetch current value from SSM (SecureString, KMS-decrypted).
#   2. Write to /run/grafana-secrets/admin-password (mounted into container).
#   3. If the value changed since last run, invoke grafana-cli inside the
#      container to update the DB record.
#   4. Skip the DB update if the container isn't running (e.g. during boot).
#
set -euo pipefail

# Platform detection — ParallelCluster uses cfnconfig, PCS reads IMDS tag.
if [[ -r /etc/parallelcluster/cfnconfig ]]; then
    # shellcheck source=/dev/null
    . /etc/parallelcluster/cfnconfig
    # shellcheck disable=SC2154
    PLATFORM="parallelcluster"
    # shellcheck disable=SC2154
    CLUSTER_NAME="${stack_name}"
    # shellcheck disable=SC2154
    REGION="${cfn_region}"
else
    PLATFORM="pcs"
    _tok=$(curl -sf -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
        http://169.254.169.254/latest/api/token 2>/dev/null)
    CLUSTER_NAME=$(curl -sf -H "X-aws-ec2-metadata-token: $_tok" \
        http://169.254.169.254/latest/meta-data/tags/instance/aws:pcs:cluster-id 2>/dev/null)
    REGION=$(curl -sf -H "X-aws-ec2-metadata-token: $_tok" \
        http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null)
    [[ -z "$CLUSTER_NAME" || -z "$REGION" ]] && {
        echo "ERROR: unable to detect platform (no cfnconfig, no PCS tags)" >&2
        exit 1
    }
fi

SECRET_DIR="/run/grafana-secrets"
SECRET_FILE="${SECRET_DIR}/admin-password"
LAST_APPLIED="${SECRET_DIR}/.last-applied"
SSM_PARAM="/${PLATFORM}/${CLUSTER_NAME}/grafana/admin-password"

mkdir -p "${SECRET_DIR}"
chmod 0750 "${SECRET_DIR}"
# Grafana runs as UID 472 inside its container
chown 472:472 "${SECRET_DIR}" 2>/dev/null || true

password=$(aws ssm get-parameter \
    --region "${REGION}" \
    --name "${SSM_PARAM}" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text 2>/dev/null) || {
    echo "ERROR: cannot read SSM parameter ${SSM_PARAM}" >&2
    exit 1
}

# Update the mounted file only if changed (avoids unnecessary disk writes).
need_update=1
if [[ -f "${SECRET_FILE}" ]] && [[ "$(cat "${SECRET_FILE}")" == "${password}" ]]; then
    need_update=0
fi

if [[ "${need_update}" -eq 1 ]]; then
    umask 0077
    printf '%s' "${password}" > "${SECRET_FILE}.tmp"
    mv -f "${SECRET_FILE}.tmp" "${SECRET_FILE}"
    chmod 0640 "${SECRET_FILE}"
    chown 472:472 "${SECRET_FILE}" 2>/dev/null || true
    echo "Wrote password file from ${SSM_PARAM}"
fi

# Also reset the admin user's DB password if (a) Grafana is running and
# (b) we haven't already applied this exact password. The .last-applied
# file records the SHA-256 of the last password we successfully applied
# so we don't re-run grafana-cli on every tick.
current_hash=$(printf '%s' "${password}" | sha256sum | cut -d' ' -f1)
last_hash=""
[[ -f "${LAST_APPLIED}" ]] && last_hash=$(cat "${LAST_APPLIED}")

if [[ "${current_hash}" != "${last_hash}" ]]; then
    if docker ps --format '{{.Names}}' | grep -qx grafana; then
        echo "Applying password change via grafana-cli"
        if printf '%s' "${password}" | \
            docker exec -i grafana grafana cli admin reset-admin-password --password-from-stdin >/dev/null 2>&1; then
            printf '%s' "${current_hash}" > "${LAST_APPLIED}"
            chmod 0600 "${LAST_APPLIED}"
            echo "Admin password updated in Grafana DB"
        else
            # Fall back to deprecated-but-still-working 'grafana-cli'.
            if printf '%s' "${password}" | \
                docker exec -i grafana grafana-cli admin reset-admin-password --password-from-stdin >/dev/null 2>&1; then
                printf '%s' "${current_hash}" > "${LAST_APPLIED}"
                chmod 0600 "${LAST_APPLIED}"
                echo "Admin password updated in Grafana DB (grafana-cli fallback)"
            else
                echo "WARN: grafana cli reset failed; will retry next tick" >&2
            fi
        fi
    else
        echo "Grafana container not running; skipping DB update (will retry next tick)"
    fi
fi
