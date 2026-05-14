#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Cluster cost estimator. Runs every minute via cron.
# Pushes cost metrics to the local pushgateway. Works on ParallelCluster
# (legacy cfnconfig path) and PCS (IMDS tag discovery).
#
# Metrics:
#   cluster_cost_per_hour{component="compute|headnode|ebs|total"}
#   cluster_cost_accumulated  (running total since cluster start)
#
set -uo pipefail

# Platform detection
if [[ -r /etc/parallelcluster/cfnconfig ]]; then
    # shellcheck disable=SC1091
    . /etc/parallelcluster/cfnconfig
    cluster_tag_key="parallelcluster:cluster-name"
    # shellcheck disable=SC2154
    cluster_name="${stack_name}"
    # shellcheck disable=SC2154
    region="${cfn_region}"
else
    _tok=$(curl -sf -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
        http://169.254.169.254/latest/api/token 2>/dev/null)
    # PCS-managed compute nodes get aws:pcs:cluster-id; login fleets
    # launched outside PCS use the pcs-cluster-id mirror tag (PCS reserves
    # the aws: prefix). See installer/platform/pcs.sh for the same logic.
    cluster_name=$(curl -sf -H "X-aws-ec2-metadata-token: $_tok" \
        http://169.254.169.254/latest/meta-data/tags/instance/aws:pcs:cluster-id 2>/dev/null) \
        || cluster_name=$(curl -sf -H "X-aws-ec2-metadata-token: $_tok" \
        http://169.254.169.254/latest/meta-data/tags/instance/pcs-cluster-id 2>/dev/null)
    region=$(curl -sf -H "X-aws-ec2-metadata-token: $_tok" \
        http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null)
    cluster_tag_key="aws:pcs:cluster-id"
fi
export AWS_DEFAULT_REGION="${region}"

CACHE_DIR="/var/lib/prometheus/cost-cache"
PRICE_CACHE="${CACHE_DIR}/prices.env"
ACCUMULATOR="${CACHE_DIR}/accumulated"
PUSHGW="http://127.0.0.1:9091/metrics/job/cost"

mkdir -p "${CACHE_DIR}"

# ─── Price cache ─────────────────────────────────────────────────────────────
fetch_price() {
    local itype="$1"
    aws pricing get-products --region us-east-1 --service-code AmazonEC2 \
        --filters "Type=TERM_MATCH,Field=instanceType,Value=${itype}" \
                  "Type=TERM_MATCH,Field=regionCode,Value=${region}" \
                  'Type=TERM_MATCH,Field=preInstalledSw,Value=NA' \
                  'Type=TERM_MATCH,Field=operatingSystem,Value=Linux' \
                  'Type=TERM_MATCH,Field=tenancy,Value=Shared' \
                  'Type=TERM_MATCH,Field=capacitystatus,Value=Used' \
        --query 'PriceList[0]' --output text 2>/dev/null \
        | jq -r '.terms.OnDemand | to_entries[0].value.priceDimensions | to_entries[0].value.pricePerUnit.USD' 2>/dev/null || echo "0"
}

build_price_cache() {
    local head_type
    # /var/lib/cloud/data/instance-type doesn't exist on AL2023.
    # This script runs as root, so IMDS is accessible (Imds.Secured allows root).
    head_type=$(cat /var/lib/cloud/data/instance-type 2>/dev/null ||         curl -sf -H "X-aws-ec2-metadata-token: $(curl -sf -X PUT http://169.254.169.254/latest/api/token -H X-aws-ec2-metadata-token-ttl-seconds:60)"         http://169.254.169.254/latest/meta-data/instance-type 2>/dev/null || echo "unknown")

    # Get compute instance types currently running
    local compute_types
    compute_types=$(aws ec2 describe-instances --region "${region}" \
        --filters "Name=tag:${cluster_tag_key},Values=${cluster_name}" \
                  "Name=tag:Name,Values=Compute" \
                  "Name=instance-state-name,Values=running" \
        --query 'Reservations[].Instances[].InstanceType' --output text 2>/dev/null \
        | tr '\t' '\n' | sort -u)

    : > "${PRICE_CACHE}.tmp"
    for itype in $(echo "${head_type} ${compute_types}" | tr ' ' '\n' | sort -u | grep -v '^$'); do
        local price
        price=$(fetch_price "${itype}")
        [[ -z "${price}" || "${price}" == "null" ]] && price="0"
        echo "${itype}=${price}" >> "${PRICE_CACHE}.tmp"
    done
    echo "HEAD_TYPE=${head_type}" >> "${PRICE_CACHE}.tmp"
    mv -f "${PRICE_CACHE}.tmp" "${PRICE_CACHE}"
}

# Build cache if missing or older than 24h
if [[ ! -f "${PRICE_CACHE}" ]] || [[ -n "$(find "${PRICE_CACHE}" -mmin +1440 2>/dev/null)" ]]; then
    build_price_cache
fi

# ─── Read prices ─────────────────────────────────────────────────────────────
get_price() {
    grep "^${1}=" "${PRICE_CACHE}" 2>/dev/null | cut -d= -f2 || echo "0"
}

HEAD_TYPE=$(grep "^HEAD_TYPE=" "${PRICE_CACHE}" 2>/dev/null | cut -d= -f2 || echo "")
head_price=$(get_price "${HEAD_TYPE}")

# ─── Compute fleet cost ──────────────────────────────────────────────────────
compute_cost=0
while IFS=$'\t' read -r itype count; do
    [[ -z "${itype}" ]] && continue
    price=$(get_price "${itype}")
    cost=$(echo "scale=4; ${count} * ${price}" | bc 2>/dev/null || echo "0")
    compute_cost=$(echo "scale=4; ${compute_cost} + ${cost}" | bc 2>/dev/null || echo "0")
done < <(aws ec2 describe-instances --region "${region}" \
    --filters "Name=tag:${cluster_tag_key},Values=${cluster_name}" \
              "Name=tag:Name,Values=Compute" \
              "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[].InstanceType' --output text 2>/dev/null \
    | tr '\t' '\n' | sort | uniq -c | awk '{print $2"\t"$1}')

# ─── EBS cost (estimate: gp3 ~$0.08/GB/month = $0.000111/GB/hour) ────────────
ebs_gb_hour="0.000111"
num_instances=$(aws ec2 describe-instances --region "${region}" \
    --filters "Name=tag:${cluster_tag_key},Values=${cluster_name}" \
              "Name=instance-state-name,Values=running" \
    --query 'length(Reservations[].Instances[])' --output text 2>/dev/null || echo "1")
ebs_cost=$(echo "scale=4; ${num_instances} * 35 * ${ebs_gb_hour}" | bc 2>/dev/null || echo "0")

# ─── PCS controller cost (managed service, fixed rate) ───────────────────────
# PCS charges $0.59/hr for the SMALL cluster controller regardless of node count.
# Only applies when running on PCS (not ParallelCluster).
pcs_controller_cost=0
if [[ "${cluster_tag_key}" == "aws:pcs:cluster-id" ]]; then
    pcs_controller_cost="0.59"
fi

# ─── Total ────────────────────────────────────────────────────────────────────
total_cost=$(echo "scale=4; ${head_price:-0} + ${compute_cost} + ${ebs_cost} + ${pcs_controller_cost}" | bc 2>/dev/null || echo "0")

# ─── Accumulator (adds cost/60 each minute) ──────────────────────────────────
accumulated=$(cat "${ACCUMULATOR}" 2>/dev/null || echo "0")
increment=$(echo "scale=6; ${total_cost} / 60" | bc 2>/dev/null || echo "0")
accumulated=$(echo "scale=4; ${accumulated} + ${increment}" | bc 2>/dev/null || echo "0")
echo "${accumulated}" > "${ACCUMULATOR}"

# ─── Push ─────────────────────────────────────────────────────────────────────
cat <<METRICS | curl --silent --data-binary @- "${PUSHGW}"
# HELP cluster_cost_per_hour Estimated cluster cost in USD per hour
# TYPE cluster_cost_per_hour gauge
cluster_cost_per_hour{component="headnode"} ${head_price:-0}
cluster_cost_per_hour{component="compute"} ${compute_cost}
cluster_cost_per_hour{component="ebs"} ${ebs_cost}
cluster_cost_per_hour{component="pcs_controller"} ${pcs_controller_cost}
cluster_cost_per_hour{component="total"} ${total_cost}
# HELP cluster_cost_accumulated Estimated total cluster cost in USD since start
# TYPE cluster_cost_accumulated gauge
cluster_cost_accumulated ${accumulated}
METRICS
