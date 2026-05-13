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

# Find squeue. ParallelCluster installs at /opt/slurm/bin; PCS AMIs vary
# (some use /opt/slurm/bin, some /opt/aws-pcs/slurm/bin). Fall back to
# PATH so we don't need to hardcode every layout.
find_slurm_bin() {
    local cmd="$1"
    for prefix in /opt/slurm/bin /opt/aws-pcs/slurm/bin; do
        [[ -x "${prefix}/${cmd}" ]] && { echo "${prefix}/${cmd}"; return 0; }
    done
    command -v "${cmd}" 2>/dev/null
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
