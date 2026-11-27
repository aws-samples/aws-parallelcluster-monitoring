#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# AWS Parallel Computing Service (PCS) platform module.
#
# Resolves the node's cluster, compute node group and role from the first
# source that has them:
#
# 1. The node lifecycle action. On node groups in standard configuration (no
#    launch template), post-install.sh runs as a lifecycle action. The PCS
#    agent exports PCS_CLUSTER_ID and PCS_COMPUTE_NODE_GROUP_ID, and the
#    role comes from post-install.sh's --role argument (MONITORING_ROLE).
#    These nodes have no user data and no instance tags in IMDS.
#
# 2. The instance tags, through IMDS (launch templates with
#    InstanceMetadataTags=enabled):
#      aws:pcs:cluster-id, aws:pcs:compute-node-group-id
#        set by PCS on the instances of a compute node group;
#      pcs-cluster-id=<id>, monitoring-role=login
#        set by the launch template of a login node launched outside PCS,
#        which gets no aws:pcs:* tags. The aws: tag namespace is reserved by
#        AWS, hence the mirror.
#
# 3. The identity saved by an earlier install on this instance
#    (PCS_NODE_ENV_FILE, see platform.sh), for re-runs of install.sh outside
#    the lifecycle action.
#
# Reads cluster metadata (slurmctld endpoint) via the PCS API.
#

# _save_pcs_identity <cluster ID> <node group ID> <role>: write
# PCS_NODE_ENV_FILE (root, 0644, through a temporary file and a rename),
# with this instance's ID. Called by _load_pcs, which defines _imds.
_save_pcs_identity() {
    local instance_id role="$3" tmp="${PCS_NODE_ENV_FILE}.new"
    instance_id=$(_imds "instance-id" 2>/dev/null) || instance_id=""
    if [[ ! "${instance_id}" =~ ^[A-Za-z0-9_-]{1,128}$ ]]; then
        warn "Could not read this instance's ID from IMDS: not saving the PCS identity to ${PCS_NODE_ENV_FILE}"
        return 0
    fi
    # A monitoring-role tag can hold any value; save only one the file's
    # parser accepts.
    [[ "${role}" =~ ^[A-Za-z0-9_-]{0,128}$ ]] || role=""
    install -d -m 0755 "$(dirname "${PCS_NODE_ENV_FILE}")"
    printf 'INSTANCE_ID=%s\nPCS_CLUSTER_ID=%s\nPCS_COMPUTE_NODE_GROUP_ID=%s\nMONITORING_ROLE=%s\n' \
        "${instance_id}" "$1" "$2" "${role}" > "${tmp}"
    chmod 0644 "${tmp}"
    mv -f "${tmp}" "${PCS_NODE_ENV_FILE}"
}

_load_pcs() {
    local token
    token=$(curl -sf -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' \
        http://169.254.169.254/latest/api/token 2>/dev/null) \
        || die "Could not get IMDS token"

    _imds() {
        curl -sf -H "X-aws-ec2-metadata-token: $token" \
            "http://169.254.169.254/latest/meta-data/$1"
    }

    # Cluster and compute node group. A login node launched outside PCS has
    # no node group.
    local cluster_id="" ng_id="" region node_type id_source role_source
    if [[ -n "${PCS_CLUSTER_ID:-}" ]]; then
        cluster_id="${PCS_CLUSTER_ID}"
        ng_id="${PCS_COMPUTE_NODE_GROUP_ID:-}"
        id_source="node lifecycle action"
    elif cluster_id=$(_imds "tags/instance/aws:pcs:cluster-id" 2>/dev/null) \
        || cluster_id=$(_imds "tags/instance/pcs-cluster-id" 2>/dev/null); then
        ng_id=$(_imds "tags/instance/aws:pcs:compute-node-group-id" 2>/dev/null || echo "")
        id_source="instance tags"
    elif [[ -n "${SAVED_PCS_CLUSTER_ID:-}" ]]; then
        cluster_id="${SAVED_PCS_CLUSTER_ID}"
        ng_id="${SAVED_PCS_NODE_GROUP_ID:-}"
        id_source="saved identity"
    else
        die "Cannot detect the PCS cluster: no PCS_CLUSTER_ID from a node lifecycle action, no aws:pcs:cluster-id or pcs-cluster-id instance tag in IMDS, and no identity saved on this instance in ${PCS_NODE_ENV_FILE}. On node groups without a launch template, run post-install.sh as a node lifecycle action. With a launch template, set MetadataOptions.InstanceMetadataTags=enabled, and set pcs-cluster-id on login nodes launched outside PCS."
    fi
    # The IDs go into sed expressions and env files. Every PCS ID matches.
    [[ "${cluster_id}" =~ ^[A-Za-z0-9_-]{1,128}$ ]] \
        || die "Invalid PCS cluster ID from the ${id_source}: '${cluster_id:0:140}'"
    [[ -z "${ng_id}" || "${ng_id}" =~ ^[A-Za-z0-9_-]{1,128}$ ]] \
        || die "Invalid PCS compute node group ID from the ${id_source}: '${ng_id:0:140}'"

    region=$(_imds "placement/region") || die "Could not read region"

    # Node type: login runs the full monitoring stack, compute the exporters.
    # post-install.sh --role sets MONITORING_ROLE; with a launch template the
    # monitoring-role tag does (any other value, such as none, skips the
    # install). Default: compute.
    if [[ -n "${MONITORING_ROLE:-}" ]]; then
        case "${MONITORING_ROLE}" in
            login|compute) node_type="${MONITORING_ROLE}" ;;
            *) die "MONITORING_ROLE must be login or compute, not '${MONITORING_ROLE:0:64}'" ;;
        esac
        role_source="--role"
    elif node_type=$(_imds "tags/instance/monitoring-role" 2>/dev/null); then
        role_source="monitoring-role tag"
    elif [[ -n "${SAVED_MONITORING_ROLE:-}" ]]; then
        node_type="${SAVED_MONITORING_ROLE}"
        role_source="saved identity"
    else
        node_type="compute"
        role_source="default"
    fi
    log "PCS identity from ${id_source}: cluster ${cluster_id}, node group ${ng_id:-none}, role ${node_type} (${role_source})"
    _save_pcs_identity "${cluster_id}" "${ng_id}" "${node_type}"

    # Slurmctld endpoint: needed by Prometheus to scrape native /metrics
    # on PCS. Discovered via PCS API on the login node.
    local slurmctld_ip=""
    if [[ "${node_type}" == "login" ]]; then
        # shellcheck disable=SC2016  # the backticks are a JMESPath literal
        slurmctld_ip=$(aws pcs get-cluster --region "${region}" \
            --cluster-identifier "${cluster_id}" \
            --query 'cluster.endpoints[?type==`SLURMCTLD`].privateIpAddress | [0]' \
            --output text 2>/dev/null) || die "Could not resolve slurmctld endpoint"
    fi

    # Detect the platform user. Ubuntu-based AMIs use 'ubuntu', AL2023 and
    # RHEL use 'ec2-user', Rocky Linux AMIs use 'rocky' (no ec2-user). Keep
    # in sync with post-install.sh.
    local platform_user="ec2-user"
    if [[ -d /home/ubuntu ]] && id ubuntu >/dev/null 2>&1; then
        platform_user="ubuntu"
    elif ! id ec2-user >/dev/null 2>&1 && id rocky >/dev/null 2>&1; then
        platform_user="rocky"
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
