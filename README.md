# Follow Your Cat ID

A web app that finds songs similar to a given Spotify track through one unified discovery flow that blends listener and audio signals.

## Features

- **Spotify URL input** — paste any Spotify track link
- **Unified blended similarity** — merges listener-style and audio-style signals into a single ranked result list
- **Configurable similarity weights** — sliders for each audio dimension; traits with weight above 30% are used as recommendation targets
- **30-second preview playback** via Deezer (fallback when Spotify previews are unavailable)
- **Spotify integration** — album art, direct links, playlist creation, and queue control (requires OAuth)
- **Provider-routed queue** — select queue provider in-app:
  - `Spotify` (when connected) routes queue actions to your Spotify account queue
- **Memory Board** — save tracks (`+ Board`), persist locally, batch-create Spotify playlists, and copy board items as fallback text
- Configurable result count (5 / 10 / 20 / 50)

### Unified similarity behavior

- The app uses one similarity pipeline and one result surface.
- The backend blends listener and audio candidates into one ranking contract.
- If Spotify audio-features access is unavailable, approximation and third-party analysis enrichment are used.
- **Optional strict Spotify-only results:** set `strict_mapped_only` to `true` on `/api/similar/unified` to return only tracks with a `spotify_id` (queue/playlist-safe rows).
- Optional **MusicBrainz** hints (`use_metadata_fallback`, default `true`) retry Spotify resolution when Deezer/Spotify text matching fails, including URL relation extraction for direct Spotify track IDs when available.
- If the user is connected to Spotify, mapping/search/queue calls use the user token path; anonymous users use app-token mapping.

## Prerequisites

- Python 3.10+
- [Poetry](https://python-poetry.org/docs/#installation)
- A **Spotify Developer** application (free) — create one at https://developer.spotify.com/dashboard
- A **Last.fm API key** (free, required for Listener mode) — create one at https://www.last.fm/api/account/create

> **Note on Spotify API access:** Audio features and recommendations require your own Spotify Client ID and Secret. Spotify restricted these endpoints for newer apps — verify that your app has access in the developer dashboard before using Audio Similarity mode.

## Setup

```bash
# Clone (repo path on GitHub should be FelineWeise/cat-id — rename in GitHub Settings if needed)
git clone https://github.com/FelineWeise/cat-id.git
cd cat-id

# Install dependencies
poetry install

# Configure credentials
cp .env.example .env
# Edit .env — see the table below for required keys
```

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `SPOTIFY_CLIENT_ID` | Yes | Client credentials for track lookup and audio features |
| `SPOTIFY_CLIENT_SECRET` | Yes | Client credentials for track lookup and audio features |
| `LASTFM_API_KEY` | Listener mode | Last.fm `track.getSimilar` and tag enrichment |
| `SPOTIFY_REDIRECT_URI` | OAuth features | Playlist/queue actions; must match your Spotify app settings |
| `APP_BASE_URL` | Optional | Base URL for current environment (local dev / production) |
| `APP_ENV` | Optional | `development` (local SSL/reload) or `production` |
| `ALLOWED_ORIGINS` | Recommended | Comma-separated CORS allowlist |
| `ENABLE_DEBUG_ENDPOINT` | Optional | Set `false` for public deployments |
| `SESSION_STORE_BACKEND` | Optional | `memory` (default) or `redis` for shared OAuth sessions |
| `SESSION_TTL_SECONDS` | Optional | Session TTL in seconds (default `3600`) |
| `REDIS_URL` | Required for redis backend | Redis URL for shared OAuth session storage |

## Running

For Spotify OAuth to work reliably in browser, use a trusted local certificate.

```bash
# one-time cert setup (recommended)
./setup_certs.sh

# run the app
poetry run python run.py
```

Then open **`https://localhost:8000`** (do not use `0.0.0.0`).

`run.py` uses development mode defaults:
- host `0.0.0.0`
- local SSL with `key.pem` + `cert.pem`
- auto-reload enabled

Useful local overrides:
- `NO_SSL=1` to run without local certs
- `NO_RELOAD=1` to disable reload
- `HOST=127.0.0.1 PORT=8000` to pin binding explicitly

### Spotify queue + link behavior

- Queue actions require a Spotify playback device to exist for your account.
- If no active device is found, the backend attempts one automatic transfer to an available device, then retries queueing.
- If no devices are available at all, open Spotify on desktop/mobile/web player first, then try queueing again.
- Track links attempt to open the Spotify desktop/app first (`spotify:` deep link) and fall back to web player if the OS/browser does not hand off to the app.
- Queue behavior:
  - Spotify queue is the only queue-capable path in-app.
  - Single-track and bulk `Add to Queue` use `/api/spotify/queue` with the connected user session.
  - Deezer and other external providers remain link/open sources, not queue targets.

### Text playlist builder behavior

- Add individual result rows to text list with `+TXT`.
- Add current visible results in one action from the text panel.
- Export current text lines to `playlist-export.txt`.
- Create Spotify playlist from text lines directly in-app via `/api/spotify/playlist/from-text`.
- Text line format: `Artist — Track` (one per line). Unmatched lines are reported back in the response summary.

## Rate limits and auth notes

### Spotify endpoints currently used

- Seed metadata: `GET /v1/tracks/{id}`
- Mapping: `GET /v1/search` (text + ISRC patterns), optional `GET /v1/tracks/{id}` for hint IDs
- Audio mode: `GET /v1/audio-features?ids=...` (batched), `GET /v1/recommendations`
- Auth/session UX: `GET /v1/me`
- User actions: queue + playlist endpoints via user-scoped OAuth client

### External high-volume endpoints in enrichment flow

- Last.fm similarity + tags (`track.getsimilar`, `track.gettoptags`, `artist.gettoptags`, artist fallback endpoints)
- Deezer track lookup/search for preview + ISRC signals
- MusicBrainz recording/ISRC hints for mapping retries
- Song.link/Odesli provider-link lookup for external playback

### Mitigations in code

- Debounced live search in frontend (400ms) with `AbortController` cancellation for stale requests
- Memoized Spotify mapping helpers to avoid repeated deterministic lookups
- Batched Spotify audio-features requests (`ids` batches up to 100)
- Retry/backoff with Retry-After awareness for HTTP providers, but capped wait windows to avoid request hangs
- Per-request enrichment budgets/caps to return degraded results quickly instead of timing out
- Spotipy retries disabled on app and user clients so throttled calls fail fast and use cooldown/degraded paths

### Can we “reset” or rotate Spotify rate limits?

- No clean/safe reset exists from app code.
- Rotating client-credentials tokens does not reliably produce a fresh quota window for the same app.
- Cycling user tokens is not a valid rate-limit bypass and can create instability.
- Practical strategy is request shaping: lower fan-out, cache/batch, honor 429 `Retry-After`, and return degraded responses quickly.

## Local dev without a public URL

Use `https://localhost:8000` with `./setup_certs.sh` and a matching redirect URI in Spotify (localhost is allowed for many apps). If your Spotify app rejects localhost, use a tunneling tool and set `APP_BASE_URL` / `SPOTIFY_REDIRECT_URI` to the tunnel URL, then restart the backend.

## Production HTTPS domain

Point `APP_BASE_URL` and `SPOTIFY_REDIRECT_URI` at your real public URL (e.g. `https://app.cat-id.eu`), add the callback in the Spotify dashboard, and terminate TLS at your host or reverse proxy (see Scaleway section below).

## Simple terminal deployment (Scaleway)

**Slim production path:** LAED Terraform owns the **VM, IP, volume, and cloud-init**. This repo owns **Scaleway DNS** ([`infrastructure/dns/`](infrastructure/dns/)), **production Compose + Caddy** with **registry-only images** ([`infrastructure/deploy/`](infrastructure/deploy/)), and **optional** a dedicated **registry namespace** ([`infrastructure/registry/`](infrastructure/registry/)). Workflow: **push image to Scaleway CR → SSH to LAED host → `docker compose pull && up -d`** from e.g. `/opt/cat-id`. Full steps: [`docs/deploy_scaleway.md`](docs/deploy_scaleway.md).

### Domain + Spotify production wiring

Recommended production hostname:
- `https://app.cat-id.eu`

Set production env vars:
- `APP_BASE_URL=https://app.cat-id.eu`
- `SPOTIFY_REDIRECT_URI=https://app.cat-id.eu/api/spotify/callback`
- `APP_ENV=production`
- `ENABLE_DEBUG_ENDPOINT=false`
- `ALLOWED_ORIGINS=https://app.cat-id.eu`

In Spotify Developer Dashboard, add **exactly** (same scheme, host, path; no trailing slash unless you also set one in env):
- `https://app.cat-id.eu/api/spotify/callback`

The redirect URI must be registered on the **same** Spotify app as `SPOTIFY_CLIENT_ID`. After deploy, confirm what the server sends to Spotify: `curl -sS https://app.cat-id.eu/api/spotify/oauth-config`.

### DNS and deploy runbook

- [docs/deploy_scaleway.md](docs/deploy_scaleway.md)

**Terraform DNS vs runtime env:** Terraform only declares the zone and A/AAAA **record targets** (the **LAED base host’s** public IP). App OAuth keys and `APP_BASE_URL` belong in **`infrastructure/deploy/production.env`** on the server (or **`infrastructure/compose/env/production.env`** for dev-oriented compose), not in `terraform.tfvars`.

To verify that `app.cat-id.eu` points to your expected Scaleway VM IP:

```bash
DOMAIN=cat-id.eu \
APP_HOST=app.cat-id.eu \
EXPECTED_PUBLIC_IPV4=<laed-base-public-ipv4> \
./scripts/dns_preflight.sh
```

## Public validation checklist (web + app clients)

After each deploy:

1. `GET /api/health` returns 200.
2. App homepage loads from `https://app.cat-id.eu`.
3. `/api/similar` and `/api/similar/audio` return expected payloads.
4. Spotify login/callback works on production callback URL.
5. Playlist/queue actions succeed for a logged-in Spotify user.
6. `GET /api/debug/tags` is unavailable in production.

For mobile/web-app clients, use:
- `https://app.cat-id.eu/api/similar`
- `https://app.cat-id.eu/api/similar/audio`

Optional automated smoke test:

```bash
BASE_URL=https://app.cat-id.eu ./scripts/smoke_test.sh
```

## Isolation guardrail (optional)

If this app must not share identifiers with another internal project, keep a separate Scaleway project/DNS state and run:

```bash
./scripts/preflight_isolation_check.sh
```

## Scaleway layout in this repo

- Runbook: [docs/deploy_scaleway.md](docs/deploy_scaleway.md)
- DNS (Terraform): [infrastructure/dns/README.md](infrastructure/dns/README.md)
- **Production** deploy bundle (image-only, SCW CR): [infrastructure/deploy/README.md](infrastructure/deploy/README.md)
- Optional registry namespace TF: [infrastructure/registry/README.md](infrastructure/registry/README.md)
- Dev / local Docker + Caddy: [infrastructure/compose/README.md](infrastructure/compose/README.md)
- Scripts: `./scripts/dns_preflight.sh`, `./scripts/scaleway_dns_apply.sh`, `./scripts/print_spotify_prod_env.sh`, `./scripts/verify_cat_id_deployment.sh`

## GitHub repository (`cat-id`)

Canonical remote: **https://github.com/FelineWeise/cat-id**

The product host is **`app.cat-id.eu`**; the **GitHub** repository name should be **`cat-id`** under **`FelineWeise`** so paths match the brand (clone folder is `cat-id`, default image is `ghcr.io/felineweise/cat-id`).

**Rename an existing repo on GitHub:** open the repository → **Settings → General** → **Repository name** → set to `cat-id` → **Rename**. GitHub keeps a redirect from the old name for a while; update all `git remote` URLs to the new path when you can.

1. **Clone** (HTTPS or SSH):

```bash
git clone https://github.com/FelineWeise/cat-id.git
cd cat-id
```

2. **Point an existing clone at the new URL** (after rename):

```bash
git remote set-url origin https://github.com/FelineWeise/cat-id.git
```

3. **Container image:** default in Compose is **GHCR** `ghcr.io/felineweise/cat-id:latest` (same name as the repo). Override with `CAT_ID_IMAGE` if needed. See [infrastructure/compose/README.md](infrastructure/compose/README.md).

## Ship to production (LAED host, registry images)

1. **Build and push** the image to Scaleway Container Registry (dedicated namespace via [`infrastructure/registry/`](infrastructure/registry/) or under LAED’s namespace—see [docs/deploy_scaleway.md](docs/deploy_scaleway.md)).

2. **SSH** to the **LAED** base host.

3. From the directory holding **`infrastructure/deploy/`** files (e.g. **`/opt/cat-id`**), set **`CAT_ID_IMAGE`** to the new tag/digest and redeploy:

```bash
ssh <user>@<laed-base-ip>
cd /opt/cat-id
export CAT_ID_IMAGE=rg.fr-par.scw.cloud/cat-id/app:v1.0.0
docker compose pull
docker compose up -d
```

Edit **`production.env`** there when OAuth URLs or secrets change; keep it out of git.

**Optional git-based workflow:** you can still clone the repo on the server and use [`infrastructure/compose/compose.stack.yml`](infrastructure/compose/compose.stack.yml) with GHCR or local `build:` for non-production experiments; the recommended **slim** path is **`infrastructure/deploy/`** + CR images only.

---

## Ship code via git (optional, dev-oriented)

1. **Commit and push** your branch (or `main`).

2. **On the host**, update a clone and restart **compose** if you use the repo checkout layout:

```bash
ssh <user>@<server-ip>
cd /opt/cat-id
git pull
docker compose -f infrastructure/compose/compose.stack.yml pull
docker compose -f infrastructure/compose/compose.stack.yml up -d
```

For local builds instead of pulled images, use `up -d --build`.

## Post-launch hardening roadmap

- Move OAuth session storage from in-memory to Redis for multi-instance scaling.
- Add a staging environment (`staging.app.cat-id.eu`) before frequent releases.
- Add CI checks for lint/test/build and optional gated deploy.

## Docker Compose layouts

**Production (Scaleway CR, LAED host):** copy [`infrastructure/deploy/`](infrastructure/deploy/) to e.g. `/opt/cat-id` and run `docker compose` there with **`CAT_ID_IMAGE`** pointing at `rg.<region>.scw.cloud/...` — no `build:` in that file.

**Dev / convenience** under `infrastructure/compose/`:

- `compose.stack.yml` — app + Caddy on 80/443 (default image GHCR; skip Caddy if LAED already uses 80/443)
- `compose.app.yml` — app only (pair with LAED or another reverse proxy)
- `compose.staging.yml` — staging
- `Caddyfile.production` / `Caddyfile.example`
- `env/*.env.example` → gitignored `production.env` / `staging.env`

Details: [infrastructure/deploy/README.md](infrastructure/deploy/README.md), [infrastructure/compose/README.md](infrastructure/compose/README.md).

**Redeploy (deploy bundle):** update `production.env` and `CAT_ID_IMAGE` if needed → `docker compose pull && docker compose up -d` → `./scripts/verify_cat_id_deployment.sh` → optional `./scripts/smoke_test.sh`.

## API

### `POST /api/similar/unified` — Unified Similarity

**Request body:**

```json
{
  "url": "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
  "limit": 10,
  "strict_mapped_only": true,
  "use_metadata_fallback": true
}
```

| Field | Default | Purpose |
|-------|---------|--------|
| `strict_mapped_only` | `false` | If `true`, only return tracks with a Spotify ID (queue/playlist-safe list; may return fewer rows). |
| `use_metadata_fallback` | `true` | On mapping miss, query MusicBrainz for ISRC/title/artist hints and retry Spotify. |

**Response:** JSON with `seed_track` and `similar_tracks`, each containing name, artists, album, album art, Spotify URL, match score, BPM, and tags. Includes `strict_mapped_only` (echo), `mapped_count`, `unmapped_count`, optional `mapping_degraded_reason`, plus diagnostics (`mapping_used_user_token`, `mapping_source_counts`). With strict mode, every `similar_track` has `spotify_id`; `unmapped_count` is always `0` for the returned slice.
For unmapped rows, responses can include `external_links` + `external_primary_provider` plus optional `external_links_degraded_reason` when enrichment is time/rate limited.
`limit` is a maximum; backend overfetches Last.fm when strict so more candidates can be mapped. `total_candidates` is the size of the ranked pool **before** the final `limit` slice and before the strict mapped-only filter—so it can include unmapped rows even when `strict_mapped_only` is `true` (those rows are omitted from `similar_tracks` only).

**Response:** Same top-level shape as previous similarity endpoints (`seed_track`, `similar_tracks`) with unified blended ranking and optional enriched `analysis_metrics`.

## How It Works

1. **Spotify** resolves the pasted URL to track metadata (name, artist, album art)
2. Last.fm and audio-signal candidates are enriched and blended into one ranked list
3. Filters can be passed to backend and refined client-side for remaining criteria
4. **Deezer** provides 30-second audio previews as a fallback

## Tech Stack

- **Backend:** Python, FastAPI, Spotipy, httpx
- **Frontend:** Vanilla HTML / CSS / JS
- **APIs:** Spotify Web API (track lookup, queue, playlist), Last.fm (similarity), Deezer (audio previews), MusicBrainz (optional mapping hints), SoundNet Track Analysis API (enriched metrics)
- **Link Aggregation:** Song.link/Odesli for external provider links when Spotify mapping is unavailable

## License

MIT
