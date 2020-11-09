#!/bin/bash
#
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
#

#Load AWS Parallelcluster environment variables
. /etc/parallelcluster/cfnconfig

#get git-hib repo to clone and the installation script
github_repo=$(echo ${cfn_postinstall_args}| cut -d ',' -f 1 )
setup_command=$(echo ${cfn_postinstall_args}| cut -d ',' -f 2 )
monitoring_dir_name=$(basename -s .git ${github_repo})

case ${cfn_node_type} in
    MasterServer)
        cd /home/$cfn_cluster_user/
        git clone ${github_repo}
    ;;
    ComputeFleet)
    
    ;;
esac

#Execute the monitoring installation script
bash -x "/home/${cfn_cluster_user}/${monitoring_dir_name}/${setup_command}" >/tmp/monitoring-setup.log 2>&1
exit $?
