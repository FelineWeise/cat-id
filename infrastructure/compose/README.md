# Compose (Docker on Scaleway)

Docker Compose files and Caddy config for **local/dev** and convenience layouts. **Production on the LAED host** with Scaleway Container Registry images only: use [`../deploy/`](../deploy/) (copy to e.g. `/opt/cat-id-data`); see [../../docs/deploy_scaleway.md](../../docs/deploy_scaleway.md).

## Production stack (app + TLS)

On the **same** Scaleway instance that serves `app.cat-id.eu`:

1. **DNS:** Apex and `app` **A** records → the **LAED base host’s** public IPv4 (see [`../dns/terraform.tfvars.example`](../dns/terraform.tfvars.example) and [`../dns/dns_records.sample.json`](../dns/dns_records.sample.json)).
2. **Terraform:** `cd infrastructure/dns && terraform apply`
3. **On the VM:**

```bash
cd /path/to/cat-id
cp infrastructure/compose/env/production.env.example infrastructure/compose/env/production.env
# edit production.env (Spotify, Last.fm, https://app.cat-id.eu)

docker compose -f infrastructure/compose/compose.stack.yml up -d
```

**Port conflict:** `compose.stack.yml` publishes **80** and **443**. If another service already uses them, use **`compose.app.yml` only**, publish the app to loopback (snippet below), and add `app.cat-id.eu` to your existing reverse proxy.

**Image:** Compose uses `CAT_ID_IMAGE` if set; otherwise the default is **GitHub Container Registry** `ghcr.io/felineweise/cat-id:latest` (matches GitHub repo **`FelineWeise/cat-id`**). Override if you publish under another tag or registry:

```bash
export CAT_ID_IMAGE=ghcr.io/felineweise/cat-id:main
docker compose -f infrastructure/compose/compose.stack.yml up -d
```

If no image is available, add a `build` section on the `cat-id` service (`context`: repo root, `dockerfile: Dockerfile`) and run `docker compose ... up -d --build`.

### App only + existing reverse proxy

Use `compose.app.yml` and bind the app to loopback:

```yaml
ports:
  - "127.0.0.1:18000:8000"
```

Then proxy `app.cat-id.eu` → `http://127.0.0.1:18000`.

## Files

| File | Purpose |
|------|---------|
| `compose.stack.yml` | App + Caddy (80/443) |
| `compose.app.yml` | App container only |
| `compose.staging.yml` | Staging container |
| `Caddyfile.production` | TLS for `app.cat-id.eu` only |
| `Caddyfile.example` | Production + staging hostnames |

## Secrets

- `env/production.env` and `env/staging.env` are **gitignored**.
- Copy from `*.env.example` on the server.

## Scaleway console (manual)

1. **Instance:** Public IPv4 matches Terraform `records.*.data` (flexible IP recommended).
2. **DNS:** Zone `cat-id.eu` — `@` and `app` **A** → that IPv4.
3. **Firewall:** Inbound **22**, **80**, **443** (restrict **22** when possible).

## Verify

```bash
EXPECTED_IPV4=<your-server-public-ip> BASE_URL=https://app.cat-id.eu ./scripts/verify_cat_id_deployment.sh
```
