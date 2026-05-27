# Self-hosting zrok v2 + LLM/MCP gateways on OCI

End-to-end install record for an OCI ARM64 VM that runs:

- **zrok v2 self-hosted** controller + frontend, tied to an existing
  OpenZiti network (no Ziti install on this host)
- **openziti/llm-gateway** as a private zrok share
- **openziti/mcp-gateway** as a private zrok share

This is the doc I wish existed before the rollout. It bakes in every
mistake the official docs don't cover. If you reproduce this on a
different environment, search-and-replace the placeholders in
`<angle brackets>` and read the per-phase notes — several steps fail
silently if you skip a non-obvious detail.

> Verified on: Ubuntu 24.04 (noble), VM.Standard.A1.Flex (aarch64),
> 11 GB RAM, 47 GB disk, ap-singapore-1.
> zrok v2.0.4, OpenZiti CLI v2.0.0, OpenZiti controller v1.6.14,
> certbot 2.9.0, Go 1.26.3, PostgreSQL 16.14, RabbitMQ 3.12.1.

---

## Conventions in this document

- `<ZONE>` — your apex domain (e.g. `example.com`)
- `<ZROK_FQDN>` — host where the zrok frontend serves TLS (e.g. `zrok.example.com`)
- `<ZITI_API>` — your OpenZiti controller URL (e.g. `https://zerotrust.example.com:1280`)
- `<ZITI_ADMIN_PASSWORD>` — Ziti `admin` user password (never write to git)
- `<CF_API_TOKEN>` — Cloudflare API token scoped to `<ZONE>` with `Zone:Read + DNS:Edit`
- `<PUBLIC_IP>` — the VM's current public IPv4
- `<COMP_OCID>`, `<INSTANCE_OCID>`, `<SUBNET_OCID>`, `<SEC_LIST_OCID>`,
  `<BOOT_VOL_OCID>` — OCI resource IDs
- `<EMAIL>` — your email for Let's Encrypt registration

Wherever you see a base64/uuid/random value, generate fresh ones for
your environment. Never reuse the ones in this document — they were
generated for the original rollout and have been rotated since.

---

## Target topology

```
Internet ─ OCI sec-list:443 ─ host iptables:443 ─ <PUBLIC_IP>
                                                       │
                                                  zrok2-frontend ✱:443
                                                       │
                                  loopback only:
                                       127.0.0.1:18080  zrok2-controller
                                       127.0.0.1:5432   postgres
                                       127.0.0.1:5672   rabbitmq AMQP
                                       127.0.0.1:25672  rabbitmq dist
                                       127.0.0.1:4369   epmd
                                                       │
                                  no local listener:
                                       llm-gateway  → zrok share "llm-gateway"
                                       mcp-gateway  → zrok share "mcp-gateway"
                                                       │
                                              outbound → <ZITI_API>
                                                          and edge-router :3022
```

---

## Prerequisites

1. **OCI tenancy** with a running ARM64 (or x86) compute instance, public IP
   attached, and `cloud-init` SSH access. The recipe assumes Ubuntu 24.04.
2. **DNS zone** under your control. Cloudflare is what these steps use, but
   the only Cloudflare-specific touch is the certbot DNS-01 plugin.
3. **Existing OpenZiti controller + edge router**, both reachable from the
   VM. The controller must run a recent enough version that supports
   `zrok.proxy.v1` config types (any 1.5+ is fine).
4. **Admin password** for the Ziti controller (used once at bootstrap; only
   stored in `/etc/zrok2/ctrl.yml` with `chmod 640`).
5. **Cloudflare API token** scoped Zone:Read + DNS:Edit on a single zone
   only. Strongly recommend tying it to the VM's public IP.

### DNS records (create *before* the rollout)

| Record | Type | Value | Proxy |
|---|---|---|---|
| `<ZROK_FQDN>` | A | `<PUBLIC_IP>` | DNS only (grey cloud) |
| `*.<ZROK_FQDN>` | A | `<PUBLIC_IP>` | DNS only |

Both must be `DNS only` — Cloudflare's proxy would terminate TLS, breaking
zrok's per-share routing.

---

## Phase 0 — VM and OCI CLI bootstrap

### 0.1 Install the OCI CLI on the VM

```bash
bash -c "$(curl -sSL https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)" -- --accept-all-defaults
```

### 0.2 Configure instance-principal auth

Add the VM to an OCI Dynamic Group, then grant the group the policies you
need. The Default identity domain requires qualified-group syntax in
policy statements:

```
Allow dynamic-group 'Default'/'<dynamic-group-name>' to manage virtual-network-family in tenancy
Allow dynamic-group 'Default'/'<dynamic-group-name>' to manage volume-family in tenancy
Allow dynamic-group 'Default'/'<dynamic-group-name>' to manage instance-family in tenancy
Allow dynamic-group 'Default'/'<dynamic-group-name>' to manage object-family in tenancy
```

> Without the `'Default'/'<name>'` prefix, OCI returns "Statement cannot be
> parsed." This catches almost everyone the first time. The fourth statement
> (object-family) is only needed if you want to back snapshots up to OCI
> Object Storage in Phase 2.7.

Export the env var so every `oci` invocation uses instance principals:

```bash
echo 'export OCI_CLI_AUTH=instance_principal' | sudo tee /etc/profile.d/oci-instance-principal.sh
source /etc/profile.d/oci-instance-principal.sh
oci iam region list   # smoke test — should print regions, not 401
```

### 0.3 Install Ziti CLI (used only for inventory + bootstrap)

The Ziti CLI is needed to inventory the production controller and to create
the dynamic-proxy-controller objects. It does NOT run as a daemon here.

```bash
ZITI_VERSION=v2.0.0
curl -sSL "https://github.com/openziti/ziti/releases/download/${ZITI_VERSION}/ziti-linux-arm64-${ZITI_VERSION#v}.tar.gz" \
  | sudo tar -xz -C /usr/local/bin ziti
ziti --version
ziti edge login <ZITI_API_HOST>:1280 -u admin -p '<ZITI_ADMIN_PASSWORD>'
```

---

## Phase 0.5 — Public IP strategy (read before you act)

**Original intent**: promote the ephemeral public IP to reserved so it
survives stop/start/boot-volume swap.

**OCI API limitation we hit**: the CLI/API path for "convert ephemeral →
reserved" is not atomic. The only sequence is `delete ephemeral` →
`create reserved`, but OCI auto-assigns a *new* ephemeral to the VNIC the
instant the old one is deleted — faster than any follow-up create call.
Result: the original IP is lost, a different ephemeral takes its place,
and the reserved create errors because the VNIC already has a public IP.

The OCI **Console** has an atomic "Reserve" button that preserves the IP,
but no equivalent in the API.

**Approach taken in this rollout: keep ephemeral.** Justifications:

- Ephemeral IPs survive instance stop/start, reboot, and boot-volume
  detach/attach (the entire Layer-B rollback path)
- They only get released on full instance termination
- Layer-B rollback never terminates the instance

**Required updates if your IP changes** (or if you keep ephemeral and ever
have it churn):

- `/etc/ipsec.conf` `left=`/`leftid=`
- `/etc/ipsec.secrets` PSK key
- Cloudflare DNS A records (`<ZROK_FQDN>` and `*.<ZROK_FQDN>`)
- Any IPsec peer that has you in its `right=` config

If you need termination-resilient IP later, do the conversion in the OCI
Console (atomic) — not via API.

---

## Phase 0.6 — Baseline boot-volume backup

Take a FULL snapshot of the boot volume *before* installing anything. This
becomes your Layer-B rollback point (restore the whole filesystem to
"clean slate").

```bash
COMP_OCID="<COMP_OCID>"
INSTANCE_OCID=$(curl -s http://169.254.169.254/opc/v1/instance/ | jq -r .id)
AD=$(curl -s http://169.254.169.254/opc/v1/instance/ | jq -r .availabilityDomain)

BOOT_VOL_OCID=$(oci compute boot-volume-attachment list \
  --compartment-id "$COMP_OCID" --availability-domain "$AD" \
  --instance-id "$INSTANCE_OCID" \
  --query 'data[0]."boot-volume-id"' --raw-output)

oci bv boot-volume-backup create \
  --boot-volume-id "$BOOT_VOL_OCID" \
  --display-name "pre-zrok-rollout-$(date -u +%Y%m%dT%H%M%SZ)" \
  --type FULL \
  --wait-for-state AVAILABLE
```

Record the returned backup OCID — you'll need it for Layer-B restore.

> **Lesson**: don't pass `--wait-interval-seconds` / `--max-wait-seconds`
> to OCI `boot-volume-backup create`. Those flags don't exist on this
> command and cause an immediate parse error that *looks* like a backup
> failure. The backup itself completes regardless — verify with `oci bv
> boot-volume-backup get --boot-volume-backup-id ...`.

---

## Phase 1 — VCN security list

Add a single ingress rule for TCP/443 (the zrok frontend). Nothing else on
this VM needs to be publicly reachable.

```bash
SEC_LIST_OCID="<SEC_LIST_OCID>"   # the subnet's default security list

# Snapshot current rules so you can roll back later
oci network security-list get --security-list-id "$SEC_LIST_OCID" \
  --query 'data."ingress-security-rules"' > phase1-seclist-before.json

# Append the new rule (this CLI replaces the array — pass the full set)
jq '. + [{
  "source": "0.0.0.0/0",
  "source-type": "CIDR_BLOCK",
  "protocol": "6",
  "tcp-options": { "destination-port-range": { "min": 443, "max": 443 } },
  "is-stateless": false,
  "description": "zrok2-frontend HTTPS"
}]' phase1-seclist-before.json > phase1-seclist-after.json

oci network security-list update \
  --security-list-id "$SEC_LIST_OCID" \
  --ingress-security-rules file://phase1-seclist-after.json \
  --force
```

> The OCI security-list update operation is full-array replacement — every
> existing rule must be present in the JSON you submit, or it gets deleted.
> Always snapshot first, then append, then submit.

### Host iptables — Phase 8 will revisit this

Don't touch host iptables here. We discovered in Phase 8 that the default
INPUT chain has a `REJECT ... reject-with icmp-host-prohibited` at the
end, which silently blocks external 443 even after OCI lets it through.
That fix is documented in Phase 8.4 so it's not forgotten.

---

## Phase 2 — External Ziti safety prep

Before touching the production Ziti, capture an inventory so any change
is auditable, and pre-stage a teardown script.

### 2.1 Inventory snapshot (read-only)

```bash
mkdir -p ~/zrok-rollout/preflight && cd ~/zrok-rollout/preflight
for kind in identities services edge-router-policies \
            service-edge-router-policies service-policies \
            configs config-types edge-routers auth-policies; do
  ziti edge list "$kind" 'limit 1000' -j > "${kind}.before.json"
done
date -u +%FT%TZ > snapshot.timestamp
```

> **The `'limit 1000'` filter is mandatory.** The ziti CLI's default page
> size is 10. Without it, a populated controller's snapshot is silently
> truncated and your diff comparisons later will be wrong. We caught this
> in Phase 5.8.

### 2.2 Collision checks

```bash
# 0 means safe to use that name; >0 means inspect and decide.
ziti edge list edge-router-policies 'name="default"' -j | jq '.data | length'
ziti edge list service-edge-router-policies 'name="default"' -j | jq '.data | length'
ziti edge list services 'name="dynamicProxyController" or name="zrok-dynamic-proxy-controller"' -j | jq '.data | length'
ziti edge list identities 'name="public" or name="zrok-public-frontend"' -j | jq '.data | length'

# Anything that would steal the @dynamicFrontends or @all role attribute?
ziti edge list identities -j | jq -r '.data[].roleAttributes // [] | .[]' | sort -u
```

### 2.3 Naming + tagging convention

| zrok docs concept | Name in this rollout | Tag |
|---|---|---|
| controller dynamic-proxy service | `zrok-dynamic-proxy-controller` | `owner=zrok-self-hosted-sg` |
| controller bind identity | `zrok-dynamic-proxy-controller` | `owner=zrok-self-hosted-sg` |
| public frontend identity | `public` (auto-created by `zrok2 admin bootstrap`) | `owner=zrok-self-hosted-sg` (applied after bootstrap) |
| service-edge-router-policy | `zrok-dpc-serp` | `owner=zrok-self-hosted-sg` |
| service-policy Bind | `zrok-dpc-bind` | `owner=zrok-self-hosted-sg` |
| service-policy Dial | `zrok-dpc-dial` | `owner=zrok-self-hosted-sg` |

When creating Ziti objects with `ziti edge`, use `--tags-json` (not
`--tags`), because `--tags` is CSV-style and chokes on JSON. Both objects
the bootstrap creates can have tags added with `ziti edge update ... --tags-json`.

### 2.4 Pre-staged teardown script

The script `scripts/teardown-ziti.sh` (in this repo) deletes every Ziti
object tagged `owner=zrok-self-hosted-sg` or named `zrok-*`. It prints a
dry-run inventory first and requires typing `I-UNDERSTAND` to proceed.

### 2.5 Copy snapshots off-VM

Layer-B rollback restores the boot volume, which wipes
`~/zrok-rollout/preflight/`. Copy to OCI Object Storage:

```bash
NS=$(oci os ns get --query data --raw-output)
oci os bucket create --compartment-id "$COMP_OCID" --namespace "$NS" \
  --name zrok-rollout-snapshots --public-access-type NoPublicAccess \
  --storage-tier Standard --versioning Enabled

cd ~ && tar -czf /tmp/preflight.tar.gz -C ~/zrok-rollout .
oci os object put --namespace "$NS" --bucket-name zrok-rollout-snapshots \
  --file /tmp/preflight.tar.gz \
  --name "zrok-rollout-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" \
  --content-type application/gzip --force
rm /tmp/preflight.tar.gz
```

---

## Phase 3 — Install zrok2 dependencies

PostgreSQL for the controller's state store. RabbitMQ for the
controller→frontend dynamic-proxy push channel. **No InfluxDB** — metrics
are skipped in this rollout.

### 3.1 Install

```bash
sudo apt update
sudo apt install -y rabbitmq-server postgresql
```

### 3.2 Lock RabbitMQ to loopback

```bash
sudo tee -a /etc/rabbitmq/rabbitmq-env.conf <<'EOF'

# Bind RabbitMQ to loopback only
NODE_IP_ADDRESS=127.0.0.1
DIST_PORT=25672
SERVER_ADDITIONAL_ERL_ARGS="-kernel inet_dist_use_interface {127,0,0,1}"

# Pin nodename so EPMD resolves correctly when bound to 127.0.0.1
NODENAME=rabbit@localhost
USE_LONGNAME=false
EOF

# Also lock down EPMD via systemd drop-in
sudo mkdir -p /etc/systemd/system/epmd.socket.d
sudo tee /etc/systemd/system/epmd.socket.d/override.conf <<'EOF'
[Socket]
ListenStream=
ListenStream=127.0.0.1:4369
EOF

# Remove the empty mnesia dir from the short-lived rabbit@<hostname>
sudo rm -rf /var/lib/rabbitmq/mnesia/rabbit@<hostname>*
sudo systemctl daemon-reload
sudo systemctl restart epmd.socket rabbitmq-server
```

> **Trap**: without `NODENAME=rabbit@localhost`, RabbitMQ tries to reach
> EPMD via the host's primary hostname → `/etc/hosts` resolves that to
> `127.0.1.1` → EPMD bound to `127.0.0.1` only → boot fails with
> "epmd error for host ...: address (cannot connect to host/port)".
> Pinning the nodename to `localhost` resolves to `127.0.0.1` reliably.

### 3.3 Verify both bind only to loopback

```bash
sudo ss -tlnp | grep -E ':5672|:5432|:4369|:25672'
# Every line must show 127.0.0.1, never *: or 0.0.0.0:
```

### 3.4 Create the zrok2 Postgres role + database

```bash
PG_PASS="$(openssl rand -base64 24 | tr -d '\n/+=' | head -c 32)"
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
CREATE USER zrok2 WITH PASSWORD '${PG_PASS}';
CREATE DATABASE zrok2 OWNER zrok2;
SQL

# Save PG_PASS securely — you'll embed it in /etc/zrok2/ctrl.yml later.
```

### 3.5 Smoke test

```bash
PGPASSWORD="$PG_PASS" psql -h 127.0.0.1 -U zrok2 -d zrok2 -At \
  -c "SELECT current_user, current_database();"
# Expect: zrok2|zrok2
```

---

## Phase 4 — Wildcard TLS via Cloudflare DNS-01

```bash
sudo apt install -y certbot python3-certbot-dns-cloudflare

sudo install -d -m 700 -o root -g root /root/.secrets
sudo install -m 600 /dev/null /root/.secrets/cloudflare.ini
echo "dns_cloudflare_api_token = <CF_API_TOKEN>" \
  | sudo tee /root/.secrets/cloudflare.ini >/dev/null
sudo chmod 600 /root/.secrets/cloudflare.ini

# Dry-run against staging first — sometimes a race during account
# registration causes a one-shot 'account not found' that succeeds on retry.
sudo certbot certonly --dry-run \
  --dns-cloudflare \
  --dns-cloudflare-credentials /root/.secrets/cloudflare.ini \
  --dns-cloudflare-propagation-seconds 30 \
  -d '<ZROK_FQDN>' \
  -d '*.<ZROK_FQDN>' \
  --email <EMAIL> --agree-tos --non-interactive

# Issue the real cert
sudo certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /root/.secrets/cloudflare.ini \
  --dns-cloudflare-propagation-seconds 30 \
  -d '<ZROK_FQDN>' \
  -d '*.<ZROK_FQDN>' \
  --email <EMAIL> --agree-tos --non-interactive
```

The `certbot.timer` is auto-enabled by the Debian package. `certbot renew
--dry-run` confirms the renewal pipeline works.

You'll wire a `--deploy-hook` for zrok2-frontend reload later in Phase 5
(after the frontend is running) — or skip and let restart happen on
expiry. The cert files live at:

```
/etc/letsencrypt/live/<ZROK_FQDN>/{cert,chain,fullchain,privkey}.pem
```

The privkey is 0600 root:root by default — Phase 5 sets up a `zrok2-tls`
group so non-root services can read it.

---

## Phase 5 — Install zrok2 and bootstrap against external Ziti

### 5.1 Add the openziti apt repo and install packages

```bash
curl -sSLf https://get.openziti.io/tun/package-repos.gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/openziti.gpg
echo "deb [signed-by=/usr/share/keyrings/openziti.gpg] https://packages.openziti.org/zitipax-openziti-deb-stable debian main" \
  | sudo tee /etc/apt/sources.list.d/openziti-release.list
sudo apt update
sudo apt install -y zrok2 zrok2-controller zrok2-frontend
# Do NOT install zrok2-metrics-bridge.
```

> **Trap**: the docs page that says "use `noble main`" is wrong — the
> openziti repo doesn't ship a noble distribution. Use `debian main`. The
> deb files are arch-portable for the same architecture; Debian-named
> Ubuntu installs are normal in OpenZiti land.

### 5.2 Write `/etc/zrok2/ctrl.yml`

```bash
ZROK_ADMIN_TOKEN="$(cat /proc/sys/kernel/random/uuid)"   # save this

sudo tee /etc/zrok2/ctrl.yml >/dev/null <<EOF
v: 4

admin:
  secrets:
    - ${ZROK_ADMIN_TOKEN}

dynamic_proxy_controller:
  identity_path: /var/lib/zrok2-controller/.zrok2/identities/zrok-dynamic-proxy-controller.json
  service_name: zrok-dynamic-proxy-controller
  amqp_publisher:
    url: amqp://guest:guest@127.0.0.1:5672
    exchange_name: dynamicProxy

endpoint:
  host: 127.0.0.1
  port: 18080

# metrics: section intentionally omitted (no InfluxDB)
# bridge:  section intentionally omitted (no metrics-bridge)

store:
  path: "host=127.0.0.1 user=zrok2 password=<PG_PASS> dbname=zrok2 sslmode=disable"
  type: postgres

ziti:
  api_endpoint: <ZITI_API>
  username: admin
  password: <ZITI_ADMIN_PASSWORD>
EOF
sudo chown zrok2-controller:zrok2-controller /etc/zrok2/ctrl.yml
sudo chmod 640 /etc/zrok2/ctrl.yml
```

### 5.3 Bootstrap

This is the first write to the production Ziti. `zrok2 admin bootstrap`
will:

- Apply 41 Postgres migrations
- Create Ziti identity `public` (the public frontend's identity)
- Create Ziti edge-router-policy `public` linking that identity to all routers
- Create Ziti config-type `zrok.proxy.v1`
- Enroll the `public` identity and write its `.json` to
  `/var/lib/zrok2-controller/.zrok2/identities/public.json`

```bash
sudo -u zrok2-controller env HOME=/var/lib/zrok2-controller \
  zrok2 admin bootstrap /etc/zrok2/ctrl.yml
```

> **Trap**: the bootstrap creates identities and ERPs **without** any tag
> by default, and the public identity has **no role attributes** — which
> means the zrok frontend won't be able to dial the dynamic-proxy-controller
> service (it needs `dynamicFrontends`). Both gaps are fixed below.

Tag the bootstrap-created objects and set the role attribute:

```bash
PUBLIC_ZID=$(ziti edge list identities 'name="public"' -j | jq -r '.data[0].id')
PUBLIC_ERP_ID=$(ziti edge list edge-router-policies 'name="public"' -j | jq -r '.data[0].id')

ziti edge update identity "$PUBLIC_ZID" \
  --role-attributes dynamicFrontends \
  --tags-json '{"owner":"zrok-self-hosted-sg","createdBy":"zrok2-admin-bootstrap"}'

ziti edge update edge-router-policy "$PUBLIC_ERP_ID" \
  --tags-json '{"owner":"zrok-self-hosted-sg","createdBy":"zrok2-admin-bootstrap"}'
```

### 5.4 Create the dynamic-proxy-controller stack

```bash
# 1. Create the bind identity (separate from the public identity)
ziti edge create identity zrok-dynamic-proxy-controller \
  --jwt-output-file /tmp/zrok-dpc.jwt \
  --tags-json '{"owner":"zrok-self-hosted-sg","createdBy":"manual"}'

# 2. Enroll it as the zrok2-controller user (so the .json lands in
#    the right home dir with the right ownership)
sudo mkdir -p /var/lib/zrok2-controller/.zrok2/identities
sudo chown -R zrok2-controller:zrok2-controller /var/lib/zrok2-controller/.zrok2

# Stage the JWT on tmpfs so zrok2-controller can read it
sudo mkdir -p /run/zrok2-enroll && sudo chmod 755 /run/zrok2-enroll
sudo install -m 644 /tmp/zrok-dpc.jwt /run/zrok2-enroll/zrok-dpc.jwt

sudo -u zrok2-controller env HOME=/var/lib/zrok2-controller \
  ziti edge enroll --jwt /run/zrok2-enroll/zrok-dpc.jwt \
  --out /var/lib/zrok2-controller/.zrok2/identities/zrok-dynamic-proxy-controller.json

sudo shred -u /run/zrok2-enroll/zrok-dpc.jwt
sudo rmdir /run/zrok2-enroll
sudo chmod 600 /var/lib/zrok2-controller/.zrok2/identities/zrok-dynamic-proxy-controller.json

# 3. Create the Ziti service + SERP + bind/dial SPs
DPC_ID=$(ziti edge list identities 'name="zrok-dynamic-proxy-controller"' -j | jq -r '.data[0].id')

ziti edge create service zrok-dynamic-proxy-controller \
  --tags-json '{"owner":"zrok-self-hosted-sg"}'
DPC_SVC_ID=$(ziti edge list services 'name="zrok-dynamic-proxy-controller"' -j | jq -r '.data[0].id')

ziti edge create service-edge-router-policy zrok-dpc-serp \
  --edge-router-roles '#all' \
  --service-roles "@${DPC_SVC_ID}" \
  --tags-json '{"owner":"zrok-self-hosted-sg"}'

ziti edge create service-policy zrok-dpc-bind Bind \
  --identity-roles "@${DPC_ID}" \
  --service-roles "@${DPC_SVC_ID}" \
  --tags-json '{"owner":"zrok-self-hosted-sg"}'

ziti edge create service-policy zrok-dpc-dial Dial \
  --identity-roles '#dynamicFrontends' \
  --service-roles "@${DPC_SVC_ID}" \
  --tags-json '{"owner":"zrok-self-hosted-sg"}'
```

### 5.5 Stage identities and start the controller

```bash
sudo bash -c 'chmod 600 /var/lib/zrok2-controller/.zrok2/identities/*.json'

# Copy the public identity into the frontend's home (the frontend reads it)
sudo install -d -m 700 -o zrok2-frontend -g zrok2-frontend /var/lib/zrok2-frontend/.zrok2/identities
sudo install -m 600 -o zrok2-frontend -g zrok2-frontend \
  /var/lib/zrok2-controller/.zrok2/identities/public.json \
  /var/lib/zrok2-frontend/.zrok2/identities/public.json

sudo systemctl enable --now zrok2-controller
sudo journalctl -u zrok2-controller -n 25 --no-pager
# Expect: "database connected", "amqp publisher connected", "started dynamic proxy controller",
# "skipping influx client; no configuration" (expected — metrics disabled), and
# "Serving zrok at http://127.0.0.1:18080".
```

### 5.6 Register the dynamic frontend, namespace, and mapping

```bash
export ZROK2_API_ENDPOINT=http://127.0.0.1:18080
export ZROK2_ADMIN_TOKEN=<ZROK_ADMIN_TOKEN>

PUBLIC_ZID=$(ziti edge list identities 'name="public"' -j | jq -r '.data[0].id')

# Dynamic frontend backed by the 'public' Ziti identity
FRONTEND_TOKEN=$(zrok2 admin create frontend "$PUBLIC_ZID" public --dynamic \
  2>&1 | awk -F"'" '/created global public frontend/ {print $2}')
echo "FRONTEND_TOKEN=$FRONTEND_TOKEN"

# Namespace anchored at the public DNS name
zrok2 admin create namespace <ZROK_FQDN> --token public --open

# Map the frontend to the namespace as default
zrok2 admin create namespace-frontend public "$FRONTEND_TOKEN" --default
```

### 5.7 Configure the frontend

```bash
sudo tee /etc/zrok2/frontend.yml >/dev/null <<EOF
v: 1

frontend_token: ${FRONTEND_TOKEN}
identity: public
bind_address: "0.0.0.0:443"
host_match: <ZROK_FQDN>
mapping_refresh_interval: 1m

amqp_subscriber:
  url: amqp://guest:guest@127.0.0.1:5672
  exchange_name: dynamicProxy

controller:
  identity_path: /var/lib/zrok2-frontend/.zrok2/identities/public.json
  service_name: zrok-dynamic-proxy-controller

tls:
  cert_path: /etc/letsencrypt/live/<ZROK_FQDN>/fullchain.pem
  key_path:  /etc/letsencrypt/live/<ZROK_FQDN>/privkey.pem
EOF
sudo chown zrok2-frontend:zrok2-frontend /etc/zrok2/frontend.yml
sudo chmod 640 /etc/zrok2/frontend.yml
```

### 5.8 TLS group + CAP_NET_BIND_SERVICE drop-ins

The frontend runs as the unprivileged `zrok2-frontend` user, which cannot:
1. Read `privkey.pem` (mode 600 root:root)
2. Bind to TCP/443 (privileged port)

Fix:

```bash
sudo groupadd --system zrok2-tls
sudo usermod -aG zrok2-tls zrok2-frontend
sudo usermod -aG zrok2-tls zrok2-controller   # symmetry, currently unused

sudo bash -c '
chgrp -R zrok2-tls /etc/letsencrypt/archive/<ZROK_FQDN>/
chmod g+r /etc/letsencrypt/archive/<ZROK_FQDN>/*.pem
chmod o+x /etc/letsencrypt /etc/letsencrypt/live /etc/letsencrypt/archive
'

sudo mkdir -p /etc/systemd/system/zrok2-frontend.service.d
sudo tee /etc/systemd/system/zrok2-frontend.service.d/cap-net-bind.conf <<'EOF'
[Service]
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
SupplementaryGroups=zrok2-tls
EOF

sudo mkdir -p /etc/systemd/system/zrok2-controller.service.d
sudo tee /etc/systemd/system/zrok2-controller.service.d/tls-group.conf <<'EOF'
[Service]
SupplementaryGroups=zrok2-tls
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now zrok2-frontend
sudo journalctl -u zrok2-frontend -n 20 --no-pager
# Expect: "started TLS listener", "dynamic proxy controller client started",
# "connected to amqp broker", "retrieved '0' mappings".
```

### 5.9 First account

```bash
GW_PASS="$(openssl rand -base64 24 | tr -d '\n/+=' | head -c 32)"
# Output prints the enable token (12 chars). Save it — you'll need it for Phase 7.
zrok2 admin create account gateway-services@<ZONE> "$GW_PASS"
```

---

## Phase 6 — Build the gateways

```bash
GO_VERSION=1.26.3
curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-arm64.tar.gz" -o /tmp/go.tar.gz
sudo rm -rf /usr/local/go
sudo tar -C /usr/local -xzf /tmp/go.tar.gz
rm /tmp/go.tar.gz
echo 'export PATH=$PATH:/usr/local/go/bin:$HOME/go/bin' | sudo tee /etc/profile.d/go.sh
export PATH=$PATH:/usr/local/go/bin:$HOME/go/bin
go version

go install github.com/openziti/llm-gateway/cmd/llm-gateway@latest
go install github.com/openziti/mcp-gateway/cmd/...@latest

# Install system-wide
for b in llm-gateway mcp-gateway mcp-bridge mcp-tools mcp-filesystem; do
  sudo install -m 755 -o root -g root "$HOME/go/bin/$b" "/usr/local/bin/$b"
done

llm-gateway version
mcp-gateway --version
```

---

## Phase 7 — Wire gateways behind reserved zrok shares (Approach A)

This rollout uses **Approach A**: reserve a named private share with
`zrok2 create share` once, then point the gateway config at the existing
share token. The gateway connects to it on every restart without
re-reserving.

The alternative (Approach B) is the gateway's built-in `--zrok` flag,
which creates a fresh share on every boot. That works for ephemeral
workloads but breaks any consumer relying on a stable share token.

### 7.1 Service users

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin \
  --comment "llm-gateway service user" llm-gw
sudo useradd --system --create-home --shell /usr/sbin/nologin \
  --comment "mcp-gateway service user" mcp-gw
```

### 7.2 Enable each user's zrok2 client against the local controller

```bash
GATEWAY_ACCOUNT_TOKEN="<from-phase-5.9>"

for u in llm-gw mcp-gw; do
  sudo -u "$u" zrok2 config set apiEndpoint http://127.0.0.1:18080
  sudo -u "$u" zrok2 enable --headless \
    --description "${u}@$(hostname)" "$GATEWAY_ACCOUNT_TOKEN"
done
```

> **Trap**: pointing the client at the public URL (e.g.
> `https://<ZROK_FQDN>`) from inside the VM fails with `no route to host`.
> OCI blocks hairpin connections to the instance's own public IP. Use the
> loopback API endpoint (`http://127.0.0.1:18080`) for all service users.

> **Trap**: `zrok2 enable` requires a TTY unless you pass `--headless`.

### 7.3 Reserve stable named private shares

```bash
sudo -u llm-gw zrok2 create share --backend-mode proxy --share-token llm-gateway
sudo -u mcp-gw zrok2 create share --backend-mode proxy --share-token mcp-gateway
```

> v1 zrok had `zrok reserve`. In v2 the command is `zrok2 create share`.

### 7.4 llm-gateway config

```bash
sudo install -d -m 755 -o root -g root /etc/llm-gateway
sudo install -d -m 750 -o llm-gw -g llm-gw /var/log/llm-gateway

sudo tee /etc/llm-gateway/config.yaml <<'EOF'
listen: "127.0.0.1:8080"

zrok:
  share:
    enabled: true
    mode: private
    token: "llm-gateway"

providers:
  open_ai:
    api_key: "${OPENAI_API_KEY}"
  anthropic:
    api_key: "${ANTHROPIC_API_KEY}"
EOF
sudo chown root:llm-gw /etc/llm-gateway/config.yaml
sudo chmod 640 /etc/llm-gateway/config.yaml

sudo tee /etc/llm-gateway/env <<'EOF'
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
EOF
sudo chown root:llm-gw /etc/llm-gateway/env
sudo chmod 640 /etc/llm-gateway/env
```

### 7.5 mcp-gateway config (scaffold)

```bash
sudo install -d -m 755 -o root -g root /etc/mcp-gateway
sudo install -d -m 750 -o mcp-gw -g mcp-gw /var/log/mcp-gateway
sudo install -d -m 750 -o mcp-gw -g mcp-gw /var/lib/mcp-gateway/scratch

sudo tee /etc/mcp-gateway/config.yaml <<'EOF'
share_token: "mcp-gateway"

aggregator:
  name: "<your-name>-mcp"
  version: "0.1"
  separator: "_"
  connection:
    connect_timeout: 30s
    call_timeout: 60s

backends:
  # mcp-gateway refuses to start with zero backends. Placeholder so the
  # service runs while you wire real backends in. Replace before relying.
  - id: "scratch-fs"
    name: "Placeholder filesystem (scaffold)"
    transport:
      type: "stdio"
      command: "/usr/local/bin/mcp-filesystem"
      args:
        - "/var/lib/mcp-gateway/scratch"
    tools:
      mode: "allow"
      list:
        - "list_directory"
EOF
sudo chown root:mcp-gw /etc/mcp-gateway/config.yaml
sudo chmod 640 /etc/mcp-gateway/config.yaml

sudo tee /etc/mcp-gateway/env <<'EOF'
# Per-backend env vars (none yet)
EOF
sudo chown root:mcp-gw /etc/mcp-gateway/env
sudo chmod 640 /etc/mcp-gateway/env
```

### 7.6 systemd units

`/etc/systemd/system/llm-gateway.service`:

```ini
[Unit]
Description=llm-gateway: OpenAI-compatible LLM proxy behind a zrok private share
After=network-online.target zrok2-controller.service
Wants=network-online.target

[Service]
Type=simple
User=llm-gw
Group=llm-gw
EnvironmentFile=-/etc/llm-gateway/env
WorkingDirectory=/home/llm-gw
ExecStart=/usr/local/bin/llm-gateway run /etc/llm-gateway/config.yaml
Restart=on-failure
RestartSec=3

NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/llm-gw /var/log/llm-gateway
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=true
MemoryDenyWriteExecute=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/mcp-gateway.service` is the same, with these changes:

- `User=mcp-gw`, `Group=mcp-gw`
- `EnvironmentFile=-/etc/mcp-gateway/env`
- `WorkingDirectory=/home/mcp-gw`
- `ExecStart=/usr/local/bin/mcp-gateway run /etc/mcp-gateway/config.yaml`
- `ReadWritePaths=/home/mcp-gw /var/log/mcp-gateway /var/lib/mcp-gateway`
- omit `MemoryDenyWriteExecute=true` (breaks some stdio backend runtimes)

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now llm-gateway mcp-gateway
sudo journalctl -u llm-gateway -n 15 --no-pager
sudo journalctl -u mcp-gateway -n 15 --no-pager
# Each should log "connecting to existing zrok share" then "listener ready".
```

### 7.7 Verify gateways via Ziti

```bash
# Spawn a temp env to act as a private accessor (any zrok env under the
# same account works). Disable it afterwards.
zrok2 config set apiEndpoint http://127.0.0.1:18080
zrok2 enable --headless --description "verify@$(date -u +%H%M%S)" "$GATEWAY_ACCOUNT_TOKEN"

zrok2 access private llm-gateway --bind 127.0.0.1:9191 --headless &
LLM_PID=$!
sleep 3
curl -s http://127.0.0.1:9191/health           # {"status":"ok"}
curl -s http://127.0.0.1:9191/v1/models | head # OpenAI-style model list
kill $LLM_PID

zrok2 access private mcp-gateway --bind 127.0.0.1:9192 --headless &
MCP_PID=$!
sleep 3
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9192/health   # 200
kill $MCP_PID

zrok2 disable
rm -rf ~/.zrok2
```

---

## Phase 8 — End-to-end verification

### 8.1 Internal health checks

```bash
systemctl is-active postgresql rabbitmq-server zrok2-controller zrok2-frontend llm-gateway mcp-gateway
sudo ss -tlnp | grep -E ':443|:5432|:5672|:18080'
```

### 8.2 Loopback TLS apex + wildcard routing

```bash
# Apex
curl -sv --resolve <ZROK_FQDN>:443:127.0.0.1 \
  -o /dev/null https://<ZROK_FQDN>/ 2>&1 \
  | grep -E 'subject|issuer|verify return|HTTP'
# Expect: 404 (no share), TLS cert verify OK, issuer Let's Encrypt

# Wildcard
curl -s --resolve abc123.<ZROK_FQDN>:443:127.0.0.1 \
  -o /dev/null -w 'HTTP: %{http_code}\n' https://abc123.<ZROK_FQDN>/
# Expect: HTTP: 404 (frontend serves, no share matches)

# host_match rejection
curl -s --resolve notamatch.example:443:127.0.0.1 \
  -o /dev/null -w 'HTTP: %{http_code}\n' https://notamatch.example/
# Expect: HTTP: 000 (frontend refuses non-matching Host header)
```

### 8.3 External probe (mandatory)

The OCI hairpin restriction means `curl https://<ZROK_FQDN>/` from inside
the VM fails. Probe from outside (your laptop, a CI runner, or any other
host with internet). HTTP 404 with a valid Let's Encrypt chain confirms
the full path.

### 8.4 Host iptables fix (often required after Phase 8.3 fails)

If the external probe times out / refuses, the host iptables INPUT chain
likely ends with a REJECT-everything-else rule (default on many cloud
images). Add an ACCEPT for TCP/443 *before* that REJECT:

```bash
# Snapshot first
sudo iptables-save | sudo tee ~/iptables-pre-fix.rules >/dev/null

# Find the REJECT rule's line number
sudo iptables -L INPUT -n --line-numbers
# Look for: <N>  REJECT   ...  reject-with icmp-host-prohibited

# Insert AT that line number (pushes REJECT down by one)
sudo iptables -I INPUT <N> -p tcp --dport 443 -m state --state NEW -j ACCEPT

# Persist across reboots
sudo apt install -y iptables-persistent
sudo netfilter-persistent save
```

Re-run the external probe — should now return HTTP 404.

### 8.5 Ziti additions audit

```bash
ziti edge list identities                   'tags.owner = "zrok-self-hosted-sg" limit 1000' -j | jq -r '.data[].name'
ziti edge list services                     'tags.owner = "zrok-self-hosted-sg" limit 1000' -j | jq -r '.data[].name'
ziti edge list edge-router-policies         'tags.owner = "zrok-self-hosted-sg" limit 1000' -j | jq -r '.data[].name'
ziti edge list service-edge-router-policies 'tags.owner = "zrok-self-hosted-sg" limit 1000' -j | jq -r '.data[].name'
ziti edge list service-policies             'tags.owner = "zrok-self-hosted-sg" limit 1000' -j | jq -r '.data[].name'

# Expect (post-Phase 5):
#   identities:                    public, zrok-dynamic-proxy-controller
#   services:                      zrok-dynamic-proxy-controller
#   edge-router-policies:          public
#   service-edge-router-policies:  zrok-dpc-serp
#   service-policies:              zrok-dpc-bind (Bind), zrok-dpc-dial (Dial)
```

Per-share services (auto-created on `zrok2 create share`, tagged
`zrokShareToken` by zrok itself):

```bash
ziti edge list services 'tags.zrokShareToken != null limit 1000' -j | jq -r '.data[].name'
# Expect: llm-gateway, mcp-gateway
```

---

## What's NOT done by this rollout

| Item | Where to do it |
|---|---|
| Real OpenAI / Anthropic API keys | `/etc/llm-gateway/env`, then `systemctl restart llm-gateway` |
| Real MCP backends | `/etc/mcp-gateway/config.yaml backends:`, then `systemctl restart mcp-gateway` |
| `api_keys` gate on llm-gateway | uncomment in `/etc/llm-gateway/config.yaml`; generate with `llm-gateway genkey` |
| zrok deploy hook for cert renewal | run `certbot certonly --deploy-hook 'systemctl restart zrok2-frontend' ...` |
| First non-admin zrok account | `zrok2 admin create account <email> <password>` |
| Optional: tighten host firewall further | up to you |
| Reserved (vs ephemeral) public IP | OCI Console "Reserve" button (atomic; API can't do this without losing the IP) |

---

## Rollback strategy

Four independent layers — pick the lightest one that puts you in a
known-good state. The external Ziti cleanup (Layer C) is always needed
regardless of which VM-side rollback you choose, because no VM snapshot
can undo objects on a different host.

### Layer A — soft, in-place

```bash
sudo systemctl disable --now llm-gateway mcp-gateway zrok2-{frontend,controller}
sudo apt purge -y zrok2 zrok2-controller zrok2-frontend rabbitmq-server postgresql
sudo rm -rf /etc/zrok2 /etc/llm-gateway /etc/mcp-gateway \
            /var/lib/zrok2-* /var/lib/mcp-gateway \
            /etc/letsencrypt/live/<ZROK_FQDN>* \
            /etc/systemd/system/zrok2-*.service.d \
            /etc/systemd/system/{llm,mcp}-gateway.service
sudo userdel -r llm-gw 2>/dev/null
sudo userdel -r mcp-gw 2>/dev/null
sudo systemctl daemon-reload
```

### Layer B — full VM restore from Phase 0.6 backup

Use the saved `BACKUP_OCID`. The instance keeps its VNIC + public IP if
you swap the boot volume rather than terminating:

```bash
NEW_BOOT_OCID=$(oci bv boot-volume create \
  --compartment-id "$COMP_OCID" --availability-domain "$AD" \
  --source-details "{\"type\":\"bootVolumeBackup\",\"id\":\"$BACKUP_OCID\"}" \
  --display-name "restored-$(date -u +%Y%m%dT%H%M%SZ)" \
  --wait-for-state AVAILABLE --query 'data.id' --raw-output)

oci compute instance action --instance-id "$INSTANCE_OCID" \
  --action SOFTSTOP --wait-for-state STOPPED

OLD_ATTACH_OCID=$(oci compute boot-volume-attachment list \
  --compartment-id "$COMP_OCID" --availability-domain "$AD" \
  --instance-id "$INSTANCE_OCID" --query 'data[0].id' --raw-output)
oci compute boot-volume-attachment detach \
  --boot-volume-attachment-id "$OLD_ATTACH_OCID" --force \
  --wait-for-state DETACHED

oci compute boot-volume-attachment attach \
  --instance-id "$INSTANCE_OCID" --boot-volume-id "$NEW_BOOT_OCID" \
  --wait-for-state ATTACHED

oci compute instance action --instance-id "$INSTANCE_OCID" \
  --action START --wait-for-state RUNNING
```

### Layer C — external Ziti cleanup (always required)

Run `scripts/teardown-ziti.sh --dry-run` first to inventory; then without
flags to actually delete. The script targets only objects tagged
`owner=zrok-self-hosted-sg` or named `zrok-*`.

If Layer B was used, the Phase 2.1 preflight snapshots are gone from the
VM. Run the script from any other machine with the ziti CLI + admin
creds, or retrieve the snapshots from Object Storage first.

### Layer D — adjacent resources (manual)

| Resource | Action |
|---|---|
| VCN security list TCP/443 rule | OCI Console or `oci network security-list update` (remove that one rule) |
| Reserved public IP | OCI Console / `oci network public-ip delete` |
| Cloudflare DNS records | Delete `<ZROK_FQDN>` and `*.<ZROK_FQDN>` A records |
| Cloudflare API token | Revoke in Cloudflare dashboard |
| Let's Encrypt cert | `certbot revoke --cert-name <ZROK_FQDN>` (optional; expires in 90 days anyway) |
| Boot-volume backup | `oci bv boot-volume-backup delete --boot-volume-backup-id <BACKUP_OCID>` |
| Object Storage snapshots bucket | Delete bucket contents + bucket itself |

---

## Common traps, summarized

1. **OCI identity-domain policy syntax** — qualified group form
   `'Default'/'<name>'` is required.
2. **OCI public IP API** — there is no atomic ephemeral→reserved promote
   via CLI/API; only the Console works.
3. **OCI boot-volume backup CLI** — don't pass `--wait-interval-seconds`
   or `--max-wait-seconds`; they don't exist on that command and cause a
   misleading parse error.
4. **OCI security-list update is full replacement** — always snapshot
   the existing ingress array, then append, then submit.
5. **OCI hairpin** — connections from inside the VM to its own public IP
   are blocked. Use `127.0.0.1` for any self-test or service config.
6. **zrok apt repo codename** — use `debian main`, not Ubuntu codenames.
7. **zrok bootstrap auto-creates `public`** — without role attribute and
   without tags. Add `--role-attributes dynamicFrontends` and
   `--tags-json '...'` immediately after bootstrap.
8. **zrok admin tag flag** — `ziti edge` accepts `--tags-json`, not
   `--tags` (which is CSV).
9. **Ziti CLI default page size = 10** — always pass `'limit 1000'` in
   inventory queries.
10. **RabbitMQ hostname/EPMD** — pinning `NODENAME=rabbit@localhost` is
    required when EPMD is bound to 127.0.0.1.
11. **Host iptables REJECT** — many cloud images ship with a default
    REJECT at the end of INPUT. OCI sec-list ACCEPT is not enough; the
    host firewall also needs ACCEPT for TCP/443.
12. **zrok2 enable needs `--headless`** when run via `sudo -u` (no TTY).
13. **CAP_NET_BIND_SERVICE drop-in** is required for the unprivileged
    zrok2-frontend user to bind 443.
14. **TLS group ACL** — without `zrok2-tls` group + chgrp, the frontend
    can't read privkey.pem.
15. **mcp-gateway requires ≥1 backend** to start. Use the
    `mcp-filesystem` placeholder until you have real backends.
