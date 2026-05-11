#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Creates (or updates) a Cognito App Client on an existing User Pool
# configured for Grafana OAuth2 login, and prints the client_id +
# client_secret to stdout in KEY=VALUE form.
#
# Usage:
#   ./setup-grafana-client.sh <pool-id> <grafana-fqdn-or-ip> [region]
#
# Example:
#   ./setup-grafana-client.sh us-east-2_abc123 my-head-node.example.com us-east-2
#
# The <grafana-fqdn-or-ip> is how users will reach Grafana in their
# browser. Usually the HeadNode's DNS name, public IP, or localhost
# (when using SSM port-forward).
#
set -euo pipefail

POOL_ID="${1:?usage: $0 <pool-id> <grafana-fqdn> [region]}"
GRAFANA_HOST="${2:?usage: $0 <pool-id> <grafana-fqdn> [region]}"
REGION="${3:-${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}}"

# Grafana's OAuth callback URL. Use https by default; include http variant
# for SSM port-forward / local testing.
CALLBACK_URLS=(
    "https://${GRAFANA_HOST}/grafana/login/generic_oauth"
    "http://${GRAFANA_HOST}/grafana/login/generic_oauth"
)
LOGOUT_URLS=(
    "https://${GRAFANA_HOST}/grafana"
    "http://${GRAFANA_HOST}/grafana"
)

CLIENT_NAME="aws-parallelcluster-monitoring-grafana"

# Create the client. --generate-secret makes Cognito issue a random secret
# that we'll retrieve via describe-user-pool-client.
client_id=$(aws cognito-idp create-user-pool-client \
    --region "${REGION}" \
    --user-pool-id "${POOL_ID}" \
    --client-name "${CLIENT_NAME}" \
    --generate-secret \
    --allowed-o-auth-flows code \
    --allowed-o-auth-scopes openid email profile \
    --allowed-o-auth-flows-user-pool-client \
    --callback-urls "${CALLBACK_URLS[@]}" \
    --logout-urls "${LOGOUT_URLS[@]}" \
    --supported-identity-providers COGNITO \
    --explicit-auth-flows ALLOW_USER_SRP_AUTH ALLOW_REFRESH_TOKEN_AUTH \
    --query 'UserPoolClient.ClientId' \
    --output text 2>/dev/null) || {
    echo "ERROR: could not create user pool client" >&2
    exit 1
}

# Fetch the secret (create doesn't return it in the same call)
client_secret=$(aws cognito-idp describe-user-pool-client \
    --region "${REGION}" \
    --user-pool-id "${POOL_ID}" \
    --client-id "${client_id}" \
    --query 'UserPoolClient.ClientSecret' \
    --output text)

# Get the pool's Cognito-hosted domain (or custom domain if set).
pool_domain=$(aws cognito-idp describe-user-pool \
    --region "${REGION}" \
    --user-pool-id "${POOL_ID}" \
    --query 'UserPool.CustomDomain || UserPool.Domain' \
    --output text)
[[ "${pool_domain}" == "None" || -z "${pool_domain}" ]] && {
    echo "ERROR: user pool ${POOL_ID} has no hosted UI domain." >&2
    echo "Create one first: aws cognito-idp create-user-pool-domain --user-pool-id ${POOL_ID} --domain <some-unique-name>" >&2
    exit 1
}

# Print KEY=VALUE for easy eval / source.
cat <<KV
COGNITO_CLIENT_ID=${client_id}
COGNITO_CLIENT_SECRET=${client_secret}
COGNITO_DOMAIN=${pool_domain}
COGNITO_REGION=${REGION}
COGNITO_POOL_ID=${POOL_ID}
KV
