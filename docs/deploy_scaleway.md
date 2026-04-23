# Deploy on Scaleway (`cat-id.eu`) — slim path (LAED host)

cat-id **does not** provision its own VPC, VM, or flexible IP in this repository. The **LAED** stack owns the server, volume, cloud-init, and (usually) Docker auth for Scaleway Container Registry. This repo owns **DNS** ([`infrastructure/dns/`](../infrastructure/dns/)), **production manifests** ([`infrastructure/deploy/`](../infrastructure/deploy/)), and **optional** registry namespace Terraform ([`infrastructure/registry/`](../infrastructure/registry/)).

**Separation on one shared VM:** use a dedicated **registry namespace** or image path, a **Docker bridge network** (`cat-id-net`), and a **data directory** (e.g. `/opt/cat-id-data`) distinct from LAED’s paths—not a second VPC on the same OS without another NIC/VM.

## 0) Preconditions

- Hostname: `app.cat-id.eu` (recommended).
- **LAED** base host reachable via SSH; its **public IPv4** (flexible IP) known for DNS.
- Scaleway API credentials for DNS / optional registry TF:
  - `SCW_ACCESS_KEY`
  - `SCW_SECRET_KEY`
  - `SCW_DEFAULT_PROJECT_ID` (recommended)

```bash
brew install jq scaleway/tap/scw
```

## 1) Preflight (DNS)

```bash
cd /path/to/cat-id
DOMAIN=cat-id.eu ./scripts/dns_preflight.sh
```

Strict check against LAED’s IP:

```bash
DOMAIN=cat-id.eu \
APP_HOST=app.cat-id.eu \
EXPECTED_PUBLIC_IPV4=<laed-base-flexible-ipv4> \
./scripts/dns_preflight.sh
```

## 2A) Terraform DNS (recommended)

**Terraform vs app secrets:** only the DNS zone and **A**/**AAAA** records—not `SPOTIFY_*` / `APP_BASE_URL`. Those live in **`infrastructure/deploy/production.env`** on the server.

```bash
cd infrastructure/dns
cp terraform.tfvars.example terraform.tfvars
# Set records → LAED base public IPv4 (same IP for apex and app if both on that host)
terraform init
terraform plan
terraform apply
terraform output nameservers
```

## 2B) CLI DNS apply

Edit [`infrastructure/dns/dns_records.sample.json`](../infrastructure/dns/dns_records.sample.json) (targets = LAED IP), then:

```bash
./scripts/scaleway_dns_apply.sh --config infrastructure/dns/dns_records.sample.json --dry-run
./scripts/scaleway_dns_apply.sh --config infrastructure/dns/dns_records.sample.json --apply
```

## 3) Container image (Scaleway Container Registry)

**Option A — dedicated cat-id namespace (optional Terraform):**

```bash
cd infrastructure/registry
terraform init && terraform apply
# Note terraform output registry_endpoint → e.g. rg.fr-par.scw.cloud/cat-id
```

Build, tag, push (example):

```bash
docker build -t cat-id:release .
docker tag cat-id:release rg.fr-par.scw.cloud/cat-id/cat-id:v1.0.0
docker push rg.fr-par.scw.cloud/cat-id/cat-id:v1.0.0
```

**Option B — reuse LAED’s namespace:** push as `rg.<region>.scw.cloud/<laed-namespace>/cat-id:<tag>` and set `CAT_ID_IMAGE` accordingly—**no** `infrastructure/registry/` apply.

Pin **tags or digests** for production; avoid relying only on `:latest`.

## 4) Runtime on the LAED host

1. SSH to the LAED base server (`terraform output` from the **LAED** repo for IP/SSH).
2. Place [`infrastructure/deploy/`](../infrastructure/deploy/) files on disk, e.g. **`/opt/cat-id-data`**: `docker-compose.yml`, `Caddyfile`, and **`production.env`** (from `production.env.example`).
3. Reuse **`/root/.docker/config.json`** from LAED bootstrap for `docker compose pull` when running as root, or `docker login` to `rg.<region>.scw.cloud` once.
4. From that directory:

```bash
export CAT_ID_IMAGE=rg.fr-par.scw.cloud/cat-id/cat-id:v1.0.0
docker compose pull
docker compose up -d
```

**TLS / Caddy:** [`infrastructure/deploy/docker-compose.yml`](../infrastructure/deploy/docker-compose.yml) publishes **80/443** for this stack’s Caddy. If LAED already uses those ports, do **not** run a second Caddy—add `app.cat-id.eu` to **LAED’s** edge proxy, or run **app-only** Compose from [`infrastructure/compose/compose.app.yml`](../infrastructure/compose/compose.app.yml) and route upstream. Details: [`infrastructure/deploy/README.md`](../infrastructure/deploy/README.md), [`infrastructure/compose/README.md`](../infrastructure/compose/README.md).

## 5) Spotify env (non-secret preview)

```bash
./scripts/print_spotify_prod_env.sh --public-host app.cat-id.eu
```

## 6) Smoke tests

```bash
BASE_URL=https://app.cat-id.eu ./scripts/smoke_test.sh
EXPECTED_IPV4=<laed-base-flexible-ipv4> BASE_URL=https://app.cat-id.eu ./scripts/verify_cat_id_deployment.sh
```

## 7) Local / dev-oriented Compose

For building from the repo or GHCR defaults, see [`infrastructure/compose/`](../infrastructure/compose/) (`compose.stack.yml`, etc.). **Production** on Scaleway should follow **`infrastructure/deploy/`** (image-only, CR refs).

## Summary

| Step | Action |
|------|--------|
| DNS | `infrastructure/dns` → A records to **LAED** public IP |
| Image | Build → tag → push to **SCW CR** (optional `infrastructure/registry`) |
| Run | Copy `infrastructure/deploy` to e.g. `/opt/cat-id-data` → `docker compose pull && up -d` |
