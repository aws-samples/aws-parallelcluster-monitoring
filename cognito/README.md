# Cognito SSO for Grafana (optional)

By default, Grafana uses a local `admin` user with a per-cluster random
password in SSM. If you want **Cognito-based SSO** instead (so team
members log in with corporate credentials), follow these steps.

## Prerequisites

- An existing AWS Cognito User Pool, in the same region as the cluster.
- The User Pool must have a **hosted UI domain** configured. If it
  doesn't, create one first:
  ```bash
  aws cognito-idp create-user-pool-domain \
      --user-pool-id <pool-id> \
      --domain <some-unique-subdomain-name>
  ```

## Setup steps

### 1. Create a Grafana-specific app client in the pool

```bash
./cognito/setup-grafana-client.sh <pool-id> <grafana-host> [region]
```

`<grafana-host>` is the hostname or IP by which users will reach
Grafana in their browser. For SSM port-forward testing, use
`localhost:8443`. For a public-facing deployment, use the DNS name or
public IP of the HeadNode (or your ALB, if you added one).

Example:
```bash
./cognito/setup-grafana-client.sh us-east-2_ABC123 head.example.com us-east-2
```

Output:
```
COGNITO_CLIENT_ID=xxxxxxxxxxxxxxxx
COGNITO_CLIENT_SECRET=yyyyyyyyyyyyyyyyyyyy
COGNITO_DOMAIN=my-pool-auth
COGNITO_REGION=us-east-2
COGNITO_POOL_ID=us-east-2_ABC123
```

### 2. Put the Cognito config in SSM Parameter Store

The installer looks for a SecureString parameter at
`/parallelcluster/<cluster-name>/grafana/cognito`. Create it with a
JSON blob:

```bash
CLUSTER=my-cluster
aws ssm put-parameter \
    --region us-east-2 \
    --name "/parallelcluster/${CLUSTER}/grafana/cognito" \
    --type SecureString \
    --value '{
        "user_pool_id":    "us-east-2_ABC123",
        "client_id":       "xxxxxxxxxxxxxxxx",
        "client_secret":   "yyyyyyyyyyyyyyyy",
        "domain":          "my-pool-auth",
        "region":          "us-east-2",
        "allowed_domains": "example.com"
    }'
```

`allowed_domains` is optional — if set, Grafana only lets users with
emails in those domains log in. Leave empty/unset to allow any user
the pool accepts.

### 3. Deploy the cluster (or restart the stack on an existing cluster)

On cluster creation, the installer reads the SSM parameter and
configures Grafana automatically. On an existing cluster, you can
re-run the installer manually:

```bash
# On the HeadNode (via SSH or SSM Session Manager)
sudo bash /opt/aws-parallelcluster-monitoring/installer/install.sh
sudo docker restart grafana
```

### 4. Log in

Visit `https://<grafana-host>/grafana/` — you'll see a new "Sign in
with Cognito" button. Click it, authenticate via Cognito's hosted UI,
and you're in.

## Fallback: local admin still works

The local `admin` user remains enabled. If Cognito is misconfigured
or unreachable, you can still log in with `admin` + the SSM-managed
password. Retrieve it with:

```bash
aws ssm get-parameter --region <region> \
    --name /parallelcluster/<cluster>/grafana/admin-password \
    --with-decryption --query Parameter.Value --output text
```

## Security notes

- The Cognito client secret is stored in SSM Parameter Store as a
  SecureString (KMS-encrypted at rest).
- At runtime, the installer materializes it to a file mode 0640 owned
  by `root:nobody` on tmpfs (`/run/grafana-secrets/cognito-client-secret`),
  readable only by the Grafana container.
- The secret is referenced by Grafana via
  `GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET__FILE`, so it does not appear
  in `docker inspect` or environment variable dumps.
- The main Cognito env file (`cognito.env`) contains the non-secret
  values (client ID, domain, endpoints, allowed domains). These are
  not sensitive.

## Disabling Cognito

Delete the SSM parameter, then restart Grafana:

```bash
aws ssm delete-parameter --region <region> \
    --name /parallelcluster/<cluster>/grafana/cognito

# On the HeadNode:
sudo bash /opt/aws-parallelcluster-monitoring/installer/install.sh
sudo docker restart grafana
```

Next install run will detect the missing parameter and write an empty
`cognito.env`, effectively disabling OAuth.
