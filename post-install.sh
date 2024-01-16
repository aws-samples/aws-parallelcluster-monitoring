#!/bin/bash
#
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Usage: ./post-install [version]

#Load AWS Parallelcluster environment variables
. /etc/parallelcluster/cfnconfig

version=${1:-v0.9}
monitoring_dir_name=aws-parallelcluster-monitoring
monitoring_tarball="${monitoring_dir_name}.tar.gz"

#get GitHub repo to clone and the installation script
monitoring_url=https://github.com/gallanik/aws-parallelcluster-monitoring/archive/refs/tags/${version}.tar.gz
setup_command=install-monitoring.sh
monitoring_home="/home/${cfn_cluster_user}/${monitoring_dir_name}"

case ${cfn_node_type} in
    HeadNode | MasterServer)
        wget ${monitoring_url} -O ${monitoring_tarball}
        mkdir -p ${monitoring_home}
        tar xvf ${monitoring_tarball} -C ${monitoring_home} --strip-components 1
    ;;
    ComputeFleet)
    
    ;;
esac

#Execute the monitoring installation script
bash -x "${monitoring_home}/parallelcluster-setup/${setup_command}" >/tmp/monitoring-setup.log 2>&1
exit $?
