#!/bin/bash -i
#
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
#

#source the AWS ParallelCluster profile
. /etc/parallelcluster/cfnconfig

monitoring_dir_name=aws-parallelcluster-monitoring
monitoring_home="/home/${cfn_cluster_user}/${monitoring_dir_name}"

echo "$> variable monitoring_dir_name -> ${monitoring_dir_name}"
echo "$> variable monitoring_home -> ${monitoring_home}"


case "${cfn_node_type}" in
	HeadNode | MasterServer)

		#cfn_efs=$(cat /etc/chef/dna.json | grep \"cfn_efs\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")
		#cfn_cluster_cw_logging_enabled=$(cat /etc/chef/dna.json | grep \"cfn_cluster_cw_logging_enabled\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")
		cfn_fsx_fs_id=$(cat /etc/chef/dna.json | grep \"cfn_fsx_fs_id\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")
		TOKEN=$(sudo curl -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
		master_instance_id=$(sudo curl -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
		cfn_max_queue_size=$(aws cloudformation describe-stacks --stack-name $stack_name --region $cfn_region | jq -r '.Stacks[0].Parameters | map(select(.ParameterKey == "MaxSize"))[0].ParameterValue')
		s3_bucket=$(echo $cfn_postinstall | sed "s/s3:\/\///g;s/\/.*//")
		cluster_s3_bucket=$(cat /etc/chef/dna.json | grep \"cluster_s3_bucket\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")
		cluster_config_s3_key=$(cat /etc/chef/dna.json | grep \"cluster_config_s3_key\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")
		cluster_config_version=$(cat /etc/chef/dna.json | grep \"cluster_config_version\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")
		user_log_group=$(sudo aws logs describe-log-groups --log-group-name-prefix "/aws/parallelcluster/$stack_name" --query 'reverse(sort_by(logGroups,&creationTime))[0].logGroupName' --output text)
		IFS="/" read -ra names <<< "$user_log_group"
		log_group_name="\/aws\/parallelcluster\/${names[3]}"

		aws s3api get-object --bucket $cluster_s3_bucket --key $cluster_config_s3_key --region $cfn_region --version-id $cluster_config_version ${monitoring_home}/parallelcluster-setup/cluster-config.json

		chown $cfn_cluster_user:$cfn_cluster_user -R /home/$cfn_cluster_user
		chmod +x ${monitoring_home}/custom-metrics/*

		cp -rp ${monitoring_home}/custom-metrics/* /usr/local/bin/
		mv ${monitoring_home}/prometheus-slurm-exporter/slurm_exporter.service /etc/systemd/system/

		# replace tokens
		sed -i "s/_S3_BUCKET_/${s3_bucket}/g"               	${monitoring_home}/grafana/dashboards/ParallelCluster.json
		sed -i "s/__INSTANCE_ID__/${master_instance_id}/g"  	${monitoring_home}/grafana/dashboards/ParallelCluster.json
		sed -i "s/__FSX_ID__/${cfn_fsx_fs_id}/g"            	${monitoring_home}/grafana/dashboards/ParallelCluster.json
		sed -i "s/__AWS_REGION__/${cfn_region}/g"           	${monitoring_home}/grafana/dashboards/ParallelCluster.json

		sed -i "s/__AWS_REGION__/${cfn_region}/g"           	${monitoring_home}/grafana/dashboards/logs.json
		sed -i "s/__LOG_GROUP__NAMES__/${log_group_name}/g"    ${monitoring_home}/grafana/dashboards/logs.json

		sed -i "s/__Application__/${stack_name}/g"          	${monitoring_home}/prometheus/prometheus.yml
		sed -i "s/__AWS_REGION__/${cfn_region}/g"          		${monitoring_home}/prometheus/prometheus.yml
		sed -i "s/__CLUSTER_NAME__/${stack_name}/g"						${monitoring_home}/prometheus/prometheus.yml

		sed -i "s/__INSTANCE_ID__/${master_instance_id}/g"  	${monitoring_home}/grafana/dashboards/master-node-details.json
		sed -i "s/__INSTANCE_ID__/${master_instance_id}/g"  	${monitoring_home}/grafana/dashboards/compute-node-list.json
		sed -i "s/__INSTANCE_ID__/${master_instance_id}/g"  	${monitoring_home}/grafana/dashboards/compute-node-details.json

		sed -i "s/__MONITORING_DIR__/${monitoring_dir_name}/g"  ${monitoring_home}/docker-compose/docker-compose.master.yml

		#Generate selfsigned certificate for Nginx over ssl
		nginx_dir="${monitoring_home}/nginx"
		nginx_ssl_dir="${nginx_dir}/ssl"
		mkdir -p ${nginx_ssl_dir}
		# echo -e "\nDNS.1=$(sudo curl http://169.254.169.254/latest/meta-data/public-ipv4)" >> "${nginx_dir}/openssl.cnf"
		openssl req -new -x509 -nodes -newkey rsa:4096 -days 3650 -keyout "${nginx_ssl_dir}/nginx.key" -out "${nginx_ssl_dir}/nginx.crt" -config "${nginx_dir}/openssl.cnf"

		#give $cfn_cluster_user ownership
		chown -R $cfn_cluster_user:$cfn_cluster_user "${nginx_ssl_dir}/nginx.key"
		chown -R $cfn_cluster_user:$cfn_cluster_user "${nginx_ssl_dir}/nginx.crt"

		/usr/local/bin/docker-compose --env-file /etc/parallelcluster/cfnconfig -f ${monitoring_home}/docker-compose/docker-compose.master.yml -p monitoring-master up -d

		# Download and build prometheus-slurm-exporter
		##### Plese note this software package is under GPLv3 License #####
		# More info here: https://github.com/vpenso/prometheus-slurm-exporter/blob/master/LICENSE
		cd ${monitoring_home}
		git clone https://github.com/vpenso/prometheus-slurm-exporter.git
		# the export and git config statements below for compatibility on Ubuntu 22.x
		export HOME=/root
		git config --global --add safe.directory ${monitoring_home}/prometheus-slurm-exporter
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
		compute_instance_type=$(sudo curl -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-type)
		gpu_instances="[pg][2-9].*\.[0-9]*[x]*large"
		echo "$> Compute Instances Type EC2 -> ${compute_instance_type}"
		echo "$> GPUS Instances EC2 -> ${gpu_instances}"
		if [[ $compute_instance_type =~ $gpu_instances ]]; then
			/usr/local/bin/docker-compose -f /home/${cfn_cluster_user}/${monitoring_dir_name}/docker-compose/docker-compose.compute.gpu.yml -p monitoring-compute up -d
        else
			/usr/local/bin/docker-compose -f /home/${cfn_cluster_user}/${monitoring_dir_name}/docker-compose/docker-compose.compute.yml -p monitoring-compute up -d
        fi
	;;
esac
