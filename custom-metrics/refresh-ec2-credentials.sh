#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Fetches IAM role credentials from IMDS and writes them to a file that
# Prometheus can read for ec2_sd_configs. Required because ParallelCluster's
# Imds.Secured=true restricts IMDS access to specific UIDs (root, ssm-user,
# etc.) — containers running as non-root can't reach IMDS directly.
#
# Runs as root on the host via systemd timer (every 5 minutes).
# Credentials are written to /run/prometheus-ec2-creds/credentials
# which is bind-mounted into the Prometheus container.
#
set -euo pipefail

CREDS_DIR="/run/prometheus-ec2-creds"
CREDS_FILE="${CREDS_DIR}/credentials"
mkdir -p "${CREDS_DIR}"
# World-readable dir/file (see chmod on the file below). The credentials
# are consumed by three containers running as different uids/gids:
#   prometheus          uid 65534 / gid 65534
#   cloudwatch-exporter uid 0     / gid 0
#   grafana             uid 472   / gid 0   (CloudWatch Logs datasource)
# No single owner/group satisfies all three, so we rely on world-read.
# This is acceptable here: the file lives only on tmpfs (never touches
# disk), holds short-lived IAM role credentials that are already
# retrievable from IMDS by any process on this single-tenant host, and
# the directory is not network-exposed.
chmod 0755 "${CREDS_DIR}"
chown root:root "${CREDS_DIR}"

# IMDSv2 token
TOKEN=$(curl -sS --max-time 5 -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 300")

# Get the role name
ROLE=$(curl -sS --max-time 5 -H "X-aws-ec2-metadata-token: ${TOKEN}" \
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/")

# Get the credentials JSON
CREDS_JSON=$(curl -sS --max-time 5 -H "X-aws-ec2-metadata-token: ${TOKEN}" \
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/${ROLE}")

# Write as AWS credentials file format
AWS_ACCESS_KEY_ID=$(echo "${CREDS_JSON}" | jq -r .AccessKeyId)
AWS_SECRET_ACCESS_KEY=$(echo "${CREDS_JSON}" | jq -r .SecretAccessKey)
AWS_SESSION_TOKEN=$(echo "${CREDS_JSON}" | jq -r .Token)

# Write atomically via tmp+rename so Prometheus never sees a partial file.
TMP_FILE="${CREDS_FILE}.tmp"
cat > "${TMP_FILE}" <<CRED
[default]
aws_access_key_id = ${AWS_ACCESS_KEY_ID}
aws_secret_access_key = ${AWS_SECRET_ACCESS_KEY}
aws_session_token = ${AWS_SESSION_TOKEN}
CRED

# World-readable (see dir comment above): consumed by prometheus,
# cloudwatch-exporter, and grafana running as different uids/gids.
# tmpfs-backed so creds never touch persistent disk.
chmod 0644 "${TMP_FILE}"
chown root:root "${TMP_FILE}"

# Detect whether the IMDS access key actually rotated (IMDS rotates role
# creds roughly every ~6h). Long-running consumers load the AWS credentials
# provider once at startup and cache it in memory, so when the underlying
# session token expires their AWS calls start failing until the process is
# restarted:
#   - cloudwatch-exporter / Grafana CloudWatch datasource — 403 "security
#     token expired"; Storage / Logs dashboards go blank.
#   - prometheus ec2_sd — the AWS SDK for Go used by ec2_sd_config caches the
#     shared-credentials file in-process and does NOT re-read it when the
#     token expires, so DescribeInstances returns "RequestExpired: Request has
#     expired" (misreported as a clock-skew error) and EC2 service discovery
#     stops finding compute nodes — node/GPU/EFA metrics silently disappear
#     while the login-local targets (slurm exporter, HeadNode) still look fine.
# So restart all three on key rotation, but only when the key actually changed,
# to avoid needless churn on every 5-minute tick.
OLD_KEY=""
if [[ -r "${CREDS_FILE}" ]]; then
    OLD_KEY=$(awk -F'= *' '/aws_access_key_id/{print $2; exit}' "${CREDS_FILE}" 2>/dev/null || echo "")
fi
mv -f "${TMP_FILE}" "${CREDS_FILE}"

if [[ "${AWS_ACCESS_KEY_ID}" != "${OLD_KEY}" && -n "${OLD_KEY}" ]]; then
    # Key rotated since last run — bounce the AWS-credential consumers so they
    # pick up the new credentials. Ignore errors (containers may not exist
    # on compute nodes; this script only does meaningful work on head/login).
    for c in prometheus cloudwatch-exporter grafana; do
        docker restart "${c}" >/dev/null 2>&1 || true
    done
fi
