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
chmod 0750 "${CREDS_DIR}"
chown root:65534 "${CREDS_DIR}"

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

# Restrict to root + the container's UID (nobody/65534 on our images).
# Directory is also tmpfs-backed so creds never touch persistent disk.
chmod 0640 "${TMP_FILE}"
chown root:65534 "${TMP_FILE}"
mv -f "${TMP_FILE}" "${CREDS_FILE}"
