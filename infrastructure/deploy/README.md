# Production deploy bundle (LAED host)

This folder is the **runtime contract** for cat-id on the **LAED base VM**: Compose + Caddy use **only `image:`** references to **Scaleway Container Registry**—no `build:` for the app.

## Ownership

| Layer | Owner |
|-------|--------|
| VPC, VM, flexible IP, volume, cloud-init, `/root/.docker/config.json` | **LAED** Terraform |
| `cat-id.eu` DNS | **cat-id** [`../dns/`](../dns/) — A records → LAED **public** IP |
| Optional dedicated registry namespace | **cat-id** [`../registry/`](../registry/) *or* push under LAED’s namespace |
| Files in this folder on disk | You — e.g. **`/opt/cat-id-data`** |

## On the server

1. Create a directory (example):

   ```bash
   sudo mkdir -p /opt/cat-id-data
   sudo chown "$USER:$USER" /opt/cat-id-data
   ```

2. Copy **`docker-compose.yml`**, **`Caddyfile`**, and create **`production.env`** from **`production.env.example`**.

3. Set **`CAT_ID_IMAGE`** to your pushed image, e.g. `rg.fr-par.scw.cloud/cat-id/app:v1.0.0` (namespace `cat-id`, repository `app`; pin immutable tags or digests in production, do not rely on `:latest`).

4. From that directory:

   ```bash
   export CAT_ID_IMAGE=rg.fr-par.scw.cloud/cat-id/app:v1.0.0
   docker compose pull
   docker compose up -d
   ```

   If you put **`CAT_ID_IMAGE`** in **`production.env`** instead, Compose only sees it when you pass that file: `docker compose --env-file production.env pull && docker compose --env-file production.env up -d`.

   **Default layout (LAED shares the host):** only the **app** container runs on host port **`18008`**. LAED’s Caddy runs **inside Docker**, so it must **not** use `127.0.0.1` on the host (that is unreachable from other containers). Use **`host.docker.internal`** after wiring it on the **LAED** `caddy` service:

   ```yaml
   extra_hosts:
     - "host.docker.internal:host-gateway"
   ```

   Then add this site to **LAED’s** `Caddyfile` and reload Caddy:

   ```caddyfile
   app.cat-id.eu {
     encode zstd gzip
     reverse_proxy host.docker.internal:18008
   }
   ```

   **Standalone VPS** (nothing else on 80/443):  
   `docker compose --profile standalone-tls up -d`  
   and use the bundled **`Caddyfile`** in this folder instead.

**Registry login:** If LAED cloud-init already wrote Docker credentials for Scaleway CR, `docker compose pull` works as root with that config. Otherwise:

```bash
printf '%s' "$SCW_SECRET_KEY" | docker login rg.fr-par.scw.cloud/cat-id -u nologin --password-stdin
```

(Use your namespace path; secret key, not access key id.)

## Image CPU architecture

Scaleway **GP/General** instances are **amd64**. Build and push from a Mac with:

```bash
./scripts/push_to_scaleway_registry.sh v1.0.1
```

(the script uses `docker build --platform linux/amd64`).

## Ports and Caddy

- **Shared LAED host:** do **not** use the bundled Caddy (profile `standalone-tls` off). Proxy from **LAED** to **`host.docker.internal:18008`** (with `extra_hosts` on LAED’s `caddy` service).
- **Dedicated host:** `docker compose --profile standalone-tls up -d` to use **80/443** here.

## Isolation on a shared host

- **Registry:** separate namespace or image path (`.../cat-id/...` vs `.../laed/...`).
- **Runtime:** Docker network **`cat-id-net`** and data dir **`/opt/cat-id-data`** (vs LAED’s paths).

Full workflow: [../../docs/deploy_scaleway.md](../../docs/deploy_scaleway.md).
