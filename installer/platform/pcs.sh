#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# AWS Parallel Computing Service (PCS) platform module.
#
# Identifies a PCS node by the union of two tag sources:
#
# 1. Compute nodes launched by PCS itself carry these tags
#    (set by PCS, not user-controllable, only present on ng instances):
#      aws:pcs:cluster-id
#      aws:pcs:compute-node-group-id
#
# 2. Login nodes are typically launched outside PCS (a plain EC2 instance
#    against the cluster's SG), so they don't get the aws:pcs:* tags
#    automatically. The launch template should set:
#      monitoring-role=login
#      pcs-cluster-id=<id>          (mirror of aws:pcs:cluster-id)
#    The aws: tag namespace is reserved by AWS, hence the mirror.
#
# Reads cluster metadata (slurmctld endpoint) via the PCS API.
#

_load_pcs() {
    local token
    token=$(curl -sf -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' \
        http://169.254.169.254/latest/api/token 2>/dev/null) \
        || die "Could not get IMDS token"

    _imds() {
        curl -sf -H "X-aws-ec2-metadata-token: $token" \
            "http://169.254.169.254/latest/meta-data/$1"
    }

    # Try the PCS-managed tag first; fall back to the user-set mirror tag
    # for login nodes that were launched directly against the cluster.
    local cluster_id ng_id region node_type
    cluster_id=$(_imds "tags/instance/aws:pcs:cluster-id" 2>/dev/null) \
        || cluster_id=$(_imds "tags/instance/pcs-cluster-id" 2>/dev/null) \
        || die "Cannot detect PCS cluster: instance has neither aws:pcs:cluster-id (set by PCS on managed nodes) nor pcs-cluster-id (mirror tag for login nodes). Check MetadataOptions.InstanceMetadataTags=enabled on the launch template, and that the user-data sets pcs-cluster-id for login-fleet instances launched outside PCS."

    ng_id=$(_imds "tags/instance/aws:pcs:compute-node-group-id" 2>/dev/null || echo "")

    region=$(_imds "placement/region") || die "Could not read region"

    # Node type: 'monitoring-role' tag identifies which node runs the
    # full monitoring stack (login). Default: compute.
    node_type=$(_imds "tags/instance/monitoring-role" 2>/dev/null || echo "compute")

    # Slurmctld endpoint: needed by Prometheus to scrape native /metrics
    # on PCS. Discovered via PCS API on the login node.
    local slurmctld_ip=""
    if [[ "${node_type}" == "login" ]]; then
        slurmctld_ip=$(aws pcs get-cluster --region "${region}" \
            --cluster-identifier "${cluster_id}" \
            --query 'cluster.endpoints[?type==`SLURMCTLD`].privateIpAddress | [0]' \
            --output text 2>/dev/null) || die "Could not resolve slurmctld endpoint"
    fi

    # Detect the platform user. Ubuntu-based AMIs use 'ubuntu', AL2023 uses 'ec2-user'.
    local platform_user="ec2-user"
    if [[ -d /home/ubuntu ]] && id ubuntu >/dev/null 2>&1; then
        platform_user="ubuntu"
    fi

    export PLATFORM="pcs"
    export PLATFORM_NODE_TYPE="${node_type}"      # login | compute
    export PLATFORM_CLUSTER_NAME="${cluster_id}"
    export PLATFORM_REGION="${region}"
    export PLATFORM_USER="${platform_user}"

    # PCS-specific
    export PCS_CLUSTER_ID="${cluster_id}"
    export PCS_NODE_GROUP_ID="${ng_id}"
    export PCS_SLURMCTLD_IP="${slurmctld_ip}"
}
