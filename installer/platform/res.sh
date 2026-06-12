#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Amazon RES (Research and Engineering Studio) platform module.
#
# RES VDI desktops are monitored as plain compute nodes: they run
# node_exporter (+ dcgm-exporter on GPU instances like g5/g6). The
# monitoring server (Prometheus/Grafana) does NOT live on a RES node — it
# runs on the ParallelCluster head node, which discovers these desktops via
# their res:EnvironmentName tag (see prometheus/prometheus-res.yml).
#
# A RES node is detected either by an explicit RES_ENVIRONMENT_NAME env var
# (set in the RES project launch script) or by the res:EnvironmentName
# instance tag exposed through IMDS (requires InstanceMetadataTags=enabled).
#

_load_res() {
    export PLATFORM="res"
    # RES desktops are always monitored as compute nodes.
    export PLATFORM_NODE_TYPE="compute"

    # Environment name: explicit env var wins, else read the res:EnvironmentName
    # instance tag from IMDS.
    local env_name="${RES_ENVIRONMENT_NAME:-}"
    if [[ -z "${env_name}" ]]; then
        env_name="$(imds_res_environment_name || true)"
    fi
    export PLATFORM_CLUSTER_NAME="${env_name:-res}"

    # Region from IMDS.
    local token region
    token=$(curl -sf -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
        http://169.254.169.254/latest/api/token 2>/dev/null) || token=""
    region=$(curl -sf -H "X-aws-ec2-metadata-token: ${token}" \
        http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null) || region=""
    export PLATFORM_REGION="${region}"

    # Local user: Ubuntu-based desktops use 'ubuntu', AL2023 uses 'ec2-user'.
    local cluster_user="ec2-user"
    if [[ -d /home/ubuntu ]] && id ubuntu >/dev/null 2>&1; then
        cluster_user="ubuntu"
    fi
    export PLATFORM_USER="${cluster_user}"
}
