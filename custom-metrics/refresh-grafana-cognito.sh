#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Writes Grafana's sign-in settings from the optional Cognito SSO parameter
# in SSM Parameter Store (see cognito/README.md) to tmpfs:
#   /run/grafana-secrets/cognito.env
#       GF_SERVER_ROOT_URL, the URL that users open Grafana at, and, when
#       Cognito is configured, the OAuth2 settings. compose/head.yml loads
#       this file when it creates the grafana container.
#   /run/grafana-secrets/cognito-client-secret
#       The app client secret. The Grafana image's entrypoint reads it
#       through GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET__FILE each time the
#       container starts.
#
# installer/install.sh runs this script before it starts the stack, and
# grafana-cognito-refresh.service runs it at every boot, because /run is
# empty after a reboot. Docker starts Grafana at boot before the secret is
# back, so the script restarts Grafana when Grafana started before the
# secret file last changed.
#
# Environment (/etc/aws-parallelcluster-monitoring/grafana-cognito.env,
# written by installer/install.sh):
#   COGNITO_SSM_PARAM   /<platform>/<cluster>/grafana/cognito
#   REGION              the cluster's region
#
# A missing parameter means no Cognito: exit 0. Any other read error, and an
# invalid value, exit 1. In all three cases cognito.env holds only the
# default root URL, so Grafana offers only the local admin login once the
# installer recreates its container.
#
# The script handles the client secret: run it directly, never under
# `bash -x`.
#
set -euo pipefail
umask 0077

: "${COGNITO_SSM_PARAM:?COGNITO_SSM_PARAM is not set}"
: "${REGION:?REGION is not set}"

SECRET_DIR="/run/grafana-secrets"
ENV_FILE="${SECRET_DIR}/cognito.env"
SECRET_FILE="${SECRET_DIR}/cognito-client-secret"
# The SSM port forwarding URL in the README (local port 8443).
DEFAULT_ROOT_URL="https://localhost:8443/grafana/"
# Grafana runs as UID 472 inside its container.
GRAFANA_UID=472

mkdir -p "${SECRET_DIR}"
chmod 0750 "${SECRET_DIR}"
chown "${GRAFANA_UID}:${GRAFANA_UID}" "${SECRET_DIR}"

# Write stdin to $1 atomically, mode 0640, owned by Grafana's user. mktemp
# creates a new file, so a link left in the directory can't redirect the
# write.
install_file() {
    local tmp
    tmp="$(mktemp "$1.XXXXXX")"
    cat > "${tmp}"
    chmod 0640 "${tmp}"
    chown "${GRAFANA_UID}:${GRAFANA_UID}" "${tmp}"
    mv -f "${tmp}" "$1"
}

# write_env <root-url> [<VAR=value>...]
write_env() {
    local root_url="$1"
    shift
    {
        printf '# Written by %s from %s; compose/head.yml loads it.\n' "${0##*/}" "${COGNITO_SSM_PARAM}"
        printf 'GF_SERVER_ROOT_URL=%s\n' "${root_url}"
        [[ $# -eq 0 ]] || printf '%s\n' "$@"
    } | install_file "${ENV_FILE}"
}

# Local admin login only: the default root URL and no OAuth2 settings.
without_cognito() {
    write_env "${DEFAULT_ROOT_URL}"
    rm -f "${SECRET_FILE}"
}

fail() {
    echo "ERROR: ${COGNITO_SSM_PARAM}: $*" >&2
    without_cognito
    exit 1
}

err_file="$(mktemp)"
trap 'rm -f "${err_file}"' EXIT
if ! json="$(aws ssm get-parameter --region "${REGION}" --name "${COGNITO_SSM_PARAM}" \
        --with-decryption --query 'Parameter.Value' --output text 2>"${err_file}")"; then
    if grep -q 'ParameterNotFound' "${err_file}"; then
        echo "No Cognito config in ${COGNITO_SSM_PARAM}; using local admin login only"
        without_cognito
        exit 0
    fi
    cat "${err_file}" >&2
    fail "could not read the parameter"
fi

# The value goes to jq through a pipe: a here-string can be a temp file.
printf '%s' "${json}" | jq -e 'type == "object"' >/dev/null 2>&1 \
    || fail "the value is not a JSON object"
field() {
    printf '%s' "${json}" | jq -r --arg k "$1" '.[$k] // "" | tostring'
}
client_id="$(field client_id)"
client_secret="$(field client_secret)"
domain="$(field domain)"
region="$(field region)"
region="${region:-${REGION}}"
allowed_domains="$(field allowed_domains)"
root_url="$(field root_url)"
root_url="${root_url:-${DEFAULT_ROOT_URL}}"
unset json

[[ -n "${client_id}" && -n "${client_secret}" && -n "${domain}" ]] \
    || fail "client_id, client_secret and domain are required"
re='^[A-Za-z0-9_+]+$'
[[ "${client_id}" =~ ${re} ]] || fail "client_id is not an app client ID"
re='^[a-z0-9-]+$'
[[ "${region}" =~ ${re} ]] || fail "region is not an AWS Region name"
re='^[A-Za-z0-9., _-]*$'
[[ "${allowed_domains}" =~ ${re} ]] \
    || fail "allowed_domains must be domain names separated by commas or spaces"
# domain: the prefix of a prefix domain, or the host name of a prefix or
# custom domain. Prefixes can't contain dots.
domain="${domain#https://}"
domain="${domain%/}"
re='^[A-Za-z0-9.-]+$'
[[ "${domain}" =~ ${re} ]] || fail "domain must be the user pool domain's prefix or host name"
if [[ "${domain}" == *.* ]]; then
    auth_host="${domain}"
else
    auth_host="${domain}.auth.${region}.amazoncognito.com"
fi
[[ "${root_url}" == */ ]] || root_url="${root_url}/"
re='^https://[A-Za-z0-9.-]+(:[0-9]+)?/grafana/$'
[[ "${root_url}" =~ ${re} ]] || fail "root_url must look like https://<host>[:<port>]/grafana/"

echo "Cognito SSO config found in ${COGNITO_SSM_PARAM}; enabling OAuth2"
# Rewrite the secret only when it changed: its modification time tells
# below whether the running Grafana has the current secret.
current_secret=""
[[ -f "${SECRET_FILE}" ]] && current_secret="$(cat "${SECRET_FILE}")"
if [[ ! -f "${SECRET_FILE}" || "${current_secret}" != "${client_secret}" ]]; then
    printf '%s' "${client_secret}" | install_file "${SECRET_FILE}"
fi
unset client_secret current_secret
# Cognito's /logout needs logout_uri, which must be a sign-out URL of the
# app client; cognito/setup-grafana-client.sh registers the root URL.
logout_uri="$(jq -rn --arg u "${root_url}" '$u | @uri')"
write_env "${root_url}" \
    "GF_AUTH_GENERIC_OAUTH_ENABLED=true" \
    "GF_AUTH_GENERIC_OAUTH_NAME=Cognito" \
    "GF_AUTH_GENERIC_OAUTH_ALLOW_SIGN_UP=true" \
    "GF_AUTH_GENERIC_OAUTH_CLIENT_ID=${client_id}" \
    "GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET__FILE=${SECRET_FILE}" \
    "GF_AUTH_GENERIC_OAUTH_SCOPES=openid email profile" \
    "GF_AUTH_GENERIC_OAUTH_AUTH_URL=https://${auth_host}/oauth2/authorize" \
    "GF_AUTH_GENERIC_OAUTH_TOKEN_URL=https://${auth_host}/oauth2/token" \
    "GF_AUTH_GENERIC_OAUTH_API_URL=https://${auth_host}/oauth2/userInfo" \
    "GF_AUTH_GENERIC_OAUTH_ALLOWED_DOMAINS=${allowed_domains}" \
    "GF_AUTH_SIGNOUT_REDIRECT_URL=https://${auth_host}/logout?client_id=${client_id}&logout_uri=${logout_uri}"
echo "Cognito env file written to ${ENV_FILE}"
echo "Grafana root URL ${root_url}, Cognito domain ${auth_host}"

# Grafana reads the secret file only when its container starts.
state="$(docker inspect -f '{{.State.Running}} {{.State.StartedAt}}' grafana 2>/dev/null)" || exit 0
[[ "${state%% *}" == true ]] || exit 0
started="$(date -d "${state#* }" +%s 2>/dev/null)" || started=0
if (( $(stat -c %Y "${SECRET_FILE}") >= started )); then
    echo "Restarting Grafana: it started before ${SECRET_FILE} last changed"
    docker restart grafana >/dev/null
fi
