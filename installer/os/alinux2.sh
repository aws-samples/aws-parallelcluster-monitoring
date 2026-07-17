#!/bin/bash
# shellcheck disable=SC2154  # PLATFORM_USER from installer/platform/platform.sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Amazon Linux 2 package installation (docker + compose plugin).
set -euo pipefail

log "Installing docker on Amazon Linux 2"
# Amazon Linux 2 has docker in its extras repo.
amazon-linux-extras install -y docker
yum -y install jq bc tar gzip

# Docker Compose is NOT in AL2 repos. Install the plugin binary (idempotent:
# skips the download if the pinned version is already present).
install_compose_plugin_binary

systemctl enable docker
systemctl start docker
usermod -a -G docker "${PLATFORM_USER}"
