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
		touch /home/centos/tonto
		cat >/home/centos/stultus
		echo ${cfn_postinstall_args} >> /home/centos/tonto
		echo ${monitoring_dir_name} >> /home/centos/tonto
		echo ${monitoring_tarball} >> /home/centos/tonto
		echo ${monitoring_home} >> /home/centos/tonto
		echo >> /home/centos/tonto
		echo ${monitoring_url} >> /home/centos/tonto
		cd /home/centos
		wget ${monitoring_url} -O ${monitoring_tarball}
###		mkdir -p ${monitoring_home}
		tar -xvf /home/centos/coderodyhpc-aws-parallelcluster-monitoring.tar.gz -C ${monitoring_home} --strip-components 1
		echo ${cfn_base_os} >> /home/centos/tonto
	;;
	ComputeFleet)

	;;
esac

#
bash -x "${monitoring_home}/parallelcluster-setup/${setup_command}" >/tmp/monitoring-setup.log 2>&1
exit
