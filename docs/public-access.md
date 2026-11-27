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

Prometheus (`/prometheus/`) and the Pushgateway (`/pushgateway/`) are
available only on this path, for example at
`https://localhost:8443/prometheus/`. nginx serves them to local clients
only, and the SSM agent connects from 127.0.0.1. Other clients get 403.

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

#### 6. With Cognito SSO, set Grafana's root URL

Without Cognito SSO, skip this step: the local admin login works at any
URL.

Grafana builds its Cognito callback URL from its root URL, as
`<root URL>login/generic_oauth`, and Cognito accepts only callback URLs
registered on the app client. The root URL is
`https://localhost:8443/grafana/` by default, the SSM port forwarding URL.
For Cognito sign-in through the ALB, the root URL must be the URL that
users open: `https://`, then the public host name of steps 1 and 5, then
`/grafana/`. With the certificate and the alias record for
`grafana.example.com`, it is `https://grafana.example.com/grafana/`, with
no port, because the listener is on 443. Use that host name in every step
below (details in [cognito/README.md](../cognito/README.md)):

1. Create an app client for the public host name:
   ```bash
   ./cognito/setup-grafana-client.sh <pool-id> grafana.example.com <region>
   ```
   It prints the root URL to store, `GRAFANA_ROOT_URL=https://grafana.example.com/grafana/`.
2. Store its values in the Cognito parameter,
   `/parallelcluster/<cluster-name>/grafana/cognito` or
   `/pcs/<cluster-id>/grafana/cognito`, with
   `"root_url": "https://grafana.example.com/grafana/"`.
3. Re-run the installer on the head or login node:
   ```bash
   sudo bash /opt/aws-parallelcluster-monitoring/installer/install.sh
   ```

Cognito sign-in then works at `https://grafana.example.com/grafana/`
only. Users who open Grafana at another URL, such as the ALB's own
`*.elb.amazonaws.com` name, are sent back to the root URL after they sign
in at Cognito, and Grafana rejects that sign-in. Over SSM port
forwarding, use the local admin login.

#### 7. Browse

`https://grafana.example.com/grafana/` — trusted cert, no warnings.

### Security considerations

- The ALB exposes Grafana to the internet. Make sure you have either:
  - Cognito SSO enabled ([cognito/README.md](../cognito/README.md), with
    step 6 above), OR
  - A strong admin password (Phase 2a.1 generates a random 32-char one), OR
  - ALB security group restricted to your corporate CIDR
- Consider enabling WAF on the ALB for additional protection
- The HeadNode's security group should only allow inbound 443 from the
  ALB's security group, not from `0.0.0.0/0`
- ALB clients get the landing page and `/grafana/` only. `/prometheus/`
  and `/pushgateway/` return 403, because nginx serves them to local
  clients only. Reach them through SSM port forwarding (top of this page).

### Cleanup

```bash
aws elbv2 delete-load-balancer --load-balancer-arn $ALB_ARN
aws elbv2 delete-target-group --target-group-arn $TG_ARN
aws route53 change-resource-record-sets ... # DELETE the alias record
```

The ALB costs ~$0.02/hr + data transfer. Delete it when not needed.
