#!/bin/bash
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Render the least-privilege policy template for a specific cluster.
#
# Usage:
#   iam/render-policy.sh <cluster-name> <region> [account-id] [s3-bucket] > out.json
#
# If account-id is omitted, it's pulled from sts get-caller-identity.
# If s3-bucket is omitted, the PostInstall S3 statements are removed.
#
set -euo pipefail

CLUSTER="${1:?usage: $0 <cluster> <region> [account] [s3-bucket]}"
REGION="${2:?usage: $0 <cluster> <region> [account] [s3-bucket]}"
ACCOUNT="${3:-$(aws sts get-caller-identity --query Account --output text)}"
S3_BUCKET="${4:-}"
PARTITION="aws"
[[ "${REGION}" == us-gov-* ]] && PARTITION="aws-us-gov"
[[ "${REGION}" == cn-* ]]     && PARTITION="aws-cn"

TEMPLATE="$(dirname "$0")/monitoring-head-node-policy.json"

# Render, substituting all ${Var} placeholders.
# If S3_BUCKET is empty, strip the S3 statement block entirely.
python3 - "$TEMPLATE" "$CLUSTER" "$REGION" "$ACCOUNT" "$PARTITION" "$S3_BUCKET" <<'PY'
import json, sys, re
template_path, cluster, region, account, partition, s3_bucket = sys.argv[1:7]
p = json.load(open(template_path))
# Remove the S3 statement if no bucket provided
if not s3_bucket:
    p['Statement'] = [s for s in p['Statement'] if s.get('Sid') != 'S3ReadPostInstallBucketOnly']
text = json.dumps(p, indent=2)
for k, v in {
    'ClusterName': cluster,
    'Region': region,
    'AccountId': account,
    'Partition': partition,
    'PostInstallBucket': s3_bucket or 'PLACEHOLDER',
}.items():
    text = text.replace('${' + k + '}', v)
# Validate it's still valid JSON
json.loads(text)
print(text)
PY
