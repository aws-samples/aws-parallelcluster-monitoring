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
computeInstanceType=$(aws cloudformation describe-stacks --stack-name $stack_name --region $cfn_region | jq -r '.Stacks[0].Parameters | map(select(.ParameterKey == "ComputeInstanceType"))[0].ParameterValue')

compute_node_h_price=$(aws pricing get-products \
    --region us-east-1 \
    --service-code AmazonEC2 \
    --filters 'Type=TERM_MATCH,Field=instanceType,Value='$computeInstanceType \
              'Type=TERM_MATCH,Field=location,Value='"${aws_region_long_name}" \
              'Type=TERM_MATCH,Field=preInstalledSw,Value=NA' \
              'Type=TERM_MATCH,Field=operatingSystem,Value=Linux' \
              'Type=TERM_MATCH,Field=tenancy,Value=Shared' \
              'Type=TERM_MATCH,Field=capacitystatus,Value=UnusedCapacityReservation' \
    --output text \
    --query 'PriceList' \
    | jq -r '.terms.OnDemand | to_entries[] | .value.priceDimensions | to_entries[] | .value.pricePerUnit.USD')

#ebs_volume_id=$(aws ec2 describe-instances     --instance-ids $computeInstanceId \
#              | jq -r '.Reservations | to_entries[].value | .Instances | to_entries[].value | .BlockDeviceMappings | to_entries[].value | .Ebs.VolumeId' \
#              | tail -1) #remove this tail

#ebs_volume_type=$(aws ec2 describe-volumes --volume-ids $ebs_volume_id | jq -r '.Volumes | to_entries[].value.VolumeType')
#ebs_volume_iops=$(aws ec2 describe-volumes --volume-ids $ebs_volume_id | jq -r '.Volumes | to_entries[].value.Iops')
ebs_volume_size=$(aws cloudformation describe-stacks --stack-name $stack_name --region $cfn_region | jq -r '.Stacks[0].Parameters | map(select(.ParameterKey == "ComputeRootVolumeSize"))[0].ParameterValue')

#check if volumeApiName can chane in the future, for now "gp2" is hardcoded
ebs_cost_gb_month=$(aws --region us-east-1 pricing get-products \
  --service-code AmazonEC2 \
  --query 'PriceList' \
  --output text \
  --filters 'Type=TERM_MATCH,Field=location,Value='"${aws_region_long_name}" \
          'Type=TERM_MATCH,Field=productFamily,Value=Storage' \
          'Type=TERM_MATCH,Field=volumeApiName,Value=gp2' \
  | jq -r '.terms.OnDemand | to_entries[] | .value.priceDimensions | to_entries[] | .value.pricePerUnit.USD')

total_num_compute_nodes=$(/opt/slurm/bin/sinfo -O "nodes" --noheader)
compute_ebs_volume_cost=$(echo "scale=2; $ebs_cost_gb_month * $total_num_compute_nodes * $ebs_volume_size / 720" | bc)
compute_nodes_cost=$(echo "scale=2; $total_num_compute_nodes * $compute_node_h_price" | bc)


echo "ebs_compute_cost $compute_ebs_volume_cost" | curl --data-binary @- http://127.0.0.1:9091/metrics/job/cost
echo "compute_nodes_cost $compute_nodes_cost" | curl --data-binary @- http://127.0.0.1:9091/metrics/job/cost