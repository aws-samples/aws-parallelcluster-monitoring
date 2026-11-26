#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Textfile collector: maps Slurm jobs to nodes for Prometheus.
# Writes to node_exporter's textfile directory so it's scraped automatically.
#
# Output metric:
#   slurm_job_node{jobid="123",nodename="queue0-st-cr0-1",user="ec2-user",jobname="wrap",state="RUNNING"} 1
#
set -euo pipefail

TEXTFILE_DIR="/var/lib/prometheus/node-exporter"
OUTPUT="${TEXTFILE_DIR}/slurm_jobs.prom"

# Find a Slurm client command. ParallelCluster installs it in /opt/slurm/bin.
# AWS PCS installs one tree per Slurm version under
# /opt/aws/pcs/scheduler/slurm-<version>/bin and puts the cluster's version
# on the login PATH, which a systemd service doesn't get, so ask a login
# shell. If that finds nothing and there is exactly one PCS tree, use it.
# Prints nothing when not found; the caller then exits quietly.
find_slurm_bin() {
    local cmd="$1" prefix found
    local -a trees
    for prefix in /opt/slurm/bin /opt/aws-pcs/slurm/bin; do
        [[ -x "${prefix}/${cmd}" ]] && { echo "${prefix}/${cmd}"; return 0; }
    done
    found="$(command -v "${cmd}" 2>/dev/null)" || found=""
    if [[ -z "${found}" ]]; then
        found="$(bash -lc "command -v ${cmd}" 2>/dev/null </dev/null | tail -n 1)" || found=""
    fi
    if [[ -z "${found}" ]]; then
        trees=(/opt/aws/pcs/scheduler/slurm-*/bin/"${cmd}")
        [[ ${#trees[@]} -eq 1 && -x "${trees[0]}" ]] && found="${trees[0]}"
    fi
    [[ -n "${found}" && -x "${found}" ]] && echo "${found}"
    return 0
}
SQUEUE="$(find_slurm_bin squeue)"
SCONTROL="$(find_slurm_bin scontrol)"

mkdir -p "${TEXTFILE_DIR}"

# If squeue isn't available (compute node, or login before slurm client
# packages land), exit silently. Timer will pick it up on the next tick.
[[ -n "${SQUEUE}" && -x "${SQUEUE}" ]] || exit 0

# Get all running/pending jobs with their node assignments.
# Format: JOBID NODELIST STATE USER JOBNAME
# NODELIST can be a range like "queue0-st-cr0-[1-2]" — expand with scontrol.
TMP="${OUTPUT}.tmp"

{
    echo "# HELP slurm_job_node Maps Slurm jobs to the nodes they run on"
    echo "# TYPE slurm_job_node gauge"

    "${SQUEUE}" -ho "%A|%N|%T|%u|%j" 2>/dev/null | while IFS='|' read -r jobid nodelist state user jobname; do
        [[ -z "${nodelist}" || "${nodelist}" == "(None)" ]] && continue
        # Expand node ranges (e.g. queue0-st-cr0-[1-2] -> queue0-st-cr0-1\nqueue0-st-cr0-2)
        if [[ -n "${SCONTROL}" && -x "${SCONTROL}" ]]; then
            expanded=$("${SCONTROL}" show hostnames "${nodelist}" 2>/dev/null) || expanded="${nodelist}"
        else
            expanded="${nodelist}"
        fi
        for node in ${expanded}; do
            # Sanitize label values (remove quotes, limit length)
            jobname_safe="${jobname//\"/}"
            jobname_safe="${jobname_safe:0:50}"
            echo "slurm_job_node{jobid=\"${jobid}\",nodename=\"${node}\",user=\"${user}\",jobname=\"${jobname_safe}\",state=\"${state}\"} 1"
        done
    done
} > "${TMP}"

# Atomic rename so node_exporter never reads a partial file.
mv -f "${TMP}" "${OUTPUT}"
