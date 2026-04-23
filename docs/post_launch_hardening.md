# Post-launch Hardening Roadmap

This roadmap captures reliability improvements after first public release.

## 1) Session durability (highest priority)

- Replace in-memory Spotify session store in `backend/main.py` with Redis-backed session storage.
- Store only short-lived access state in memory; keep refresh token metadata in Redis.
- Add TTL-based cleanup and explicit logout invalidation.

## 2) Environment segmentation

- Add separate environments:
  - `dev`
  - `staging`
  - `production`
- Use distinct domains and secrets per environment.
- Keep `ENABLE_DEBUG_ENDPOINT=false` in staging and production.

## 3) Release safety checks

- Keep `scripts/preflight_isolation_check.sh` as mandatory before deploy.
- Run `scripts/smoke_test.sh` against target domain after each deploy.
- Add a rollback runbook (re-deploy previous image) for failed smoke tests.

## 4) Optional CI/CD introduction (later)

- Add lightweight checks first (lint/test/build).
- Keep deploy trigger manual initially.
- Add protected production deploy only after staging is stable.

## 5) Observability and operations

- Add request/error metrics and structured logs.
- Track endpoint latency and external API failures (Spotify/Last.fm/Deezer).
- Define alerts for:
  - health endpoint failures
  - OAuth callback failure spike
  - elevated 5xx rates
