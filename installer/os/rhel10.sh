#!/bin/bash
# shellcheck disable=SC2154  # PLATFORM_USER from installer/platform/platform.sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# RHEL 10 / Rocky 10 / Alma 10 / CentOS Stream 10.
set -euo pipefail

log "Installing docker on ${OS_ID} ${OS_VERSION_ID}"
# Same upstream docker-ce repo as RHEL 9 (download.docker.com has el10
# builds); podman-docker is NOT a drop-in replacement for what we need
# (compose plugin, custom runtimes for GPU).
dnf -y install dnf-plugins-core

dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo

# Import Docker's GPG key before dnf verifies packages, as rhel9.sh does.
# Unlike rhel9.sh there is no fallback that turns gpgcheck off: that works
# around a GPGME failure on some RHEL 9 AMIs (issue #41), and EL10's rpm
# verifies signatures with rpm-sequoia. A failed import fails the install.
rpm --import https://download.docker.com/linux/centos/gpg

dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
               jq bc curl tar

systemctl enable docker
systemctl start docker
usermod -a -G docker "${PLATFORM_USER}"
