#!/bin/bash
# shellcheck disable=SC2154  # cfn_* / stack_name vars come from /etc/parallelcluster/cfnconfig
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Amazon Linux 2 package installation (docker + compose v2 plugin).
set -euo pipefail

log "Installing docker on Amazon Linux 2"
# Amazon Linux 2 has docker in its extras repo.
amazon-linux-extras install -y docker
yum -y install jq bc tar gzip

# Docker Compose v2 is NOT in AL2 repos. Install as a plugin binary.
COMPOSE_VERSION="v2.29.7"
install -d -m 0755 /usr/libexec/docker/cli-plugins
curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-$(uname -m)" \
    -o /usr/libexec/docker/cli-plugins/docker-compose
chmod +x /usr/libexec/docker/cli-plugins/docker-compose

systemctl enable docker
systemctl start docker
usermod -a -G docker "${cfn_cluster_user}"
