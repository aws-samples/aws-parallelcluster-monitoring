#!/usr/bin/env bash
# Print connection commands for both test clusters.
set -euo pipefail

REGION="us-east-2"

echo "==================================================================="
echo " ParallelCluster (monitoring-test-pc)"
echo "==================================================================="

if [[ -f .state/pc.env ]]; then
    # shellcheck disable=SC1091
    . .state/pc.env
    echo "Head node: $PC_HEAD_ID"
    echo
    echo "Open Grafana via SSM port-forward:"
    echo "  aws ssm start-session --target $PC_HEAD_ID --region $REGION \\"
    echo "    --document-name AWS-StartPortForwardingSession \\"
    echo "    --parameters 'portNumber=[\"443\"],localPortNumber=[\"8443\"]'"
    echo
    echo "Then browse: https://localhost:8443/grafana/"
    echo
    echo "Retrieve the Grafana admin password:"
    echo "  aws ssm get-parameter --region $REGION \\"
    echo "    --name /parallelcluster/$PC_CLUSTER/grafana/admin-password \\"
    echo "    --with-decryption --query Parameter.Value --output text"
    echo
    echo "SSH (via SSM):"
    echo "  aws ssm start-session --target $PC_HEAD_ID --region $REGION"
else
    echo "  (no PC cluster, run pc-create.sh first)"
fi

echo
echo "==================================================================="
echo " PCS (monitoring-test-pcs)"
echo "==================================================================="

if [[ -f .state/pcs.env ]]; then
    # shellcheck disable=SC1091
    . .state/pcs.env
    echo "Login node: $PCS_LOGIN_ID"
    echo "Slurmctld:  $PCS_SLURMCTLD_IP"
    echo
    echo "Open Grafana via SSM port-forward (use a different local port"
    echo "than the PC cluster so they don't collide):"
    echo "  aws ssm start-session --target $PCS_LOGIN_ID --region $REGION \\"
    echo "    --document-name AWS-StartPortForwardingSession \\"
    echo "    --parameters 'portNumber=[\"443\"],localPortNumber=[\"8444\"]'"
    echo
    echo "Then browse: https://localhost:8444/grafana/"
    echo
    echo "Retrieve the Grafana admin password:"
    echo "  aws ssm get-parameter --region $REGION \\"
    echo "    --name /pcs/$PCS_CLUSTER_ID/grafana/admin-password \\"
    echo "    --with-decryption --query Parameter.Value --output text"
    echo
    echo "SSH (via SSM):"
    echo "  aws ssm start-session --target $PCS_LOGIN_ID --region $REGION"
else
    echo "  (no PCS cluster, run pcs-create.sh first)"
fi
echo
