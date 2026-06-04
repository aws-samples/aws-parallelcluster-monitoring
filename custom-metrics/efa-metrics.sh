#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Textfile collector: exports Amazon EFA hardware counters for Prometheus.
#
# Why this exists:
#   node_exporter's --collector.infiniband only reads the standard IB
#   counters in /sys/class/infiniband/<dev>/ports/<port>/counters/
#   (port_rcv_data, port_xmit_data, ...). The EFA-specific counters that
#   matter for HPC/ML fabric health — RDMA read/write bytes, SRD
#   retransmits, work-request errors — live in a SEPARATE directory:
#       /sys/class/infiniband/<dev>/ports/<port>/hw_counters/
#   which that collector does not read. Rather than ship a forked
#   node_exporter binary (as some solutions do), we surface the same
#   counters via the textfile collector.
#
# Metric naming mirrors the upstream awsome-distributed-training EFA node
# exporter (node_amazonefa_<counter>), so dashboards/queries are portable
# to/from that exporter.
#
# Output: /var/lib/prometheus/node-exporter/efa.prom
#   node_amazonefa_rdma_read_bytes{device="rdmap16s27",port="1"} 12345
#   node_amazonefa_retrans_pkts{device="rdmap16s27",port="1"} 0
#   ...
#
# The instance_id / instance_type labels are added by Prometheus at scrape
# time (ec2_sd relabeling on the :9100 node_exporter target), so this
# script only emits device/port labels — exactly like the other panels.
#
# Emits nothing (zero-length file) on instances without EFA, so the panels
# simply stay empty there. Runs as root via a systemd timer.
#
set -euo pipefail

# Paths are overridable for testing (EFA_SYSFS_PATH / EFA_OUTPUT). In
# production they default to the real sysfs and node_exporter textfile dir.
IB_PATH="${EFA_SYSFS_PATH:-/sys/class/infiniband}"
TEXTFILE_DIR="/var/lib/prometheus/node-exporter"
OUTPUT="${EFA_OUTPUT:-${TEXTFILE_DIR}/efa.prom}"
TMP="${OUTPUT}.$$"
mkdir -p "$(dirname "${OUTPUT}")"

# No IB/EFA class at all → write an empty file and exit so any stale
# metrics are cleared.
if [[ ! -d "${IB_PATH}" ]]; then
    : > "${TMP}"
    mv -f "${TMP}" "${OUTPUT}"
    exit 0
fi

# Decide whether a device is EFA. EFA devices are bound to the 'efa'
# kernel driver; fall back to the presence of an EFA-specific counter
# file (rdma_read_bytes) for robustness across kernels.
is_efa_device() {
    local dev="$1"
    local drv
    drv=$(readlink -f "${IB_PATH}/${dev}/device/driver" 2>/dev/null || true)
    [[ "${drv##*/}" == "efa" ]] && return 0
    # Fallback: any port exposing the EFA-specific hw_counter.
    compgen -G "${IB_PATH}/${dev}/ports/*/hw_counters/rdma_read_bytes" >/dev/null 2>&1
}

# First pass: collect EFA (device, port, hw_counters-dir) tuples and the
# union of counter file names, so we can group samples by metric name
# (the Prometheus text format requires each metric's TYPE line to precede
# its samples and all samples to be contiguous).
#
# Uses indexed arrays + a newline-delimited string (deduped via sort -u)
# instead of associative arrays, so it runs on bash 3.2+ (macOS dev hosts,
# minimal images) as well as the bash 4+ on the HPC AMIs.
HWDIRS=()
DEVS=()
PORTS=()
counter_names=""

for devpath in "${IB_PATH}"/*; do
    [[ -d "${devpath}" ]] || continue
    dev="$(basename "${devpath}")"
    is_efa_device "${dev}" || continue
    for portpath in "${devpath}"/ports/*; do
        [[ -d "${portpath}/hw_counters" ]] || continue
        port="$(basename "${portpath}")"
        hwdir="${portpath}/hw_counters"
        HWDIRS+=("${hwdir}")
        DEVS+=("${dev}")
        PORTS+=("${port}")
        for cfile in "${hwdir}"/*; do
            [[ -f "${cfile}" ]] || continue
            counter_names="${counter_names}$(basename "${cfile}")
"
        done
    done
done

# Sorted, de-duplicated list of counter names across all EFA ports.
counters_sorted="$(printf '%s' "${counter_names}" | sort -u | sed '/^$/d')"

# Emit grouped by metric name.
{
    while IFS= read -r counter; do
        [[ -n "${counter}" ]] || continue
        metric="node_amazonefa_${counter}"
        echo "# HELP ${metric} EFA hw_counter ${counter} from /sys/class/infiniband/*/ports/*/hw_counters"
        echo "# TYPE ${metric} counter"
        for i in "${!HWDIRS[@]}"; do
            cfile="${HWDIRS[$i]}/${counter}"
            [[ -r "${cfile}" ]] || continue
            value="$(cat "${cfile}" 2>/dev/null || echo "")"
            # Only emit clean non-negative integers; skip N/A or blank.
            [[ "${value}" =~ ^[0-9]+$ ]] || continue
            echo "${metric}{device=\"${DEVS[$i]}\",port=\"${PORTS[$i]}\"} ${value}"
        done
    done <<< "${counters_sorted}"
} > "${TMP}"

# Atomic rename so node_exporter never reads a partial file.
mv -f "${TMP}" "${OUTPUT}"
