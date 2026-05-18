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

# Import Docker's GPG key via rpm --import before dnf tries to verify
# packages. This bypasses GPGME which can fail on some RHEL 9 AMIs with
# "Invalid crypto engine" when gnupg2 isn't fully configured (issue #41).
# If rpm --import also fails (same GPGME issue), fall back to disabling
# gpgcheck on the Docker repo — acceptable because we fetch the repo
# definition itself over HTTPS from docker.com.
if ! rpm --import https://download.docker.com/linux/centos/gpg 2>/dev/null; then
    log "WARN: rpm --import failed (GPGME issue); disabling gpgcheck for docker-ce repo"
    sed -i 's/^gpgcheck=1/gpgcheck=0/' /etc/yum.repos.d/docker-ce.repo
fi

dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
               jq bc curl tar

systemctl enable docker
systemctl start docker
usermod -a -G docker "${PLATFORM_USER}"
