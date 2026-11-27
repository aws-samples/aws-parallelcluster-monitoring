#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Creates a Cognito app client for Grafana's OAuth2 sign-in on an existing
# user pool, and prints the values that the Cognito parameter in SSM needs,
# as KEY=VALUE lines on stdout (see cognito/README.md).
#
# Usage:
#   ./setup-grafana-client.sh <pool-id> <grafana-host> [region]
#
# <grafana-host> is the host, with the port unless it is 443, of the URL
# that users open Grafana at:
#   localhost:8443        SSM port forwarding, as in the README
#   grafana.example.com   the ALB of docs/public-access.md
#
# The app client gets the callback URL
# https://<grafana-host>/grafana/login/generic_oauth and the sign-out URL
# https://<grafana-host>/grafana/. Cognito accepts http:// callback URLs
# only for localhost, and nginx serves Grafana over HTTPS only, so the
# script registers https:// URLs only.
#
# Example:
#   ./setup-grafana-client.sh us-east-1_ABC123 localhost:8443 us-east-1
#
set -euo pipefail

usage="usage: $0 <pool-id> <grafana-host> [region]"
POOL_ID="${1:?${usage}}"
GRAFANA_HOST="${2:?${usage}}"
REGION="${3:-${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}}"

re='^[A-Za-z0-9.-]+(:[0-9]+)?$'
if [[ ! "${GRAFANA_HOST}" =~ ${re} ]]; then
    echo "ERROR: <grafana-host> must be a host name with an optional :<port>, such as localhost:8443 or grafana.example.com, not '${GRAFANA_HOST}'" >&2
    exit 1
fi
ROOT_URL="https://${GRAFANA_HOST}/grafana/"
CALLBACK_URL="${ROOT_URL}login/generic_oauth"
CLIENT_NAME="aws-parallelcluster-monitoring-grafana"

# The hosted sign-in pages need a user pool domain: check it before the
# client is created. A custom domain wins over a prefix domain, as in
# Cognito's OIDC discovery.
domains="$(aws cognito-idp describe-user-pool --region "${REGION}" --user-pool-id "${POOL_ID}" \
    --query '[UserPool.CustomDomain, UserPool.Domain]' --output text)" || {
    echo "ERROR: could not read user pool ${POOL_ID} in ${REGION} (see the error above)" >&2
    exit 1
}
read -r custom_domain prefix_domain <<<"${domains}"
if [[ -n "${custom_domain}" && "${custom_domain}" != None ]]; then
    domain="${custom_domain}"
    auth_host="${custom_domain}"
elif [[ -n "${prefix_domain}" && "${prefix_domain}" != None ]]; then
    domain="${prefix_domain}"
    auth_host="${prefix_domain}.auth.${REGION}.amazoncognito.com"
else
    echo "ERROR: user pool ${POOL_ID} has no domain, which the hosted sign-in pages need." >&2
    echo "Create one first: aws cognito-idp create-user-pool-domain --region ${REGION} --user-pool-id ${POOL_ID} --domain <some-unique-prefix>" >&2
    exit 1
fi
# 1 is the classic hosted UI, 2 is managed login.
login_version="$(aws cognito-idp describe-user-pool-domain --region "${REGION}" --domain "${domain}" \
    --query 'DomainDescription.ManagedLoginVersion' --output text)" || {
    echo "ERROR: could not read the user pool domain ${domain} (see the error above)" >&2
    exit 1
}

created="$(aws cognito-idp create-user-pool-client \
    --region "${REGION}" \
    --user-pool-id "${POOL_ID}" \
    --client-name "${CLIENT_NAME}" \
    --generate-secret \
    --allowed-o-auth-flows code \
    --allowed-o-auth-scopes openid email profile \
    --allowed-o-auth-flows-user-pool-client \
    --callback-urls "${CALLBACK_URL}" \
    --logout-urls "${ROOT_URL}" \
    --supported-identity-providers COGNITO \
    --explicit-auth-flows ALLOW_USER_SRP_AUTH ALLOW_REFRESH_TOKEN_AUTH \
    --query 'UserPoolClient.[ClientId, ClientSecret]' \
    --output text)" || {
    echo "ERROR: Cognito did not create the app client (see the error above)" >&2
    exit 1
}
read -r client_id client_secret <<<"${created}"
echo "Created app client ${client_id}: callback URL ${CALLBACK_URL}, sign-out URL ${ROOT_URL}" >&2

# Cognito serves managed login pages only to app clients that have a
# branding style, and gives none to clients created through the API.
if [[ "${login_version}" == 2 ]]; then
    if ! aws cognito-idp create-managed-login-branding --region "${REGION}" \
            --user-pool-id "${POOL_ID}" --client-id "${client_id}" \
            --use-cognito-provided-values >/dev/null; then
        echo "ERROR: Cognito did not create a managed login style for app client ${client_id} (see the error above)" >&2
        echo "Deleting app client ${client_id}, so that a re-run starts clean" >&2
        aws cognito-idp delete-user-pool-client --region "${REGION}" \
            --user-pool-id "${POOL_ID}" --client-id "${client_id}" >&2 \
            || echo "WARN: could not delete app client ${client_id}; delete it yourself" >&2
        exit 1
    fi
    echo "Domain ${domain} uses managed login: created a style with Cognito's defaults for the client" >&2
fi

# KEY=VALUE lines, for eval or source.
cat <<KV
COGNITO_CLIENT_ID=${client_id}
COGNITO_CLIENT_SECRET=${client_secret}
COGNITO_DOMAIN=${auth_host}
COGNITO_REGION=${REGION}
COGNITO_POOL_ID=${POOL_ID}
GRAFANA_ROOT_URL=${ROOT_URL}
KV
