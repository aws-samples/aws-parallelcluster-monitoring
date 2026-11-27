#!/usr/bin/env bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Static checks of the configuration files that installer/install.sh
# deploys, run with the images that compose/head.yml pins:
#
#   prometheus   promtool check config on prometheus.yml and
#                prometheus-pcs.yml, each rendered as install.sh renders it,
#                with and without the RES snippet; promtool check rules on
#                prometheus/rules/; promtool test rules on every rule unit
#                test file; promtool check rules on the dashboards' queries
#   compose      docker compose config on the rendered compose files, and
#                the head node's listen addresses and credential-group grants
#   nginx        nginx -t on nginx/conf.d
#   cloudwatch   cloudwatch-exporter --check-config on the rendered config
#
# Usage: .github/ci/check-configs.sh CHECK...   (from the repository root)
# Needs docker. The compose check runs $COMPOSE (default: docker compose).
set -euo pipefail

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
# Values for the install-time placeholders.
REGION=us-east-1
PC_CLUSTER_NAME=ci-pc
PCS_CLUSTER_ID=pcs_ci00000001
SLURMCTLD_IP=10.0.0.10
LOGIN_INSTANCE_ID=i-0123456789abcdef0
RES_ENVIRONMENT_NAME=ci-res
MONITORING_HOME=/opt/aws-parallelcluster-monitoring
CREDS_GID=990

fail() {
    echo "ERROR $*" >&2
    exit 1
}

# The image that compose/head.yml pins for a service, pulled unless it's
# present. A registry can refuse an anonymous pull for a while, so the pull
# is tried three times.
image_of() {
    local image try
    image="$(awk -v svc="  $1:" '
        $0 == svc { found = 1; next }
        found && /^([^ #]|  [^ #])/ { exit }
        found && $1 == "image:" { gsub(/["\047]/, "", $2); print $2; exit }' compose/head.yml)"
    [[ -n "${image}" ]] || fail "no image for service $1 in compose/head.yml"
    if ! docker image inspect "${image}" >/dev/null 2>&1; then
        for try in 1 2 3; do
            docker pull -q "${image}" >&2 && break
            [[ "${try}" == 3 ]] && fail "could not pull ${image}"
            sleep 20
        done
    fi
    echo "${image}"
}

no_placeholders() {
    if grep -n -E '__[A-Z][A-Z0-9_]*__' "$@"; then
        fail "a placeholder is left in the rendered file above"
    fi
}

check_prometheus() {
    local image platform res f n out rc
    local -a promtool tests
    image="$(image_of prometheus)"
    echo "== promtool from ${image}"
    # The configs load their rule files from /etc/prometheus/rules, where
    # compose/head.yml mounts prometheus/rules.
    promtool=(docker run --rm --network none --user "$(id -u):$(id -g)"
              -v "${PWD}:/repo:ro" -v "${TMP}:/tmp/ci:ro"
              -v "${PWD}/prometheus/rules:/etc/prometheus/rules:ro"
              -w /repo --entrypoint promtool "${image}")
    "${promtool[@]}" --version

    # The order of install.sh: platform placeholders, then the RES snippet,
    # then the region.
    for platform in pc pcs; do
        for res in "" -res; do
            f="${TMP}/${platform}${res}.yml"
            if [[ "${platform}" == pc ]]; then
                sed "s|__PC_CLUSTER_NAME__|${PC_CLUSTER_NAME}|g" prometheus/prometheus.yml > "${f}"
            else
                sed -e "s|__SLURMCTLD_IP__|${SLURMCTLD_IP}|g" -e "s|__PCS_CLUSTER_ID__|${PCS_CLUSTER_ID}|g" \
                    -e "s|__LOGIN_INSTANCE_ID__|${LOGIN_INSTANCE_ID}|g" prometheus/prometheus-pcs.yml > "${f}"
            fi
            if [[ -n "${res}" ]]; then
                sed "s|__RES_ENVIRONMENT_NAME__|${RES_ENVIRONMENT_NAME}|g" prometheus/prometheus-res.yml >> "${f}"
            fi
            sed -i "s/__AWS_REGION__/${REGION}/g" "${f}"
            no_placeholders "${f}"
            echo "== promtool check config: ${platform}${res:+ with the RES snippet}"
            "${promtool[@]}" check config "/tmp/ci/${f##*/}"
        done
    done

    echo "== promtool check rules prometheus/rules/*.yml"
    "${promtool[@]}" check rules prometheus/rules/*.yml

    mapfile -t tests < <(git grep -l -E '^tests:' -- '*.yml' '*.yaml')
    [[ ${#tests[@]} -gt 0 ]] || fail "no rule unit test files found"
    echo "== promtool test rules ${tests[*]}"
    "${promtool[@]}" test rules "${tests[@]}"

    echo "== promtool check rules on the dashboards' queries"
    n="$(python3 .github/ci/dashboard-promql.py "${TMP}/dashboards.yml")"
    rc=0
    out="$("${promtool[@]}" check rules /tmp/ci/dashboards.yml 2>&1)" || rc=$?
    printf '%s\n' "${out}"
    if [[ "${rc}" != 0 ]]; then
        # Show the dashboard and panel of each rejected query.
        grep -o -E 'dashboard:[0-9]+' <<<"${out}" | sort -u | while read -r rule; do
            grep -B 1 -A 1 -F "record: \"${rule}\"" "${TMP}/dashboards.yml"
        done
        fail "promtool rejected a dashboard query"
    fi
    grep -q -F "SUCCESS: ${n} rules found" <<<"${out}" || fail "expected ${n} dashboard queries in promtool's output"
}

check_compose() {
    local f
    local -a compose
    read -r -a compose <<<"${COMPOSE:-docker compose}"
    "${compose[@]}" version
    for f in head compute compute.gpu; do
        sed -e "s|__MONITORING_HOME__|${MONITORING_HOME}|g" -e "s|__CREDS_GID__|${CREDS_GID}|g" \
            "compose/${f}.yml" > "${TMP}/${f}.yml"
        no_placeholders "${TMP}/${f}.yml"
        echo "== docker compose config: compose/${f}.yml"
        # install.sh exports DCGM_EXPORTER_IMAGE, empty unless the user sets
        # it. head.yml's env_file exists only on the node, so environment
        # files aren't read.
        DCGM_EXPORTER_IMAGE="" "${compose[@]}" -f "${TMP}/${f}.yml" -p ci config \
            --no-env-resolution --format json > "${TMP}/${f}.json"
    done
    expect() { # expect FILE DESCRIPTION JQ-FILTER: the filter must print true
        if [[ "$(jq -r "$3" "${TMP}/$1.json")" == true ]]; then
            echo "ok   $1: $2"
        else
            fail "$1: $2"
        fi
    }
    echo "== head node: listen addresses and the credentials group"
    expect head "prometheus listens on 127.0.0.1:9090" \
        '.services.prometheus.command | index("--web.listen-address=127.0.0.1:9090") != null'
    expect head "pushgateway listens on 127.0.0.1:9091" \
        '.services.pushgateway.command | index("--web.listen-address=127.0.0.1:9091") != null'
    expect head "grafana listens on 127.0.0.1" \
        '.services.grafana.environment.GF_SERVER_HTTP_ADDR == "127.0.0.1"'
    expect head "cloudwatch-exporter is published on 127.0.0.1:9106 only, off the host network" \
        '(.services["cloudwatch-exporter"].ports | map({host_ip, target, published})) == [{"host_ip":"127.0.0.1","target":9106,"published":"9106"}] and .services["cloudwatch-exporter"].network_mode == null'
    expect head "only cloudwatch-exporter, grafana and prometheus get the credentials group" \
        "[.services | to_entries[] | select((.value.group_add // []) | index(\"${CREDS_GID}\") != null) | .key] | sort == [\"cloudwatch-exporter\",\"grafana\",\"prometheus\"]"
    expect compute.gpu "the default dcgm-exporter image is pinned by digest" \
        '.services["dcgm-exporter"].image | test("@sha256:[0-9a-f]{64}$")'
}

check_nginx() {
    local image
    image="$(image_of nginx)"
    mkdir -p "${TMP}/conf.d" "${TMP}/ssl"
    cp nginx/conf.d/*.conf "${TMP}/conf.d/"
    # install.sh generates the certificate at install.
    openssl req -new -x509 -nodes -newkey rsa:2048 -days 1 -subj /CN=localhost \
        -keyout "${TMP}/ssl/nginx.key" -out "${TMP}/ssl/nginx.crt" 2>/dev/null
    # nginx reads its configuration as root, and the files are copies.
    chmod -R a+rX "${TMP}"
    echo "== nginx -t on ${image}"
    docker run --rm --network none -v "${TMP}/conf.d:/etc/nginx/conf.d:ro" -v "${TMP}/ssl:/etc/ssl:ro" \
        "${image}" nginx -t
}

check_cloudwatch() {
    local image out
    image="$(image_of cloudwatch-exporter)"
    sed "s/__AWS_REGION__/${REGION}/g" cloudwatch-exporter/config.yml > "${TMP}/cloudwatch.yml"
    no_placeholders "${TMP}/cloudwatch.yml"
    chmod a+r "${TMP}/cloudwatch.yml"
    echo "== cloudwatch-exporter --check-config on ${image}"
    out="$(docker run --rm --network none -v "${TMP}/cloudwatch.yml:/config/config.yml:ro" \
        --entrypoint java "${image}" -jar /cloudwatch_exporter.jar --check-config /config/config.yml 2>&1)"
    printf '%s\n' "${out}"
    [[ "$(tail -n 1 <<<"${out}")" == OK ]] || fail "cloudwatch-exporter rejected cloudwatch-exporter/config.yml"
}

[[ $# -gt 0 ]] || fail "usage: $0 prometheus|compose|nginx|cloudwatch..."
for check in "$@"; do
    case "${check}" in
        prometheus|compose|nginx|cloudwatch) "check_${check}" ;;
        *) fail "unknown check ${check}" ;;
    esac
done
