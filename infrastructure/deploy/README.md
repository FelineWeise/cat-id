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

3. Set **`CAT_ID_IMAGE`** to your pushed image, e.g. `rg.fr-par.scw.cloud/cat-id/app:v1.0.0` (namespace `cat-id`, repository `app`; pin tags or digests in production).

4. From that directory:

   ```bash
   export CAT_ID_IMAGE=rg.fr-par.scw.cloud/cat-id/app:v1.0.0
   docker compose pull
   docker compose up -d
   ```

**Registry login:** If LAED cloud-init already wrote Docker credentials for Scaleway CR, `docker compose pull` works as root with that config. Otherwise `docker login rg.fr-par.scw.cloud` (or your region endpoint) once.

## Ports and Caddy

- This stack publishes **80** and **443** for Caddy.
- If LAED already terminates TLS on those ports, **do not** run this Caddy: use **`compose.app.yml`** from [`../compose/`](../compose/) (app only) and add **`app.cat-id.eu`** to **LAED’s** edge proxy, or merge the `reverse_proxy` block into LAED’s Caddyfile.

## Isolation on a shared host

- **Registry:** separate namespace or image path (`.../cat-id/...` vs `.../laed/...`).
- **Runtime:** Docker network **`cat-id-net`** and data dir **`/opt/cat-id-data`** (vs LAED’s paths).

Full workflow: [../../docs/deploy_scaleway.md](../../docs/deploy_scaleway.md).
