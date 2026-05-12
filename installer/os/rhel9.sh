#!/bin/bash
# shellcheck disable=SC2154  # PLATFORM_USER from installer/platform/platform.sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# RHEL 9 / Rocky 9 / Alma 9 / CentOS Stream 9.
set -euo pipefail

log "Installing docker on ${OS_ID} ${OS_VERSION_ID}"
# RHEL 9 uses the upstream docker-ce repo; podman-docker is NOT a drop-in
# replacement for what we need (compose plugin, custom runtimes for GPU).
dnf -y install dnf-plugins-core
dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
               jq bc curl tar

systemctl enable docker
systemctl start docker
usermod -a -G docker "${PLATFORM_USER}"
