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

masterInstanceType=$(ec2-metadata -t | awk '{print $2}')
masterInstanceId=$(ec2-metadata -i | awk '{print $2}')
s3_bucket=$(echo $cfn_postinstall | sed "s/s3:\/\///g;s/\/.*//")
s3_size_gb=$(echo "$(aws s3api list-objects --bucket $s3_bucket --output json --query "[sum(Contents[].Size)]"| sed -n 2p | tr -d ' ') / 1024 / 1024 / 1024" | bc)


#retrieve the s3 cost
if [[ $s3_size_gb -le 51200 ]]; then
  s3_range=51200
elif [[ $VAR -le 512000 ]]; then
  s3_range=512000
else
  s3_range="Inf"
fi

####################### S3 #########################

s3_cost_gb_month=$(aws --region ${$cfn_region} pricing get-products \
  --service-code AmazonS3 \
  --filters 'Type=TERM_MATCH,Field=location,Value='"${aws_region_long_name}" \
            'Type=TERM_MATCH,Field=storageClass,Value=General Purpose' \
  --query 'PriceList[0]' --output text \
  | jq -r --arg endRange $s3_range '.terms.OnDemand | to_entries[] | .value.priceDimensions | to_entries[].value | select(.endRange==$endRange).pricePerUnit.USD')

s3=$(echo "scale=2; $s3_cost_gb_month * $s3_size_gb / 720" | bc)
echo "s3_cost $s3" | curl --data-binary @- http://127.0.0.1:9091/metrics/job/cost
  

####################### Master #########################
master_node_h_price=$(aws pricing get-products \
  --region ${$cfn_region} \
  --service-code AmazonEC2 \
  --filters 'Type=TERM_MATCH,Field=instanceType,Value='$masterInstanceType \
            'Type=TERM_MATCH,Field=location,Value='"${aws_region_long_name}" \
            'Type=TERM_MATCH,Field=preInstalledSw,Value=NA' \
            'Type=TERM_MATCH,Field=operatingSystem,Value=Linux' \
            'Type=TERM_MATCH,Field=tenancy,Value=Shared' \
            'Type=TERM_MATCH,Field=capacitystatus,Value=UnusedCapacityReservation' \
  --output text \
  --query 'PriceList' \
  | jq -r '.terms.OnDemand | to_entries[] | .value.priceDimensions | to_entries[] | .value.pricePerUnit.USD')
  
echo "master_node_cost $master_node_h_price" | curl --data-binary @- http://127.0.0.1:9091/metrics/job/cost
  

####################### FSX #########################
fsx_size_gb=$(aws cloudformation describe-stacks --stack-name $stack_name --region $cfn_region \
              | jq -r '.Stacks[0].Parameters | map(select(.ParameterKey == "FSXOptions"))[0].ParameterValue' \
              | awk -F "," '{print $3}')
              
fsx_type=$(aws cloudformation describe-stacks --stack-name $stack_name --region $cfn_region \
              | jq -r '.Stacks[0].Parameters | map(select(.ParameterKey == "FSXOptions"))[0].ParameterValue' \
              | awk -F "," '{print $9}')
            
fsx_throughput=$(aws cloudformation describe-stacks --stack-name $stack_name --region $cfn_region \
              | jq -r '.Stacks[0].Parameters | map(select(.ParameterKey == "FSXOptions"))[0].ParameterValue' \
              | awk -F "," '{print $10}')

if [[ $fsx_type = "SCRATCH_2" ]] || [[ $fsx_type = "SCRATCH_1" ]]; then
  fsx_cost_gb_month=$(aws pricing get-products \
                      --region ${$cfn_region} \
                      --service-code AmazonFSx \
                      --filters 'Type=TERM_MATCH,Field=location,Value='"${aws_region_long_name}" \
                      'Type=TERM_MATCH,Field=fileSystemType,Value=Lustre' \
                      'Type=TERM_MATCH,Field=throughputCapacity,Value=N/A' \
                      --output text \
                      --query 'PriceList' \
                      | jq -r '.terms.OnDemand | to_entries[] | .value.priceDimensions | to_entries[] | .value.pricePerUnit.USD')

elif [ $fsx_type = "PERSISTENT_1" ]; then
  fsx_cost_gb_month=$(aws pricing get-products \
                      --region ${$cfn_region} \
                      --service-code AmazonFSx \
                      --filters 'Type=TERM_MATCH,Field=location,Value='"${aws_region_long_name}" \
                      'Type=TERM_MATCH,Field=fileSystemType,Value=Lustre' \
                      'Type=TERM_MATCH,Field=throughputCapacity,Value='$fsx_throughput \
                      --output text \
                      --query 'PriceList' \
                      | jq -r '.terms.OnDemand | to_entries[] | .value.priceDimensions | to_entries[] | .value.pricePerUnit.USD')

else
  fsx_cost_gb_month=0
fi

fsx=$(echo "scale=2; $fsx_cost_gb_month * $fsx_size_gb / 720" | bc)
echo "fsx_cost $fsx" | curl --data-binary @- http://127.0.0.1:9091/metrics/job/cost


#parametrize:
ebs_volume_total_cost=0
ebs_volume_ids=$(aws ec2 describe-instances     --instance-ids $masterInstanceId \
              | jq -r '.Reservations | to_entries[].value | .Instances | to_entries[].value | .BlockDeviceMappings | to_entries[].value | .Ebs.VolumeId')

for ebs_volume_id in $ebs_volume_ids
do
  ebs_volume_type=$(aws ec2 describe-volumes --volume-ids $ebs_volume_id | jq -r '.Volumes | to_entries[].value.VolumeType')
  #ebs_volume_iops=$(aws ec2 describe-volumes --volume-ids $ebs_volume_id | jq -r '.Volumes | to_entries[].value.Iops')
  ebs_volume_size=$(aws ec2 describe-volumes --volume-ids $ebs_volume_id | jq -r '.Volumes | to_entries[].value.Size')
  
  ebs_cost_gb_month=$(aws --region ${$cfn_region} pricing get-products \
    --service-code AmazonEC2 \
    --query 'PriceList' \
    --output text \
    --filters 'Type=TERM_MATCH,Field=location,Value='"${aws_region_long_name}" \
            'Type=TERM_MATCH,Field=productFamily,Value=Storage' \
            'Type=TERM_MATCH,Field=volumeApiName,Value='$ebs_volume_type \
    | jq -r '.terms.OnDemand | to_entries[] | .value.priceDimensions | to_entries[] | .value.pricePerUnit.USD')

  ebs_volume_cost=$(echo "scale=2; $ebs_cost_gb_month * $ebs_volume_size / 720" | bc)
  ebs_volume_total_cost=$(echo "scale=2; $ebs_volume_total_cost + $ebs_volume_cost" | bc)
done

echo "ebs_master_cost $ebs_volume_total_cost" | curl --data-binary @- http://127.0.0.1:9091/metrics/job/cost