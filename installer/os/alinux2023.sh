#!/bin/bash
# shellcheck disable=SC2154  # PLATFORM_USER from installer/platform/platform.sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Amazon Linux 2023 package installation.
set -euo pipefail

log "Installing docker on Amazon Linux 2023"

# AL2023 ships curl-minimal by default; requesting the full `curl` package
# triggers a conflict. Use --allowerasing so dnf replaces curl-minimal.
# `bc` is not in the base repos, so it gets its own command below and may
# be skipped.
dnf -y install --allowerasing docker jq tar gzip
# bc is not in the default AL2023 repos; install it from the community repo
# if available, otherwise skip. No monitoring script needs it.
dnf -y install bc || log "WARN: bc not available; no monitoring script needs it"

# AL2023 does not ship docker-compose-plugin in its default repos yet.
# Install the plugin binary (idempotent: skips the download if the pinned
# version is already present, e.g. baked into a custom AMI).
install_compose_plugin_binary

systemctl enable docker
systemctl start docker
usermod -a -G docker "${PLATFORM_USER}"
