#!/bin/bash
#
#

# Env variable
. /etc/parallelcluster/cfnconfig

monitoring_url=$(echo ${cfn_postinstall_args}| cut -d ',' -f 1 )
monitoring_dir_name=$(echo ${cfn_postinstall_args}| cut -d ',' -f 2 )
monitoring_tarball="${monitoring_dir_name}.tar.gz"
setup_command=$(echo ${cfn_postinstall_args}| cut -d ',' -f 3 )
monitoring_home="/home/${cfn_cluster_user}/${monitoring_dir_name}"

case ${cfn_node_type} in
	MasterServer)
		cd /home/centos
		wget ${monitoring_url} -O ${monitoring_tarball}
		mkdir -p ${monitoring_home}
		tar xvf ${monitoring_tarball} -C ${monitoring_home} --strip-components 1
	;;
	ComputeFleet)

	;;
esac

#
bash -x "${monitoring_home}/parallelcluster-setup/${setup_command}" >/tmp/monitoring-setup.log 2>&1
exit
