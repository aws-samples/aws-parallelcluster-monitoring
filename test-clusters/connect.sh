#!/usr/bin/env bash
# Print the commands that open Grafana on the test clusters through an SSM
# port forward, and that read the Grafana admin password.
set -euo pipefail
# shellcheck source=test-clusters/settings.sh
. "$(dirname "$0")/settings.sh"

running() { # running FILTER...: the IDs of the running instances that match
    aws ec2 describe-instances --filters "$@" Name=instance-state-name,Values=running \
        --query 'Reservations[].Instances[].InstanceId' --output text
}

print_access() { # print_access INSTANCE-ID LOCAL-PORT PASSWORD-PARAMETER
    cat <<EOF
Instance: $1

Open Grafana through an SSM port forward:
  aws ssm start-session --region ${REGION} --target $1 \\
    --document-name AWS-StartPortForwardingSession \\
    --parameters 'portNumber=["443"],localPortNumber=["$2"]'
Then browse to https://localhost:$2/grafana/ (self-signed certificate).

Grafana admin password:
  aws ssm get-parameter --region ${REGION} --name $3 \\
    --with-decryption --query Parameter.Value --output text

Shell:
  aws ssm start-session --region ${REGION} --target $1
EOF
}

echo "=== ParallelCluster ${PC_CLUSTER}: head node"
head_id="$(running "Name=tag:parallelcluster:cluster-name,Values=${PC_CLUSTER}" \
    Name=tag:parallelcluster:node-type,Values=HeadNode)"
if [[ -n "${head_id}" ]]; then
    print_access "${head_id}" 8443 "/parallelcluster/${PC_CLUSTER}/grafana/admin-password"
else
    echo "No running head node; run pc-create.sh first."
fi

echo
echo "=== AWS PCS ${PCS_CLUSTER}: login node"
login_id=""
if [[ -f "${STATE}/pcs.env" ]]; then
    # shellcheck source=/dev/null
    . "${STATE}/pcs.env"
    login_id="$(running "Name=tag:aws:pcs:compute-node-group-id,Values=${PCS_NG_LOGIN:-none}")"
fi
if [[ -n "${login_id}" ]]; then
    # A different local port, so both clusters can be open at once.
    print_access "${login_id%%[[:space:]]*}" 8444 "/pcs/${PCS_CLUSTER_ID}/grafana/admin-password"
else
    echo "No running login node; run pcs-create.sh first."
fi
