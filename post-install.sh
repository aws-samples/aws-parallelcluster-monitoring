#!/bin/bash
#
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Usage: ./post-install [version]

#Load AWS Parallelcluster environment variables
. /etc/parallelcluster/cfnconfig

version=${1:-main}
monitoring_dir_name=aws-parallelcluster-monitoring
monitoring_tarball="${monitoring_dir_name}.tar.gz"

# get GitHub repo to clone and the installation script
monitoring_url=https://github.com/aws-samples/aws-parallelcluster-monitoring/tarball/${version}
setup_command=install-monitoring.sh
monitoring_home="/home/${cfn_cluster_user}/${monitoring_dir_name}"
setup_command_path="${monitoring_home}/parallelcluster-setup"

case ${cfn_node_type} in
    HeadNode | MasterServer)
        wget ${monitoring_url} -O ${monitoring_tarball}
        mkdir -p ${monitoring_home}
        tar xvf ${monitoring_tarball} -C ${monitoring_home} --strip-components 1
    ;;
    ComputeFleet)
    
    ;;
esac

OS=$(. /etc/os-release; echo $NAME)
if [ "${OS}" = "Ubuntu" ]; then
    systemctl stop apache2
    systemctl disable apache2
    sed \
        -e "s/yum -y install docker/apt-get install docker.io -y/g" \
        -e "s/yum -y install golang-bin/apt-get install golang-go -y/g" \
        -e "s/ec2-metadata -i | awk '{print \$2}'/ec2metadata --instance-id/g" \
        -e "s/ec2-metadata -p | awk '{print \$2}'/ec2metadata --public-hostname/g" \
        -e "s/ec2-metadata -t | awk '{print \$2}'/ec2metadata --instance-type/g" \
        "${setup_command_path}/${setup_command}" \
        > "${setup_command_path}/tmp-${setup_command}"
    bash -x "${setup_command_path}/tmp-${setup_command}" | tee /tmp/monitoring-setup.log 2>&1
else
    bash -x "${setup_command_path}/${setup_command}" | tee /tmp/monitoring-setup.log 2>&1
fi
exit $?
