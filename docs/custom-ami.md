# Reducing compute node boot time with a custom AMI

## Why this matters

By default the monitoring stack is installed by a ParallelCluster **post-install (`OnNodeConfigured`) action** that runs [`post-install.sh`](../post-install.sh), which downloads this repo to node-local `/opt` and hands off to [`installer/install.sh`](../installer/install.sh).

On a compute node this runs **every time a node is launched** — that is, on every scale-up event, because ParallelCluster launches compute instances dynamically as jobs are queued. Each boot the installer has to:

1. Install Docker and the Docker Compose v2 plugin for the node's OS ([`installer/os/*.sh`](../installer/os))
2. **GPU nodes only:** add the NVIDIA repository, install the [NVIDIA Container Toolkit](https://github.com/NVIDIA/nvidia-container-toolkit), run `nvidia-ctk runtime configure`, and restart Docker
3. `docker compose ... up -d`, which **pulls the container images over the network** — `node-exporter` (`compose/compute.yml`), plus the large `dcgm-exporter` image on GPU nodes (`compose/compute.gpu.yml`)

These are all network-bound steps that sit between "instance launched" and "node ready to run jobs". The more your fleet scales out and back, the more often you pay this cost.

The fix is to **bake the packages and images into a custom compute-node AMI**, so at boot the node only has to start already-present containers from already-cached images.

## Important: custom AMIs are tied to a ParallelCluster version

A ParallelCluster custom AMI is built **on top of the official ParallelCluster base AMI for a specific ParallelCluster version and OS**. You cannot use an AMI built for one ParallelCluster version with a different version — the cluster fails validation.

For that reason **we do not publish a prebuilt AMI**. This page documents the procedure so you can build one against the exact ParallelCluster version and OS you run, and rebuild it whenever you upgrade ParallelCluster. (This solution supports ParallelCluster 3.14–3.16 on AL2023, Ubuntu 22.04/24.04 and RHEL 9, and Amazon Linux 2 on 3.14–3.15 only, because ParallelCluster 3.16 dropped it. ParallelCluster 3.10–3.13 are past their AWS end-of-support date; see the [support policy](https://docs.aws.amazon.com/parallelcluster/latest/ug/support-policy.html).)

> When you adopt a new ParallelCluster release, repeat these steps to produce a matching AMI and update `CustomAmi` in your cluster config.

## Expected boot time impact

On a custom AMI, `installer/install.sh` detects that the container runtime is already present and **skips the entire per-OS bootstrap** (docker install + Compose plugin download), and `docker compose up` starts the containers from the **pre-baked images** instead of pulling them. So the monitoring stack adds only a few seconds to boot.

| Compute node type | Standard method (installer runs full bootstrap every boot) | Custom AMI (runtime + images pre-baked) |
|---|---|---|
| CPU node | docker install + ~34 MB Compose plugin download + `node-exporter` image pull | OS bootstrap skipped, image served from cache; only the installer downloads from GitHub remain |
| GPU node | above **plus** NVIDIA Container Toolkit install + Docker restart + `dcgm-exporter` image pull (about 49 MB to download, about 150 MB on disk with Docker 25) | toolkit install is a no-op and both images are served from cache; the node still contacts the NVIDIA package repository and restarts Docker for the NVIDIA runtime |

> **Validated live** (ParallelCluster 3.15.1, Amazon Linux 2023). Custom-AMI compute nodes were compared against a control queue on the stock AMI, both running the same installer pinned to `main`:
>
> - **CPU (`c5.large`)**: time from EC2 launch to `node-exporter` running was **~109 s on the custom AMI vs ~126 s on the stock AMI (~17 s faster)**. On the custom node the installer logged *"Container runtime already present (pre-baked AMI) — skipping OS bootstrap"*: **zero** Compose-plugin downloads and **zero** image pulls at boot, versus one of each on the stock node. Both served 665 `node_` metrics.
> - **GPU (`g4dn.xlarge`, Tesla T4)**: both the `node-exporter` image and the `dcgm-exporter` image (about 49 MB to download, about 150 MB on disk with Docker 25) were served from the pre-baked cache with **zero pulls at boot**, the NVIDIA Container Toolkit install was a no-op, and the OS bootstrap was skipped. This is where pre-baking helps most, since the stock path would otherwise download the large `dcgm-exporter` image on every scale-up.
>
> The launch-to-ready times above are dominated by the OS boot and the ParallelCluster bootstrap (identical on both AMIs); the delta is the monitoring stack's contribution. Measure in your own environment before drawing conclusions.

**Benefits of the custom AMI approach:**

- Compute nodes become ready for Slurm much faster, so jobs start sooner and autoscaling is more responsive.
- Boot no longer pulls images from quay.io or nvcr.io and no longer installs Docker or the Compose plugin, which removes a class of transient boot failures and rate-limit issues. Nodes still need outbound HTTPS: every boot downloads `post-install.sh` and the monitoring tree from GitHub (with `Args: latest`, it also calls the GitHub API), and GPU nodes still contact the NVIDIA package repository. See [Step 5](#step-5--reference-the-custom-ami-in-your-cluster-configuration).
- More predictable, repeatable boot times across the fleet.

**Trade-off:** you take on the maintenance of rebuilding the AMI for each ParallelCluster version you upgrade to (see note above).

## Prerequisites

- The [AWS ParallelCluster CLI](https://docs.aws.amazon.com/parallelcluster/latest/ug/install-v3.html) installed, at the **same version** you use to create your cluster:
  ```bash
  pcluster version
  ```
- Credentials for an account/region where EC2 Image Builder can run (the build launches a temporary EC2 instance).
- The build component script from this repo, [`parallelcluster-setup/build-custom-ami-component.sh`](../parallelcluster-setup/build-custom-ami-component.sh). It reuses this repo's own installer to install Docker + the Compose plugin and pre-pulls the exact pinned images from the `compose/` files, and — when it detects an NVIDIA GPU on the build instance — installs the NVIDIA Container Toolkit and pre-pulls `dcgm-exporter`.

## Shortcut: `build-ami.sh`

If you just want the AMI, the helper [`parallelcluster-setup/build-ami.sh`](../parallelcluster-setup/build-ami.sh) automates Steps 1, 3 and 4 — it resolves the official base AMI, fills in the build template, and runs `pcluster build-image`:

```bash
# CPU AMI on Amazon Linux 2023
parallelcluster-setup/build-ami.sh --type cpu --os alinux2023 --region us-east-1 --wait

# GPU AMI (builds on a GPU instance; bakes the NVIDIA toolkit + dcgm-exporter image)
parallelcluster-setup/build-ami.sh --type gpu --os alinux2023 --region us-east-1 --wait
```

It uses the templates [`build-image-cpu.yaml`](../parallelcluster-setup/build-image-cpu.yaml) / [`build-image-gpu.yaml`](../parallelcluster-setup/build-image-gpu.yaml). The manual steps below explain what it does and how to customize the build. Either way, continue at Step 5 to use the resulting AMI.

## Step 1 — Find the official ParallelCluster base AMI for your version

The custom AMI must be built on the official ParallelCluster AMI matching your CLI version and target OS. List them:

```bash
pcluster list-official-images --region <your-region> --os alinux2023
```

Note the `amiId` returned for your OS (e.g. `ami-0123456789abcdef0`) — you'll use it as the `ParentImage` below. Supported `--os` values for this solution: `alinux2023`, `ubuntu2204`, `ubuntu2404` and `rhel9`, plus `alinux2` with ParallelCluster 3.15 or earlier.

## Step 2 — Choose how to reference the build component script

The build configuration references the component script by an `https` or `s3` URL.

- **Simplest:** reference the raw file from this repository (pin to a tag for reproducibility):
  ```
  https://raw.githubusercontent.com/aws-samples/aws-parallelcluster-monitoring/main/parallelcluster-setup/build-custom-ami-component.sh
  ```
- **Recommended for production:** upload a copy to your own S3 bucket so the build is reproducible and unaffected by upstream changes:
  ```bash
  aws s3 cp parallelcluster-setup/build-custom-ami-component.sh \
    s3://<your-bucket>/pcluster/build-custom-ami-component.sh
  ```
  Then reference `s3://<your-bucket>/pcluster/build-custom-ami-component.sh` and give the build instance read access (see the `Iam` note in Step 3).

## Step 3 — Create the build-image configuration file

Create `build-image-config.yaml`. Replace `ParentImage` with the AMI ID from Step 1 and set `InstanceType` for the architecture you target.

For a **CPU** compute-node AMI:

```yaml
Build:
  InstanceType: c5.xlarge
  ParentImage: ami-0123456789abcdef0   # from: pcluster list-official-images
  UpdateOsPackages:
    Enabled: false
  Components:
    - Type: script
      Value: https://raw.githubusercontent.com/aws-samples/aws-parallelcluster-monitoring/main/parallelcluster-setup/build-custom-ami-component.sh
```

For a **GPU** compute-node AMI, build on a GPU instance so the component installs the NVIDIA Container Toolkit and pre-pulls `dcgm-exporter`. You can also let ParallelCluster install the NVIDIA GPU driver/CUDA into the AMI:

```yaml
Build:
  InstanceType: g4dn.xlarge
  ParentImage: ami-0123456789abcdef0   # from: pcluster list-official-images
  UpdateOsPackages:
    Enabled: false
  Installation:
    NvidiaSoftware:
      Enabled: true                    # installs the NVIDIA GPU driver + CUDA
  Components:
    - Type: script
      Value: https://raw.githubusercontent.com/aws-samples/aws-parallelcluster-monitoring/main/parallelcluster-setup/build-custom-ami-component.sh
```

> **If you host the script in your own S3 bucket**, grant the build instance read access:
> ```yaml
>   Iam:
>     AdditionalIamPolicies:
>       - Policy: arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess
> ```
> **Newer GPUs (e.g. B300):** the default `dcgm-exporter` image (4.9.0, with DCGM 4.7.0) covers B300. To pre-bake a different build, set `DCGM_EXPORTER_IMAGE` (by digest, to bypass the Docker 29.x OCI-index pull issue) in the component's environment — the script honors it, same as the boot-time installer. The override must be dcgm-exporter 4.9.0 or later, because `dcgm/counters.csv` uses the DCGM 4.7 field names.

## Step 4 — Build the image

```bash
pcluster build-image \
  --image-id monitoring-compute-alinux2023 \
  --image-configuration build-image-config.yaml \
  --region <your-region>
```

Building takes up to ~1 hour (EC2 Image Builder provisions an instance, runs the component, creates the AMI, then launches and tests it).

Monitor progress:

```bash
# Overall status
pcluster describe-image --image-id monitoring-compute-alinux2023 --region <your-region>

# Tail build logs
pcluster list-image-log-streams --image-id monitoring-compute-alinux2023 --region <your-region>
pcluster get-image-log-events --image-id monitoring-compute-alinux2023 \
  --log-stream-name <stream-name> --region <your-region>
```

When the status is `BUILD_COMPLETE`, `describe-image` returns the new `amiId`. Note it for the next step.

## Step 5 — Reference the custom AMI in your cluster configuration

Point your compute queues at the custom AMI with `Image / CustomAmi`. Set it per queue (recommended, so the head node keeps the official AMI), or globally under the top-level `Image` section.

```yaml
Scheduling:
  Scheduler: slurm
  SlurmQueues:
    - Name: queue0
      Image:
        CustomAmi: ami-0abc123customami456   # the amiId from Step 4
      ComputeResources:
        - Name: queue0-compute-resource-0
          Instances:
            - InstanceType: c5n.large
          MinCount: 0
          MaxCount: 4
      # keep your existing Networking / Iam / CustomActions (OnNodeConfigured) here
```

Keep the existing `OnNodeConfigured` post-install action in place (see [the Quickstart in the README](../README.md#quickstart--parallelcluster)). On a custom AMI the installer detects the pre-baked runtime and short-circuits:

- It **skips the entire per-OS bootstrap** — no docker install and no Compose plugin download — logging *"Container runtime already present (pre-baked AMI) — skipping OS bootstrap"*.
- `docker compose ... up -d` starts the containers from the **pre-baked images** instead of pulling them. On GPU nodes this avoids the large `dcgm-exporter` download, and the NVIDIA Container Toolkit install is a no-op.

This behavior is automatic (added in `installer/install.sh` via `docker_runtime_ready`); no extra flag or slimmed script is needed. It needs installer v2.11 or later, the version that `Args` selects. With an older release the node runs the full OS bootstrap on every boot, and the custom AMI saves only the image pulls.

These steps still run on every compute-node boot, even on the fast path:

- ParallelCluster downloads `post-install.sh` from GitHub, and `post-install.sh` downloads the monitoring tree from GitHub. With `Args: latest` it also calls the GitHub API to resolve the newest release.
- On GPU nodes, the installer downloads the NVIDIA Container Toolkit repository definition, runs the package manager against that repository (the toolkit install itself is a no-op), runs `nvidia-ctk runtime configure` and restarts Docker to activate the NVIDIA runtime.

The head/login node still needs the full installer (it configures Prometheus, Grafana, TLS, SSM, etc.), so leave it on the official AMI unless you also pre-bake its images. On a head or login node, the fast path also installs `jq` and `bc` if the AMI lacks them.

## Maintenance reminder

Custom AMIs are pinned to a ParallelCluster version. **Each time you upgrade the ParallelCluster CLI**, rebuild the AMI (repeat Steps 1–4 with the new version's base image) and update `CustomAmi` in your cluster config. Otherwise cluster create/update fails validation because of the version mismatch. Bump container/tool versions in the repo's `compose/*.yml` and `installer/` — the build component reads them from there, so a rebuilt AMI automatically matches the boot-time installer.

## References

- [Building a custom AWS ParallelCluster AMI](https://docs.aws.amazon.com/parallelcluster/latest/ug/building-custom-ami-v3.html)
- [AWS ParallelCluster AMI customization](https://docs.aws.amazon.com/parallelcluster/latest/ug/custom-ami-v3.html)
- [`pcluster build-image` command reference](https://docs.aws.amazon.com/parallelcluster/latest/ug/pcluster.build-image-v3.html)
- [Build section reference](https://docs.aws.amazon.com/parallelcluster/latest/ug/Build-v3.html)
