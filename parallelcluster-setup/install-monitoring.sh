#!/bin/bash -i
#
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
#

# Source the AWS ParallelCluster profile
. /etc/parallelcluster/cfnconfig

# install Docker
yum -y install docker
service docker start
chkconfig docker on
usermod -a -G docker $cfn_cluster_user

# Install Docker Compose
curl -L https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m) -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose
docker-compose version

monitoring_dir_name=aws-parallelcluster-monitoring
monitoring_home="/home/${cfn_cluster_user}/${monitoring_dir_name}"

echo "$> variable monitoring_dir_name -> ${monitoring_dir_name}"
echo "$> variable monitoring_home -> ${monitoring_home}"


case "${cfn_node_type}" in
	HeadNode | MasterServer)
		cfn_efs=$(cat /etc/chef/dna.json | grep \"cfn_efs\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")
		cw_logging_enabled=$(cat /etc/chef/dna.json | grep \"cw_logging_enabled\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")
		cfn_fsx_fs_id=$(cat /etc/chef/dna.json | grep \"cfn_fsx_fs_id\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")
		headnode_instance_id=$(ec2-metadata -i | awk '{print $2}')
		cfn_max_queue_size=$(aws cloudformation describe-stacks --stack-name $stack_name --region $cfn_region | jq -r '.Stacks[0].Parameters | map(select(.ParameterKey == "MaxSize"))[0].ParameterValue')
		cluster_s3_bucket=$(cat /etc/chef/dna.json | grep \"cluster_s3_bucket\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")
		cluster_config_s3_key=$(cat /etc/chef/dna.json | grep \"cluster_config_s3_key\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")
		cluster_config_version=$(cat /etc/chef/dna.json | grep \"cluster_config_version\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")
		log_group_names=$(cat /etc/chef/dna.json | grep \"log_group_name\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")

		aws s3api get-object --bucket $cluster_s3_bucket --key $cluster_config_s3_key --region $cfn_region --version-id $cluster_config_version ${monitoring_home}/parallelcluster-setup/cluster-config.yaml

		yum -y install golang-bin

		chown $cfn_cluster_user:$cfn_cluster_user -R /home/$cfn_cluster_user
		chmod +x ${monitoring_home}/custom-metrics/*

		cp -rp ${monitoring_home}/custom-metrics/* /usr/local/bin/
		mv ${monitoring_home}/prometheus-slurm-exporter/slurm_exporter.service /etc/systemd/system/

		# replace tokens
		sed -i "s/__INSTANCE_ID__/${headnode_instance_id}/g"  	${monitoring_home}/grafana/dashboards/ParallelCluster.json
		sed -i "s/__FSX_ID__/${cfn_fsx_fs_id}/g"            	${monitoring_home}/grafana/dashboards/ParallelCluster.json
		sed -i "s/__AWS_REGION__/${cfn_region}/g"           	${monitoring_home}/grafana/dashboards/ParallelCluster.json

		sed -i "s/__Application__/${stack_name}/g"          	${monitoring_home}/prometheus/prometheus.yml
		sed -i "s/__AWS_REGION__/${cfn_region}/g"          		${monitoring_home}/prometheus/prometheus.yml

		sed -i "s/__INSTANCE_ID__/${headnode_instance_id}/g"  	${monitoring_home}/grafana/dashboards/head-node-details.json
		sed -i "s/__INSTANCE_ID__/${headnode_instance_id}/g"  	${monitoring_home}/grafana/dashboards/compute-node-list.json
		sed -i "s/__INSTANCE_ID__/${headnode_instance_id}/g"  	${monitoring_home}/grafana/dashboards/compute-node-details.json

		sed -i "s/__MONITORING_DIR__/${monitoring_dir_name}/g"  ${monitoring_home}/docker-compose/docker-compose.headnode.yml

		# Generate self-signed certificate for Nginx over ssl
		nginx_dir="${monitoring_home}/nginx"
		nginx_ssl_dir="${nginx_dir}/ssl"
		mkdir -p ${nginx_ssl_dir}
		echo -e "\nDNS.1=$(ec2-metadata -p | awk '{print $2}')" >> "${nginx_dir}/openssl.cnf"
		openssl req -new -x509 -nodes -newkey rsa:4096 -days 3650 -keyout "${nginx_ssl_dir}/nginx.key" -out "${nginx_ssl_dir}/nginx.crt" -config "${nginx_dir}/openssl.cnf"

		# give $cfn_cluster_user ownership
		chown -R $cfn_cluster_user:$cfn_cluster_user "${nginx_ssl_dir}/nginx.key"
		chown -R $cfn_cluster_user:$cfn_cluster_user "${nginx_ssl_dir}/nginx.crt"

		docker-compose --env-file /etc/parallelcluster/cfnconfig -f ${monitoring_home}/docker-compose/docker-compose.headnode.yml -p monitoring-headnode up -d

		# Download and build prometheus-slurm-exporter
		##### Please note this software package is under GPLv3 License #####
		# More info here: https://github.com/vpenso/prometheus-slurm-exporter/blob/master/LICENSE
		cd ${monitoring_home}
		git clone https://github.com/vpenso/prometheus-slurm-exporter.git
		sed -i 's/NodeList,AllocMem,Memory,CPUsState,StateLong/NodeList: ,AllocMem: ,Memory: ,CPUsState: ,StateLong:/' prometheus-slurm-exporter/node.go
		cd prometheus-slurm-exporter
		GOPATH=/root/go-modules-cache HOME=/root go mod download
		GOPATH=/root/go-modules-cache HOME=/root go build
		mv ${monitoring_home}/prometheus-slurm-exporter/prometheus-slurm-exporter /usr/bin/prometheus-slurm-exporter

		systemctl daemon-reload
		systemctl enable slurm_exporter
		systemctl start slurm_exporter
	;;

	ComputeFleet)
		compute_instance_type=$(ec2-metadata -t | awk '{print $2}')
		echo "$> Compute Instances Type EC2 -> ${compute_instance_type}"
		/usr/local/bin/docker-compose -f /home/${cfn_cluster_user}/${monitoring_dir_name}/docker-compose/docker-compose.compute.yml -p monitoring-compute up -d
	;;
esac
