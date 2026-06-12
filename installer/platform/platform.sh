#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Platform detection for the monitoring installer.
#
# Exports a uniform set of variables regardless of whether the node is
# running on ParallelCluster or PCS:
#
#   PLATFORM              parallelcluster | pcs | res
#   PLATFORM_NODE_TYPE    head | compute | login
#   PLATFORM_CLUSTER_NAME <string>   (unique cluster identifier)
#   PLATFORM_REGION       <aws-region>
#   PLATFORM_USER         <local-user>   (ec2-user, ubuntu, etc)
#
# Plus any platform-specific extras set by the sub-modules.
#

detect_platform() {
    # ParallelCluster: writes /etc/parallelcluster/cfnconfig
    if [[ -r /etc/parallelcluster/cfnconfig ]]; then
        # shellcheck disable=SC1091
        . "$(dirname "${BASH_SOURCE[0]}")/parallelcluster.sh"
        _load_parallelcluster
        return 0
    fi

    # PCS: /etc/aws-pcs/ directory present, or instance has aws:pcs:* tags
    if [[ -d /etc/aws-pcs ]] || imds_has_pcs_tag; then
        # shellcheck disable=SC1091
        . "$(dirname "${BASH_SOURCE[0]}")/pcs.sh"
        _load_pcs
        return 0
    fi

    # RES: explicit RES_ENVIRONMENT_NAME env var (set in the RES project
    # launch script) or the res:EnvironmentName instance tag via IMDS.
    if [[ -n "${RES_ENVIRONMENT_NAME:-}" ]] || [[ -n "$(imds_res_environment_name)" ]]; then
        # shellcheck disable=SC1091
        . "$(dirname "${BASH_SOURCE[0]}")/res.sh"
        _load_res
        return 0
    fi

    die "Cannot detect platform: no ParallelCluster, PCS, or RES markers found"
}

# Echo the res:EnvironmentName instance tag from IMDS (empty if absent).
# Requires InstanceMetadataTags=enabled on the instance.
imds_res_environment_name() {
    local token
    token=$(curl -sf -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
        http://169.254.169.254/latest/api/token 2>/dev/null) || return 0
    curl -sf -H "X-aws-ec2-metadata-token: ${token}" \
        "http://169.254.169.254/latest/meta-data/tags/instance/res:EnvironmentName" 2>/dev/null || return 0
}

imds_has_pcs_tag() {
    local token tags
    token=$(curl -sf -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
        http://169.254.169.254/latest/api/token 2>/dev/null) || return 1
    tags=$(curl -sf -H "X-aws-ec2-metadata-token: $token" \
        http://169.254.169.254/latest/meta-data/tags/instance 2>/dev/null) || return 1
    # PCS-managed compute nodes get the aws:pcs:* tags from PCS itself.
    # Login nodes launched directly against the cluster don't, so they
    # use a user-set mirror tag (pcs-cluster-id) — see installer/platform/pcs.sh.
    echo "$tags" | grep -qE '^aws:pcs:|^pcs-cluster-id$' && return 0
    return 1
}
