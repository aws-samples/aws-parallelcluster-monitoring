#!/bin/bash
#
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
#

#source the AWS ParallelCluster profile
. /etc/parallelcluster/cfnconfig

export AWS_DEFAULT_REGION=$cfn_region
aws_region_long_name=$(python /usr/local/bin/aws-region.py $cfn_region)

monitoring_dir_name=$(echo ${cfn_postinstall_args}| cut -d ',' -f 2 )
monitoring_home="/home/${cfn_cluster_user}/${monitoring_dir_name}"

queues=$(/opt/slurm/bin/sinfo --noheader -O partition  | sed 's/\*//g')
cluster_config_file="${monitoring_home}/parallelcluster-setup/cluster-config.json"

compute_nodes_total_cost=0

for queue in $queues; do 

  instance_type=$(cat "${cluster_config_file}" | jq -r --arg queue $queue '.cluster.queue_settings | to_entries[] | select(.key==$queue).value.compute_resource_settings | to_entries[]| .value.instance_type')

  compute_node_h_price=$(aws pricing get-products \
    --region ${cfn_region} \
    --service-code AmazonEC2 \
    --filters 'Type=TERM_MATCH,Field=instanceType,Value='$instance_type \
              'Type=TERM_MATCH,Field=location,Value='"${aws_region_long_name}" \
              'Type=TERM_MATCH,Field=preInstalledSw,Value=NA' \
              'Type=TERM_MATCH,Field=operatingSystem,Value=Linux' \
              'Type=TERM_MATCH,Field=tenancy,Value=Shared' \
              'Type=TERM_MATCH,Field=capacitystatus,Value=UnusedCapacityReservation' \
    --output text \
    --query 'PriceList' \
    | jq -r '.terms.OnDemand | to_entries[] | .value.priceDimensions | to_entries[] | .value.pricePerUnit.USD')

  ebs_cost_gb_month=$(aws --region ${cfn_region} pricing get-products \
    --service-code AmazonEC2 \
    --query 'PriceList' \
    --output text \
    --filters 'Type=TERM_MATCH,Field=location,Value='"${aws_region_long_name}" \
              'Type=TERM_MATCH,Field=productFamily,Value=Storage' \
              'Type=TERM_MATCH,Field=volumeApiName,Value=gp2' \
    | jq -r '.terms.OnDemand | to_entries[] | .value.priceDimensions | to_entries[] | .value.pricePerUnit.USD')

  total_num_compute_nodes=$(/opt/slurm/bin/sinfo --noheader  --partition=$queue  | egrep  -v "idle~" | awk '{sum += $4} END {if (sum) print sum; else print 0; }')

  ebs_volume_size=$(aws cloudformation describe-stacks --stack-name $stack_name --region $cfn_region | jq -r '.Stacks[0].Parameters | map(select(.ParameterKey == "ComputeRootVolumeSize"))[0].ParameterValue')
  compute_ebs_volume_cost=$(echo "scale=2; $ebs_cost_gb_month * $total_num_compute_nodes * $ebs_volume_size / 720" | bc)
  compute_nodes_cost=$(echo "scale=2; $total_num_compute_nodes * $compute_node_h_price" | bc)
  
  compute_nodes_total_cost=$(echo "scale=2; $compute_nodes_total_cost + $compute_nodes_cost" | bc)

done 

echo "ebs_compute_cost $compute_ebs_volume_cost"    | curl --data-binary @- http://127.0.0.1:9091/metrics/job/cost
echo "compute_nodes_cost $compute_nodes_total_cost" | curl --data-binary @- http://127.0.0.1:9091/metrics/job/cost