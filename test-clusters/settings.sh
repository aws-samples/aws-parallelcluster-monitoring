# shellcheck shell=bash disable=SC2034  # the scripts that source this file use the variables
#
# Parameters of the test-cluster scripts, sourced by each of them. Set them in
# the environment or in config.env next to this file, which git ignores (see
# config.env.example). A value in config.env replaces the environment's.
#
#   REGION         AWS Region of the clusters. Default: AWS_REGION, then
#                  AWS_DEFAULT_REGION.
#   SUBNET_ID      Subnet for every node (required to create). It needs a
#                  route to the internet, through an internet gateway or a
#                  NAT gateway, for the installer's downloads and for SSM.
#   REF            Tag or branch of the monitoring stack to install. Default:
#                  main.
#   REPO           GitHub owner/repo to install it from. Default:
#                  aws-samples/aws-parallelcluster-monitoring.
#   PREFIX         Prefix of the cluster and resource names. Default:
#                  monitoring-test.
#   KEY_NAME       EC2 key pair for SSH. Optional: the scripts connect through
#                  SSM.
#   FSX_ID         Existing FSx for Lustre file system that the ParallelCluster
#                  cluster mounts at /shared. Optional. Its security group
#                  must accept Lustre traffic from the cluster's nodes.
#   SLURM_VERSION  Slurm version of the AWS PCS cluster. Default: 26.05.
#   AMI_ID         AMI of the AWS PCS login and CPU nodes. Default: the newest
#                  AWS PCS sample AMI for Amazon Linux 2023 (x86_64).
#   GPU_AMI_ID     AMI of the AWS PCS GPU node. Default: the newest PCS-ready
#                  DLAMI for Ubuntu 24.04 (x86_64), which has the NVIDIA
#                  driver.
#   TAGS           Extra tags for every resource, as key=value pairs
#                  separated by commas. Optional.

TC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${TC_DIR}/config.env" ]]; then
    # shellcheck source=/dev/null
    . "${TC_DIR}/config.env"
fi
REGION="${REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
SUBNET_ID="${SUBNET_ID:-}"
REF="${REF:-main}"
REPO="${REPO:-aws-samples/aws-parallelcluster-monitoring}"
PREFIX="${PREFIX:-monitoring-test}"
KEY_NAME="${KEY_NAME:-}"
FSX_ID="${FSX_ID:-}"
SLURM_VERSION="${SLURM_VERSION:-26.05}"
AMI_ID="${AMI_ID:-}"
GPU_AMI_ID="${GPU_AMI_ID:-}"
TAGS="${TAGS:-}"

if [[ -z "${REGION}" ]]; then
    echo "Set REGION to the AWS Region of the test clusters (see settings.sh)" >&2
    exit 1
fi
export AWS_REGION="${REGION}" AWS_DEFAULT_REGION="${REGION}" AWS_PAGER=""

PC_CLUSTER="${PREFIX}-pc"
PCS_CLUSTER="${PREFIX}-pcs"
STATE="${TC_DIR}/.state"
TAG_KEY="aws-parallelcluster-monitoring-test"
POST_INSTALL_URL="https://raw.githubusercontent.com/${REPO}/${REF}/post-install.sh"

require() { # require VARIABLE DESCRIPTION
    if [[ -z "${!1}" ]]; then
        echo "Set $1 to $2 (see settings.sh)" >&2
        exit 1
    fi
}

# The tags of a resource: TAG_KEY=VALUE and TAGS, one key=value per line.
tag_lines() { # tag_lines VALUE
    printf '%s=%s\n' "${TAG_KEY}" "$1"
    if [[ -n "${TAGS}" ]]; then
        tr ',' '\n' <<<"${TAGS}"
    fi
}

# The tags as a JSON list of Key/Value objects (EC2, EFS, IAM).
tag_list() { # tag_list VALUE
    tag_lines "$1" | jq -R -s -c \
        '[split("\n")[] | select(length > 0) | capture("^(?<Key>[^=]+)=(?<Value>.*)$")]'
}

# The tags as a JSON object (AWS PCS).
tag_map() { # tag_map VALUE
    tag_list "$1" | jq -c 'map({key: .Key, value: .Value}) | from_entries'
}
