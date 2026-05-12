#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# ParallelCluster platform module.
#

_load_parallelcluster() {
    # shellcheck disable=SC1091
    . /etc/parallelcluster/cfnconfig
    : "${cfn_node_type:?cfn_node_type not set}"
    : "${cfn_cluster_user:?cfn_cluster_user not set}"
    : "${cfn_region:?cfn_region not set}"
    : "${stack_name:?stack_name not set}"

    export PLATFORM="parallelcluster"
    export PLATFORM_NODE_TYPE
    case "${cfn_node_type}" in
        HeadNode)     PLATFORM_NODE_TYPE="head" ;;
        ComputeFleet) PLATFORM_NODE_TYPE="compute" ;;
        *) die "Unknown cfn_node_type: ${cfn_node_type}" ;;
    esac
    export PLATFORM_CLUSTER_NAME="${stack_name}"
    export PLATFORM_REGION="${cfn_region}"
    export PLATFORM_USER="${cfn_cluster_user}"

    # ParallelCluster-specific exports used elsewhere
    export CFN_POSTINSTALL="${cfn_postinstall:-}"
}
