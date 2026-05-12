#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# AWS Parallel Computing Service (PCS) platform module.
#
# Identifies a PCS node from IMDS instance tags:
#   aws:pcs:cluster-id
#   aws:pcs:compute-node-group-id
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

    # PCS identifies nodes via instance tags. Requires
    # MetadataOptions.InstanceMetadataTags=enabled on the launch template.
    local cluster_id ng_id region
    cluster_id=$(_imds "tags/instance/aws:pcs:cluster-id") \
        || die "Not a PCS node (missing aws:pcs:cluster-id tag — check MetadataOptions.InstanceMetadataTags=enabled on the launch template)"
    ng_id=$(_imds "tags/instance/aws:pcs:compute-node-group-id") \
        || die "Missing aws:pcs:compute-node-group-id tag"
    region=$(_imds "placement/region") || die "Could not read region"

    # Node type: a user-provided 'monitoring-role' tag distinguishes the
    # node group that runs the monitoring stack. Default: 'compute'.
    local node_type
    node_type=$(_imds "tags/instance/monitoring-role" 2>/dev/null || echo "compute")

    # Slurmctld endpoint: discovered via PCS API on the login node where
    # this script has aws cli access. Stored so compute nodes can read it.
    local slurmctld_ip=""
    if [[ "${node_type}" == "login" ]]; then
        slurmctld_ip=$(aws pcs get-cluster --region "${region}" \
            --cluster-identifier "${cluster_id}" \
            --query 'cluster.endpoints[?type==`SLURMCTLD`].privateIpAddress | [0]' \
            --output text 2>/dev/null) || die "Could not resolve slurmctld endpoint"
    fi

    export PLATFORM="pcs"
    export PLATFORM_NODE_TYPE="${node_type}"      # login | compute
    export PLATFORM_CLUSTER_NAME="${cluster_id}"
    export PLATFORM_REGION="${region}"
    export PLATFORM_USER="ec2-user"               # AL2023 default

    # PCS-specific
    export PCS_CLUSTER_ID="${cluster_id}"
    export PCS_NODE_GROUP_ID="${ng_id}"
    export PCS_SLURMCTLD_IP="${slurmctld_ip}"
}
