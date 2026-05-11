# Public access to Grafana (optional)

By default, Grafana is served over HTTPS with a **self-signed certificate**
on the HeadNode. Access is via SSM Session Manager port-forward:

```bash
aws ssm start-session --target <head-node-id> --region <region> \
    --document-name AWS-StartPortForwardingSession \
    --parameters 'portNumber=["443"],localPortNumber=["8443"]'
# Then browse: https://localhost:8443/grafana/
```

This is the recommended approach for most users — no public exposure,
no domain required, works from anywhere with AWS CLI + SSM plugin.

## Self-signed certificate details

The installer generates a 4096-bit RSA cert valid for 10 years with
these Subject Alternative Names (SANs):

- `DNS.1 = localhost` — for SSM port-forward access
- `DNS.2 = <private-hostname>` — for direct VPC access (e.g. `ip-10-3-7-74`)
- `IP.1 = 127.0.0.1` — loopback
- `IP.2 = <private-ip>` — head node's VPC IP (e.g. `10.3.7.74`)

Your browser will still show a "not trusted" warning (self-signed), but
the connection IS encrypted. Click through the warning or add an exception.

---

## Option: ALB + ACM for trusted HTTPS (requires a domain)

If you want a trusted certificate (green lock, no browser warnings) and
public access without SSM, you can put an Application Load Balancer in
front of the HeadNode with an ACM certificate.

### Prerequisites

1. A **public hosted zone** in Route53 (e.g. `example.com`)
2. An **ACM certificate** in the same region as the cluster, covering
   your desired subdomain (e.g. `grafana.example.com`)
3. The HeadNode's **security group** must allow inbound from the ALB

### Steps

#### 1. Request an ACM certificate

```bash
aws acm request-certificate \
    --domain-name grafana.example.com \
    --validation-method DNS \
    --region us-east-2
```

Follow the DNS validation instructions (add the CNAME record to Route53).
Wait for status `ISSUED`.

#### 2. Create the ALB

```bash
# Get the VPC and public subnets from your cluster
VPC_ID=$(aws ec2 describe-instances --instance-ids <head-node-id> \
    --query 'Reservations[0].Instances[0].VpcId' --output text)

# Create ALB (internet-facing, in public subnets)
ALB_ARN=$(aws elbv2 create-load-balancer \
    --name pcluster-grafana \
    --subnets <public-subnet-1> <public-subnet-2> \
    --security-groups <alb-sg-id> \
    --scheme internet-facing \
    --type application \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)
```

#### 3. Create target group pointing at HeadNode:443

```bash
TG_ARN=$(aws elbv2 create-target-group \
    --name pcluster-grafana-tg \
    --protocol HTTPS \
    --port 443 \
    --vpc-id $VPC_ID \
    --target-type instance \
    --health-check-path /grafana/api/health \
    --health-check-protocol HTTPS \
    --query 'TargetGroups[0].TargetGroupArn' --output text)

aws elbv2 register-targets --target-group-arn $TG_ARN \
    --targets Id=<head-node-id>
```

#### 4. Create HTTPS listener with ACM cert

```bash
aws elbv2 create-listener \
    --load-balancer-arn $ALB_ARN \
    --protocol HTTPS \
    --port 443 \
    --certificates CertificateArn=<acm-cert-arn> \
    --default-actions Type=forward,TargetGroupArn=$TG_ARN
```

#### 5. Create Route53 alias record

```bash
ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns $ALB_ARN \
    --query 'LoadBalancers[0].DNSName' --output text)
ALB_ZONE=$(aws elbv2 describe-load-balancers --load-balancer-arns $ALB_ARN \
    --query 'LoadBalancers[0].CanonicalHostedZoneId' --output text)

aws route53 change-resource-record-sets --hosted-zone-id <your-zone-id> \
    --change-batch '{
      "Changes": [{
        "Action": "CREATE",
        "ResourceRecordSet": {
          "Name": "grafana.example.com",
          "Type": "A",
          "AliasTarget": {
            "HostedZoneId": "'$ALB_ZONE'",
            "DNSName": "'$ALB_DNS'",
            "EvaluateTargetHealth": true
          }
        }
      }]
    }'
```

#### 6. Update Grafana's root URL

On the HeadNode, update the compose environment:

```bash
# In compose/head.yml, change:
#   GF_SERVER_ROOT_URL=http://%(domain)s/grafana/
# To:
#   GF_SERVER_ROOT_URL=https://grafana.example.com/grafana/

sudo docker restart grafana
```

Or set it via SSM parameter for the next install run.

#### 7. Browse

`https://grafana.example.com/grafana/` — trusted cert, no warnings.

### Security considerations

- The ALB exposes Grafana to the internet. Make sure you have either:
  - Cognito SSO enabled (Phase 2b.2), OR
  - A strong admin password (Phase 2a.1 generates a random 32-char one), OR
  - ALB security group restricted to your corporate CIDR
- Consider enabling WAF on the ALB for additional protection
- The HeadNode's security group should only allow inbound 443 from the
  ALB's security group, not from `0.0.0.0/0`

### Cleanup

```bash
aws elbv2 delete-load-balancer --load-balancer-arn $ALB_ARN
aws elbv2 delete-target-group --target-group-arn $TG_ARN
aws route53 change-resource-record-sets ... # DELETE the alias record
```

The ALB costs ~$0.02/hr + data transfer. Delete it when not needed.
