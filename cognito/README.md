# Cognito SSO for Grafana (optional)

By default, Grafana uses a local `admin` user with a per-cluster random
password in SSM. If you want **Cognito-based SSO** instead (so team
members log in with corporate credentials), follow these steps.

## How it works

Grafana sends users to your user pool's sign-in pages (managed login or
the classic hosted UI). After sign-in, Cognito sends them back to
Grafana's callback URL, which Grafana builds from its **root URL**:
`<root URL>login/generic_oauth`. Cognito accepts only callback URLs that
are registered on the app client, so three things must match:

- the URL that users open Grafana at;
- the root URL, `root_url` in the Cognito parameter (step 3);
- the host you give `setup-grafana-client.sh` (step 2).

A node has one root URL, so Cognito sign-in works at that one URL. The
local `admin` login works at any URL.

| Access path | Users open | `<grafana-host>` | `root_url` |
|---|---|---|---|
| SSM port forwarding, as in the README (local port 8443) | `https://localhost:8443/grafana/` | `localhost:8443` | not needed: this is the default |
| ALB with an ACM certificate ([docs/public-access.md](../docs/public-access.md)) | `https://grafana.example.com/grafana/` | `grafana.example.com` | `https://grafana.example.com/grafana/` |

With another local port for SSM port forwarding, for example 9443, use
`localhost:9443` and `https://localhost:9443/grafana/`.

## Prerequisites

- An existing Cognito user pool. If it isn't in the cluster's region, set
  `region` in step 3.
- A domain on the user pool, for its sign-in pages: a prefix domain
  (`<prefix>.auth.<region>.amazoncognito.com`) or a custom domain (such as
  `auth.example.com`). Either branding version works, managed login or the
  classic hosted UI. To create a prefix domain:
  ```bash
  aws cognito-idp create-user-pool-domain --region <region> \
      --user-pool-id <pool-id> \
      --domain <some-unique-prefix>
  ```

## Setup steps

### 1. Choose the URL that users open Grafana at

Pick a row of the table above. It gives `<grafana-host>` for step 2 and
`root_url` for step 3.

### 2. Create a Grafana-specific app client in the pool

```bash
./cognito/setup-grafana-client.sh <pool-id> <grafana-host> [region]
```

The script creates an app client named
`aws-parallelcluster-monitoring-grafana` with:

- the callback URL `https://<grafana-host>/grafana/login/generic_oauth`;
- the sign-out URL `https://<grafana-host>/grafana/`;
- the authorization code grant and the `openid`, `email` and `profile`
  scopes.

It registers `https://` URLs only, because Cognito accepts `http://`
callback URLs only for localhost and nginx serves Grafana over HTTPS only.
When the pool's domain uses managed login, it also gives the client a
branding style with Cognito's defaults: Cognito serves managed login pages
only to app clients that have one, and gives none to clients created
through the API. The script first checks that the pool has a domain. When a
call fails, it prints Cognito's error and exits non-zero, and if the style
can't be created, it deletes the client it just created.

Example, for SSM port forwarding:
```bash
eval "$(./cognito/setup-grafana-client.sh us-east-1_ABC123 localhost:8443 us-east-1)"
```

The script prints `KEY=VALUE` lines on stdout, and `eval` keeps them as
shell variables for step 3. Without `eval`, the output looks like this:
```
COGNITO_CLIENT_ID=xxxxxxxxxxxxxxxx
COGNITO_CLIENT_SECRET=yyyyyyyyyyyyyyyyyyyy
COGNITO_DOMAIN=my-pool-auth.auth.us-east-1.amazoncognito.com
COGNITO_REGION=us-east-1
COGNITO_POOL_ID=us-east-1_ABC123
GRAFANA_ROOT_URL=https://localhost:8443/grafana/
```

### 3. Put the Cognito config in SSM Parameter Store

The installer looks for a SecureString parameter named:

- ParallelCluster: `/parallelcluster/<cluster-name>/grafana/cognito`
- AWS PCS: `/pcs/<cluster-id>/grafana/cognito`, with the cluster ID, such
  as `pcs_abc123def4`, not the cluster name

Its value is a JSON object:

| Key | Required | Value |
|---|---|---|
| `client_id` | yes | `COGNITO_CLIENT_ID` from step 2 |
| `client_secret` | yes | `COGNITO_CLIENT_SECRET` from step 2 |
| `domain` | yes | The user pool domain: the full host name, as `COGNITO_DOMAIN` prints it (`my-pool-auth.auth.us-east-1.amazoncognito.com`, or a custom domain such as `auth.example.com`), or only the prefix of a prefix domain (`my-pool-auth`). A value with a dot is used as the host name. |
| `region` | no | The user pool's region, which the installer needs only for a prefix. Default: the cluster's region. |
| `root_url` | no | The URL that users open Grafana at, ending in `/grafana/`, as `GRAFANA_ROOT_URL` prints it. Default: `https://localhost:8443/grafana/`. |
| `allowed_domains` | no | Email domains, separated by spaces or commas. Grafana then lets in only users whose email is in one of them. Default: any user the pool accepts. |
| `user_pool_id` | no | For your records. The installer doesn't use it. |

In the shell where you ran step 2, create the parameter for a
ParallelCluster cluster named `my-cluster`:

```bash
aws ssm put-parameter --region us-east-1 --type SecureString \
    --name "/parallelcluster/my-cluster/grafana/cognito" \
    --value "$(jq -n \
        --arg id "${COGNITO_CLIENT_ID}" --arg secret "${COGNITO_CLIENT_SECRET}" \
        --arg domain "${COGNITO_DOMAIN}" --arg root "${GRAFANA_ROOT_URL}" \
        --arg pool "${COGNITO_POOL_ID}" \
        '{client_id: $id, client_secret: $secret, domain: $domain,
          root_url: $root, user_pool_id: $pool}')"
```

On AWS PCS, use `--name "/pcs/<cluster-id>/grafana/cognito"`. To limit
sign-in to some email domains, add `allowed_domains: "example.com"` to the
object. To change the parameter later, add `--overwrite`.

The head or login node's role reads the parameter with `ssm:GetParameter`
and `kms:Decrypt` through SSM. The IAM permissions in the README and in
[iam/README.md](../iam/README.md) already grant both.

### 4. Deploy the cluster, or re-run the installer on an existing cluster

On cluster creation, the installer reads the parameter and configures
Grafana. On an existing cluster, re-run the installer on the
ParallelCluster head node or the PCS login node, through SSM Session
Manager:

```bash
sudo bash /opt/aws-parallelcluster-monitoring/installer/install.sh
```

It prints `Cognito env file written to /run/grafana-secrets/cognito.env`,
then the root URL and the Cognito domain it uses. It restarts or recreates
Grafana when its settings change. Re-run it after every change to the
parameter.

The settings live on tmpfs, which a reboot empties. At every boot,
`grafana-cognito-refresh.service` writes them again from the parameter
and restarts Grafana, which Docker starts before the client secret is
back. To check it after a reboot:

```bash
systemctl status grafana-cognito-refresh.service
journalctl -u grafana-cognito-refresh.service -b
```

### 5. Log in

Open the root URL, for example `https://localhost:8443/grafana/` through
SSM port forwarding. The login page shows a **Sign in with Cognito**
button. Click it, sign in on Cognito's page, and you're in. Grafana
creates your account at your first sign-in, with its default role for new
users, Viewer.

Sign-out goes through Cognito's sign-out endpoint, which ends the Cognito
session and returns you to the root URL. That happens for every Grafana
user, including the local admin.

## Fallback: local admin still works

The local `admin` user remains enabled. If Cognito is misconfigured
or unreachable, you can still log in with `admin` + the SSM-managed
password. Retrieve it with:

```bash
aws ssm get-parameter --region <region> \
    --name /<platform>/<cluster>/grafana/admin-password \
    --with-decryption --query Parameter.Value --output text
```

`/<platform>/<cluster>` is `/parallelcluster/<cluster-name>` on
ParallelCluster and `/pcs/<cluster-id>` on AWS PCS.

## Troubleshooting

- **The installer prints an `ERROR` about the parameter**, for example
  `AccessDeniedException` or `the value is not a JSON object`. Grafana
  then offers only the local admin login. Fix the parameter or the node
  role's permissions, and re-run the installer. A missing parameter is not
  an error: the installer prints
  `No Cognito config in <parameter>; using local admin login only`.
- **Cognito shows an error page instead of the sign-in form.** The root
  URL doesn't match a callback URL of the app client. Compare
  `GF_SERVER_ROOT_URL` in `/run/grafana-secrets/cognito.env` with:
  ```bash
  aws cognito-idp describe-user-pool-client --region <region> \
      --user-pool-id <pool-id> --client-id <client-id> \
      --query 'UserPoolClient.[CallbackURLs, LogoutURLs]'
  ```
  The callback URL must be the root URL followed by `login/generic_oauth`.
  On a managed login domain, the app client also needs a branding style:
  ```bash
  aws cognito-idp create-managed-login-branding --region <region> \
      --user-pool-id <pool-id> --client-id <client-id> --use-cognito-provided-values
  ```
- **Grafana rejects the sign-in after Cognito accepted it.** You started at
  a URL other than the root URL. Grafana keeps the sign-in's state in a
  cookie for the URL you started at, and Cognito returns you to the root
  URL. Open Grafana at the root URL.
- **Sign-in stopped working after a reboot.** Check
  `grafana-cognito-refresh.service` as in step 4. To retry it:
  `sudo systemctl start grafana-cognito-refresh.service`.
- **Sign-out ends on a Cognito error page.** The root URL isn't a sign-out
  URL of the app client. App clients created by the setup script of
  earlier releases have `https://<host>/grafana`, without the final `/`.
  Create a new app client with this release's script, store its ID and
  secret in the parameter, re-run the installer, then delete the old app
  client.

## Security notes

- The Cognito client secret is stored in SSM Parameter Store as a
  SecureString (KMS-encrypted at rest).
- At runtime, the installer, and `grafana-cognito-refresh.service` at
  every boot, write it to a file on tmpfs,
  `/run/grafana-secrets/cognito-client-secret`, with mode 0640 and owner
  `472:472`, the user that Grafana runs as in its container. The directory
  is mode 0750 with the same owner. The Grafana container mounts it
  read-only.
- The container's configuration references the secret through
  `GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET__FILE`, so `docker inspect` and
  `docker exec grafana env` show only the file's path. The Grafana
  image's entrypoint reads the file and starts Grafana with the secret in
  `GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET`, so the Grafana server process
  holds it in its environment. Grafana also passes that environment to
  its CloudWatch plugin process (`GF_PLUGINS_FORWARD_HOST_ENV_VARS=cloudwatch`
  in `compose/head.yml`), which therefore holds the client secret and the
  Grafana admin password (`GF_SECURITY_ADMIN_PASSWORD`) too. Both
  processes run as Grafana's user (472) in the Grafana container, which
  can read the secret files anyway.
- The main Cognito env file (`/run/grafana-secrets/cognito.env`) contains
  the non-secret values: the root URL, the client ID, the Cognito
  endpoints and the allowed domains.

## Disabling Cognito

Delete the SSM parameter, then re-run the installer on the head or login
node:

```bash
aws ssm delete-parameter --region <region> \
    --name /<platform>/<cluster>/grafana/cognito

# On the head or login node:
sudo bash /opt/aws-parallelcluster-monitoring/installer/install.sh
```

The installer finds no parameter, writes a `cognito.env` that holds only
the default root URL, deletes the secret file and recreates Grafana
without the Cognito button.
