#!/bin/bash

#source the AWS ParallelCluster profile
. /etc/parallelcluster/cfnconfig

yum -y install docker glibc-static
service docker start
chkconfig docker on
usermod -a -G docker $cfn_cluster_user

#to be replaced with yum -y install docker-compose as the repository problem is fixed
curl -L "https://github.com/docker/compose/releases/download/1.27.4/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose


case "${cfn_node_type}" in
MasterServer)

# Install docker to simplify Prometheus & Grafana deployment
yum -y install git golang-bin make

wget "${2}"

#Unsupported
#cfn_efs=$(cat /etc/chef/dna.json | grep \"cfn_efs\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")
#cfn_cluster_cw_logging_enabled=$(cat /etc/chef/dna.json | grep \"cfn_cluster_cw_logging_enabled\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")
cfn_fsx_fs_id=$(cat /etc/chef/dna.json | grep \"cfn_fsx_fs_id\" | awk '{print $2}' | sed "s/\",//g;s/\"//g")

#Supported
master_instance_id=$(ec2-metadata -i | awk '{print $2}')
cfn_max_queue_size=$(aws cloudformation describe-stacks --stack-name $stack_name --region $cfn_region | jq -r '.Stacks[0].Parameters | map(select(.ParameterKey == "MaxSize"))[0].ParameterValue')
s3_bucket=$(echo $cfn_postinstall | sed "s/s3:\/\///g;s/\/.*//")

yum -y install git golang-bin make 

wget "${2}"
unzip main.zip ../Grafana/grafana/*
unzip main.zip ../Grafana/nginx/*
unzip main.zip ../Grafana/www/*
unzip main.zip ../Grafana/docker-compose/*
unzip main.zip ../Grafana/prometheus/*
mv Grafana/* /home/$cfn_cluster_user/

unzip -j main.zip ../Grafana/custom-metrics/1h-cost-metrics.sh                  -d /usr/local/bin/
unzip -j main.zip ../Grafana/custom-metrics/1m-cost-metrics.sh                  -d /usr/local/bin/
unzip -j main.zip ../Grafana/custom-metrics/aws-region.py                       -d /usr/local/bin/
unzip -j main.zip ../Grafana/prometheus-slurm-exporter/slurm_exporter.service   -d /etc/systemd/system/

chmod +x /usr/local/bin/1h-cost-metrics.sh 
chmod +x /usr/local/bin/1m-cost-metrics.sh 

chown $cfn_cluster_user:$cfn_cluster_user /usr/local/bin/1h-cost-metrics.sh 
chown $cfn_cluster_user:$cfn_cluster_user /usr/local/bin/1m-cost-metrics.sh
chown $cfn_cluster_user:$cfn_cluster_user /usr/local/bin/aws-region.py

chown $cfn_cluster_user:$cfn_cluster_user -R /home/$cfn_cluster_user/

(crontab -l -u $cfn_cluster_user; echo "*/1 * * * * /usr/local/bin/1m-cost-metrics.sh") | crontab -u $cfn_cluster_user -
(crontab -l -u $cfn_cluster_user; echo "*/60 * * * * /usr/local/bin/1h-cost-metrics.sh") | crontab -u $cfn_cluster_user - 


# replace tokens 
sed -i "s/_S3_BUCKET_/${s3_bucket}/g"               /home/$cfn_cluster_user/grafana/dashboards/ParallelCluster.json
sed -i "s/__INSTANCE_ID__/${master_instance_id}/g"  /home/$cfn_cluster_user/grafana/dashboards/ParallelCluster.json 
sed -i "s/__FSX_ID__/${cfn_fsx_fs_id}/g"            /home/$cfn_cluster_user/grafana/dashboards/ParallelCluster.json
sed -i "s/__AWS_REGION__/${cfn_region}/g"           /home/$cfn_cluster_user/grafana/dashboards/ParallelCluster.json 

sed -i "s/__AWS_REGION__/${cfn_region}/g"           /home/$cfn_cluster_user/grafana/dashboards/logs.json 

sed -i "s/__Application__/${stack_name}/g"          /home/$cfn_cluster_user/prometheus/prometheus.yml 

sed -i "s/__INSTANCE_ID__/${master_instance_id}/g"  /home/$cfn_cluster_user/grafana/dashboards/master-node-details.json
sed -i "s/__INSTANCE_ID__/${master_instance_id}/g"  /home/$cfn_cluster_user/grafana/dashboards/compute-node-list.json 
sed -i "s/__INSTANCE_ID__/${master_instance_id}/g"  /home/$cfn_cluster_user/grafana/dashboards/compute-node-details.json 


#Generate selfsigned certificate for Nginx over ssl
mkdir -p /home/$cfn_cluster_user/nginx/ssl
echo -e "\nDNS.1=$(ec2-metadata -p | awk '{print $2}')" >> /home/$cfn_cluster_user/nginx/openssl.cnf 
openssl req -new -x509 -nodes -newkey rsa:4096 -days 3650 -keyout /home/$cfn_cluster_user/nginx/ssl/nginx.key -out /home/$cfn_cluster_user/nginx/ssl/nginx.crt -config /home/$cfn_cluster_user/nginx/openssl.cnf 

#give $cfn_cluster_user ownership of new stuff on its own home dir
chown -R $cfn_cluster_user:$cfn_cluster_user /home/$cfn_cluster_user/

docker-compose --env-file /etc/parallelcluster/cfnconfig -f /home/$cfn_cluster_user/docker-compose/docker-compose.master.yml -p grafana-master up -d

# Download and build prometheus-slurm-exporter 
##### Plese note this software package is under GPLv3 License #####
# More info here: https://github.com/vpenso/prometheus-slurm-exporter/blob/master/LICENSE
git clone https://github.com/vpenso/prometheus-slurm-exporter.git
cd prometheus-slurm-exporter
GOPATH=/root/go-modules-cache HOME=/root go mod download
GOPATH=/root/go-modules-cache HOME=/root go build
cp /tmp/prometheus-slurm-exporter/prometheus-slurm-exporter /usr/bin/prometheus-slurm-exporter

systemctl daemon-reload
systemctl enable slurm_exporter
systemctl start slurm_exporter


;;
ComputeFleet)

docker-compose --env-file /etc/parallelcluster/cfnconfig -f /home/$cfn_cluster_user/docker-compose/docker-compose.compute.yml -p grafana-compute up -d

;;
esac
