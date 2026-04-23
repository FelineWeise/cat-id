# Deploy on Scaleway (`cat-id.eu`)

Terminal-oriented flow: DNS (Terraform or CLI), Docker Compose on the VM, Spotify callback alignment.

## 0) Preconditions

- Hostname: `app.cat-id.eu` (recommended).
- Scaleway API credentials:
  - `SCW_ACCESS_KEY`
  - `SCW_SECRET_KEY`
  - `SCW_DEFAULT_PROJECT_ID` (recommended)

```bash
brew install jq scaleway/tap/scw
```

## 1) Preflight

```bash
cd /path/to/cat-id
DOMAIN=cat-id.eu ./scripts/dns_preflight.sh
```

Optional strict check:

```bash
DOMAIN=cat-id.eu \
APP_HOST=app.cat-id.eu \
EXPECTED_PUBLIC_IPV4=<your-instance-ipv4> \
./scripts/dns_preflight.sh
```

## 2A) Terraform (recommended)

**Terraform vs app secrets:** only the DNS zone and `records` (A/AAAA). Not `SPOTIFY_*` / `APP_BASE_URL` — those go in `infrastructure/compose/env/production.env`.

```bash
cd infrastructure/dns
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars — set records to your instance public IPv4
terraform init
terraform plan
terraform apply
terraform output nameservers
```

## 2B) CLI DNS apply

Edit [`infrastructure/dns/dns_records.sample.json`](../infrastructure/dns/dns_records.sample.json), then:

```bash
./scripts/scaleway_dns_apply.sh --config infrastructure/dns/dns_records.sample.json --dry-run
./scripts/scaleway_dns_apply.sh --config infrastructure/dns/dns_records.sample.json --apply
```

If records already exist, prefer Terraform or edit records in the console instead of duplicate `add`.

## 3) TLS on the host

Use Caddy (see `infrastructure/compose/compose.stack.yml`) or nginx. For Caddy in Compose, see [`infrastructure/compose/README.md`](../infrastructure/compose/README.md).

## 4) Spotify env (non-secret preview)

```bash
./scripts/print_spotify_prod_env.sh --public-host app.cat-id.eu
```

## 5) Run containers on the VM

```bash
cd /path/to/cat-id
cp infrastructure/compose/env/production.env.example infrastructure/compose/env/production.env
# Edit production.env
docker compose -f infrastructure/compose/compose.stack.yml up -d
```

If **80/443** are already taken, use `compose.app.yml` and your existing edge proxy — see compose README.

## 6) Smoke tests

```bash
BASE_URL=https://app.cat-id.eu ./scripts/smoke_test.sh
EXPECTED_IPV4=<your-instance-ipv4> BASE_URL=https://app.cat-id.eu ./scripts/verify_cat_id_deployment.sh
```

## 7) Single instance (apex + app same IP)

Point **`cat-id.eu`** and **`app.cat-id.eu`** at the **same** public IPv4 (e.g. one Scaleway Instance). Update `terraform.tfvars` or `dns_records.sample.json`, apply, then deploy with `compose.stack.yml` on that host.

## 8) Clone path on server (example)

```bash
mkdir -p /opt && cd /opt
git clone https://github.com/FelineWeise/cat-id.git
cd cat-id
cp infrastructure/compose/env/production.env.example infrastructure/compose/env/production.env
# edit env
docker compose -f infrastructure/compose/compose.stack.yml up -d
```

Staging (optional):

```bash
cp infrastructure/compose/env/staging.env.example infrastructure/compose/env/staging.env
docker compose -f infrastructure/compose/compose.staging.yml up -d
```
