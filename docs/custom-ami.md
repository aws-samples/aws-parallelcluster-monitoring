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

For that reason **we do not publish a prebuilt AMI**. This page documents the procedure so you can build one against the exact ParallelCluster version and OS you run, and rebuild it whenever you upgrade ParallelCluster. (This solution supports ParallelCluster 3.10–3.15 on AL2, AL2023, Ubuntu 22.04/24.04, and RHEL 9.)

> When you adopt a new ParallelCluster release, repeat these steps to produce a matching AMI and update `CustomAmi` in your cluster config.

## Expected boot time impact

The figures below are **approximate, order-of-magnitude** estimates for the extra time the monitoring setup adds to a compute node's boot. Actual values depend on instance type, region, network, and image sizes (the `dcgm-exporter` image is large) — measure in your own environment before drawing conclusions.

| Compute node type | Standard method (installer runs on every boot) | Custom AMI (packages + images pre-baked) |
|---|---|---|
| CPU node | ~60–120 s of extra boot time (docker + compose install, node-exporter image pull) | ~5–15 s (only `docker compose up -d` against cached images) |
| GPU node | ~3–6 min of extra boot time (adds NVIDIA Container Toolkit install, Docker restart, large `dcgm-exporter` image pull) | ~15–40 s |

**Benefits of the custom AMI approach:**

- Compute nodes become ready for Slurm much faster, so jobs start sooner and autoscaling is more responsive.
- No per-boot dependency on external endpoints (GitHub, quay.io, nvcr.io, the NVIDIA repo), which removes a class of transient boot failures and rate-limit issues.
- More predictable, repeatable boot times across the fleet.

**Trade-off:** you take on the maintenance of rebuilding the AMI for each ParallelCluster version you upgrade to (see note above).

## Prerequisites

- The [AWS ParallelCluster CLI](https://docs.aws.amazon.com/parallelcluster/latest/ug/install-v3.html) installed, at the **same version** you use to create your cluster:
  ```bash
  pcluster version
  ```
- Credentials for an account/region where EC2 Image Builder can run (the build launches a temporary EC2 instance).
- The build component script from this repo, [`parallelcluster-setup/build-custom-ami-component.sh`](../parallelcluster-setup/build-custom-ami-component.sh). It reuses this repo's own installer to install Docker + the Compose plugin and pre-pulls the exact pinned images from the `compose/` files, and — when it detects an NVIDIA GPU on the build instance — installs the NVIDIA Container Toolkit and pre-pulls `dcgm-exporter`.

## Step 1 — Find the official ParallelCluster base AMI for your version

The custom AMI must be built on the official ParallelCluster AMI matching your CLI version and target OS. List them:

```bash
pcluster list-official-images --region <your-region> --os alinux2023
```

Note the `amiId` returned for your OS (e.g. `ami-0123456789abcdef0`) — you'll use it as the `ParentImage` below. Supported `--os` values for this solution: `alinux2`, `alinux2023`, `ubuntu2204`, `ubuntu2404`, `rhel9`.

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
> **Newer GPUs (e.g. B300):** the default `dcgm-exporter` image covers up to B200. To pre-bake a DCGM ≥ 4.4.0 build, set `DCGM_EXPORTER_IMAGE` (by digest, to bypass the Docker 29.x OCI-index pull issue) in the component's environment — the script honors it, same as the boot-time installer.

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

Keep the existing `OnNodeConfigured` post-install action in place (see [the Quickstart in the README](../README.md#quickstart--parallelcluster)). With the custom AMI:

- The OS package installs become fast no-ops because Docker, the Compose plugin, and (on GPU AMIs) the NVIDIA Container Toolkit are already present.
- `docker compose ... up -d` starts the containers from the **pre-pulled images** instead of downloading them.

That combination is where the boot-time savings come from. The head/login node still needs the full installer (it configures Prometheus, Grafana, TLS, SSM, etc.), so leave it on the official AMI unless you also pre-bake its images.

### Optional: trim the boot path further

If you want to squeeze out the last few seconds on compute nodes, you can maintain a slimmed variant of the compute-node bootstrap that only runs `docker compose -f compose/compute[.gpu].yml -p monitoring-compute up -d` against the cached images and skips the install steps entirely. This is optional — the standard installer already benefits from the cached packages and images.

## Maintenance reminder

Custom AMIs are pinned to a ParallelCluster version. **Each time you upgrade the ParallelCluster CLI**, rebuild the AMI (repeat Steps 1–4 with the new version's base image) and update `CustomAmi` in your cluster config. Otherwise cluster create/update fails validation because of the version mismatch. Bump container/tool versions in the repo's `compose/*.yml` and `installer/` — the build component reads them from there, so a rebuilt AMI automatically matches the boot-time installer.

## References

- [Building a custom AWS ParallelCluster AMI](https://docs.aws.amazon.com/parallelcluster/latest/ug/building-custom-ami-v3.html)
- [AWS ParallelCluster AMI customization](https://docs.aws.amazon.com/parallelcluster/latest/ug/custom-ami-v3.html)
- [`pcluster build-image` command reference](https://docs.aws.amazon.com/parallelcluster/latest/ug/pcluster.build-image-v3.html)
- [Build section reference](https://docs.aws.amazon.com/parallelcluster/latest/ug/Build-v3.html)
