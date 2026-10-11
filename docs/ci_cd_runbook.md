# cat-id CI/CD runbook

Happy path for shipping to production (`app.cat-id.eu` on the LAED base host).

Full design: [`ci_cd_migration_plan.md`](./ci_cd_migration_plan.md).

## Ship a normal change

1. Open a PR → wait for **CI → Build image**.
2. Merge to `main` → wait for **CI → Build, push, and pin** (updates `infrastructure/deploy/image.env`).
3. GitHub → **Actions** → **Deploy** → **Run workflow**.
   - `git_ref`: `main` (default)
   - `image_ref`: leave empty (use pinned `image.env`)
   - `run_smoke`: `true` (default)
4. Watch the Deploy run until verify (and smoke) pass.

The server is **not** updated by merges alone — only by **Deploy**.

## Rollback

1. Find a prior pin: `git log -- infrastructure/deploy/image.env`
2. Actions → **Deploy** → **Run workflow** with `image_ref` set to that digest  
   **or** revert the pin commit on `main`, then Run **Deploy** with empty `image_ref`.

## Emergency laptop path

```bash
./scripts/push_to_scaleway_registry.sh sha-manual
# then SSH and apply, or Run Deploy with image_ref=<pushed digest>
```

Afterward, reconcile `infrastructure/deploy/image.env` on `main` so the next Deploy does not overwrite an undocumented image.

## Preconditions (once)

- GitHub Environment **`production`** has portable `REGISTRY_*` (CI) and `DEPLOY_*` (Deploy) vars/secrets.
- Server `/opt/cat-id/production.env` exists and is **not** managed by CI.
- Prefer `CAT_ID_IMAGE` only in `image.env` (not in `production.env`).
- Host can `docker pull` from the registry (LAED docker auth / root config).
