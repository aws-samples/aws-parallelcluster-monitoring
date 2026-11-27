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
#   PLATFORM_USER         <local-user>   (ec2-user, ubuntu, rocky, etc)
#
# Plus any platform-specific extras set by the sub-modules.
#

# The PCS identity that installer/platform/pcs.sh saves on every PCS install,
# so that a re-run outside the node lifecycle action finds it. It holds
# INSTANCE_ID, PCS_CLUSTER_ID, PCS_COMPUTE_NODE_GROUP_ID and MONITORING_ROLE.
# custom-metrics/refresh-grafana-password.sh reads PCS_CLUSTER_ID from it too:
# keep the path and the format in sync.
PCS_NODE_ENV_FILE="/etc/aws-parallelcluster-monitoring/pcs-node.env"

detect_platform() {
    # ParallelCluster: writes /etc/parallelcluster/cfnconfig
    if [[ -r /etc/parallelcluster/cfnconfig ]]; then
        # shellcheck disable=SC1091
        . "$(dirname "${BASH_SOURCE[0]}")/parallelcluster.sh"
        _load_parallelcluster
        return 0
    fi

    # PCS: the PCS agent's environment (a node lifecycle action exports
    # PCS_CLUSTER_ID), the identity an earlier install saved on this instance,
    # or the aws:pcs:* / pcs-cluster-id instance tags (launch templates with
    # InstanceMetadataTags=enabled). No file that PCS itself creates is used:
    # PCS documents none as a marker.
    load_saved_pcs_identity
    if [[ -n "${PCS_CLUSTER_ID:-}" || -n "${SAVED_PCS_CLUSTER_ID}" ]] || imds_has_pcs_tag; then
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

# Echo this instance's ID from IMDS (empty if IMDS doesn't answer).
imds_instance_id() {
    local token
    token=$(curl -sf -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
        http://169.254.169.254/latest/api/token 2>/dev/null) || return 0
    curl -sf -H "X-aws-ec2-metadata-token: ${token}" \
        http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || return 0
}

# Set SAVED_PCS_CLUSTER_ID, SAVED_PCS_NODE_GROUP_ID and SAVED_MONITORING_ROLE
# from PCS_NODE_ENV_FILE, or to empty strings. The file is used only on the
# instance that wrote it, so that the instances of an AMI baked from a
# monitored node don't inherit its role. It is parsed line by line, never
# sourced: only the four keys are read, and only values of letters, digits,
# '_' and '-'.
load_saved_pcs_identity() {
    local line key value saved_instance="" cluster="" group="" role=""
    SAVED_PCS_CLUSTER_ID=""
    SAVED_PCS_NODE_GROUP_ID=""
    SAVED_MONITORING_ROLE=""
    [[ -r "${PCS_NODE_ENV_FILE}" ]] || return 0
    while IFS= read -r line || [[ -n "${line}" ]]; do
        [[ "${line}" =~ ^([A-Z_]+)=([A-Za-z0-9_-]{0,128})$ ]] || continue
        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        case "${key}" in
            INSTANCE_ID)               saved_instance="${value}" ;;
            PCS_CLUSTER_ID)            cluster="${value}" ;;
            PCS_COMPUTE_NODE_GROUP_ID) group="${value}" ;;
            MONITORING_ROLE)           role="${value}" ;;
        esac
    done < "${PCS_NODE_ENV_FILE}"
    if [[ -z "${saved_instance}" || "${saved_instance}" != "$(imds_instance_id)" ]]; then
        log "Ignoring ${PCS_NODE_ENV_FILE}: it was saved on another instance (${saved_instance:-none})"
        return 0
    fi
    SAVED_PCS_CLUSTER_ID="${cluster}"
    # shellcheck disable=SC2034  # used by installer/platform/pcs.sh
    SAVED_PCS_NODE_GROUP_ID="${group}"
    # shellcheck disable=SC2034  # used by installer/platform/pcs.sh
    SAVED_MONITORING_ROLE="${role}"
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
    # A here-string, not "echo | grep -q": under pipefail grep -q can exit
    # before echo finishes, echo dies of SIGPIPE and the check fails although
    # the tag is there (seen on 1 of 4 PCS boots in v2.11).
    grep -qE '^aws:pcs:|^pcs-cluster-id$' <<<"${tags}" && return 0
    return 1
}
