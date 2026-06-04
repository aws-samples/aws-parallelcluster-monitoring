#!/bin/bash
# shellcheck disable=SC2154  # PLATFORM_USER from installer/platform/platform.sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Amazon Linux 2023 package installation.
set -euo pipefail

log "Installing docker on Amazon Linux 2023"

# AL2023 ships curl-minimal by default; requesting the full `curl` package
# triggers a conflict. Use --allowerasing so dnf replaces curl-minimal.
# Also: `bc` is in amazon-linux-extras territory on AL2023 — install what
# is actually needed from the base repos, with graceful fallback.
dnf -y install --allowerasing docker jq tar gzip
# bc is not in the default AL2023 repos; install from the community repo
# if available, otherwise skip (cost scripts will tolerate its absence by
# falling through to 0 values).
dnf -y install bc || log "WARN: bc not available, cost scripts may be degraded"

# AL2023 does not ship docker-compose-plugin in its default repos yet.
# Install upstream plugin binary.
COMPOSE_VERSION="v5.1.4"
install -d -m 0755 /usr/libexec/docker/cli-plugins
curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-$(uname -m)" \
    -o /usr/libexec/docker/cli-plugins/docker-compose
chmod +x /usr/libexec/docker/cli-plugins/docker-compose

systemctl enable docker
systemctl start docker
usermod -a -G docker "${PLATFORM_USER}"
