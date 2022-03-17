#!/bin/bash
#
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
#

#Load AWS Parallelcluster environment variables
. /etc/parallelcluster/cfnconfig

#get GitHub repo to clone and the installation script
monitoring_url=${cfn_postinstall_args[0]}
monitoring_dir_name=${cfn_postinstall_args[1]}
monitoring_tarball="${monitoring_dir_name}.tar.gz"
setup_command=${cfn_postinstall_args[2]}
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
