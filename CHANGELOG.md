# Changelog

## v1.0 — Phase 1 modernization (unreleased)

This release restores compatibility with current ParallelCluster and operating
system versions. No breaking changes to end-user configuration.

### Added
- OS-aware installer (`installer/install.sh` + `installer/os/*.sh`) supporting
  Amazon Linux 2, Amazon Linux 2023, Ubuntu 22.04, Ubuntu 24.04, and RHEL /
  Rocky / Alma / CentOS Stream 9.
- Docker Compose v2 plugin installation on every supported OS.
- GPU detection via `lspci` / `/dev/nvidia0` — no longer depends on parsing
  the EC2 instance-type string (works with p4de, p5, p5e, p5en, g5, g6, g6e,
  and future families).
- `nvidia-container-toolkit` installation on GPU compute nodes (replaces
  deprecated `nvidia-docker2`).
- Current GPU and accelerator instance types in `prometheus.yml`
  ec2_sd_configs filter (p4de, p5, p5e, p5en, g5, g6, g6e).

### Changed
- `prometheus-slurm-exporter`: replaced unmaintained GPLv3
  `vpenso/prometheus-slurm-exporter` with Apache-2.0
  `rivosinc/prometheus-slurm-exporter` v1.8.0.
- Pinned every container image to an explicit version tag; removed every
  `:latest` reference.
- Removed the `version:` key from all compose files (ignored by Compose v2).
- Compose files moved from `docker-compose/` to `compose/` and renamed
  `master` → `head` to match ParallelCluster terminology.
- `post-install.sh` default version bumped to `v1.0`.
- Cron `MAILTO=""` set to silence cost-scraper email spam (closes #15).
- `aws-region.py` migrated from deprecated `pkg_resources` to `pathlib`.

### Fixed
- S3 cost tier detection referenced an undefined `$VAR` variable (fell
  through to the `Inf` tier for all buckets > 50 TB).
- FSx cost script now handles the `PERSISTENT_2` deployment type.
- EBS cost script defaults to `gp3` (the PC 3.x default) instead of `gp2`.
- `install-monitoring.sh` was Amazon-Linux-only (`yum install docker` hardcoded)
  and failed on Ubuntu and AL2023.
- Slurm exporter no longer built from source on every cluster boot; prebuilt
  binary downloaded from GitHub releases instead. Removes the `golang-bin`
  install and the `go mod download && go build` step.

### Removed
- `parallelcluster-setup/install-monitoring.sh` (replaced by
  `installer/install.sh`).
- `docker-compose/` directory (replaced by `compose/`).
- In-tree `git clone` + `go build` of the slurm exporter on the HeadNode.
