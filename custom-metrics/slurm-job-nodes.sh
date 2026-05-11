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
SQUEUE="/opt/slurm/bin/squeue"

mkdir -p "${TEXTFILE_DIR}"

# If squeue isn't available (compute node without slurmctld), exit silently.
[[ -x "${SQUEUE}" ]] || exit 0

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
        expanded=$(/opt/slurm/bin/scontrol show hostnames "${nodelist}" 2>/dev/null) || expanded="${nodelist}"
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
