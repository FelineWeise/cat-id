# cat-id CI/CD migration plan

Pilot for the LAED-stack Scaleway → portable OCI/CD path.  
**Decision set (locked):** Approach **2 hybrid**, SSH apply from GitHub Actions, **portable secret names**, user-facing deploy action named **Deploy** (not “workflow_dispatch”).

Related existing docs: [`deploy_scaleway.md`](./deploy_scaleway.md), [`../infrastructure/deploy/README.md`](../infrastructure/deploy/README.md), repo-root `scaleway-push-template.md` (CI/CD migration notes).

---

## 1. Goals

1. Replace Mac-only `./scripts/push_to_scaleway_registry.sh` as the **primary** release path with GitHub Actions.
2. Keep the server contract unchanged: **pull → up -d → verify** under `/opt/cat-id`.
3. **Build** updates the registry (and a tracked image pin). **Deploy** is a separate, intentional pipeline action.
4. Name that action **Deploy** everywhere operators look (Actions tab, docs, runbooks). Do not teach the process as “workflow_dispatch.”
5. Use **portable** secret/variable names so Scaleway can later become STACKIT (or another OCI registry + VM) without rewriting workflows.
6. Keep `production.env` (Spotify/Last.fm secrets) **only on the server** — never in CI.

Non-goals for this pilot: migrating laed/openclaw/llm-gateway; re-platforming off docker compose; Scaleway-native Serverless/Kapsule.

---

## 2. Target shape

```text
PR opened/updated
  → CI workflow: lint/build-check (no registry push, no server touch)

Push / merge to main
  → CI workflow: build linux/amd64 → push sha-<short> → resolve digest
  → update tracked pin: infrastructure/deploy/image.env
  → server NOT updated

Operator runs "Deploy" (Actions → Deploy → Run workflow)
  → Deploy workflow (Environment: production)
  → SSH to deploy host
  → sync pin + compose (not production.env)
  → docker compose pull && up -d
  → verify_cat_id_deployment.sh (+ optional smoke_test.sh)
```

### Why hybrid

| Stage | Automatic? | Why |
|-------|------------|-----|
| Build + pin on `main` | Yes | Every merge produces a known, pullable image and an auditable pin in git |
| Deploy to `/opt/cat-id` | **Only via Deploy** | Public repo; avoid “docs typo on main = prod restart”; matches intentional ship |

---

## 3. Naming contract

| Thing | Name | Notes |
|-------|------|--------|
| Build/CI workflow file | `.github/workflows/ci.yml` | Display name: **CI** |
| Deploy workflow file | `.github/workflows/deploy.yml` | Display name: **Deploy** |
| GitHub Environment | `production` | Holds deploy + registry secrets; optional required reviewer later |
| Image pin file | `infrastructure/deploy/image.env` | Committed; non-secret; only `CAT_ID_IMAGE=…` |
| Operator docs language | “Run **Deploy**” | Never “run workflow_dispatch” |

Implementation detail (do not surface in runbooks): GitHub’s event key for a manual **Deploy** run is still `workflow_dispatch` in YAML. The **workflow `name:` is `Deploy`**, so the Actions UI shows a **Deploy** button. Inputs are labeled for operators (`image_digest` optional override, `run_smoke` boolean, etc.).

---

## 4. Triggers

### CI (`.github/workflows/ci.yml`)

| Git event | Registry push? | Update `image.env`? | Server? |
|-----------|----------------|---------------------|---------|
| `pull_request` → `main` | No | No | No |
| `push` → `main` | Yes (`sha-<short>` + digest) | Yes | No |

PR job: `docker build --platform linux/amd64` (and later Poetry checks if/when tests exist).  
`main` job: build → login → push → write pin → commit pin back to `main` *or* open a short-lived bot commit (see §7).

### Deploy (`.github/workflows/deploy.yml`)

| How operators start it | Server updated? |
|------------------------|-----------------|
| Actions tab → **Deploy** → **Run workflow** | Yes |
| CLI: `gh workflow run Deploy …` (optional) | Yes |
| Automatic on push to `main` | **No** |

Optional Deploy inputs:

| Input | Default | Purpose |
|-------|---------|---------|
| `image_ref` | empty → use `infrastructure/deploy/image.env` from the chosen ref | Override pin (rollback / hotfix) |
| `run_smoke` | `true` | Run `scripts/smoke_test.sh` after verify |
| `git_ref` | `main` | Branch/tag whose pin + compose to apply |

Deploy runs only in Environment **`production`**, and only from the default branch (or tags you explicitly allow) — never from fork PRs.

---

## 5. Portable configuration

### Variables (non-secret) — repo or Environment `production`

| Name | Current cat-id value | Role |
|------|----------------------|------|
| `REGISTRY_HOST` | `rg.fr-par.scw.cloud` | OCI registry host |
| `REGISTRY_NAMESPACE` | `cat-id` | Namespace / path prefix |
| `REGISTRY_IMAGE` | `app` | Repository name under namespace |
| `DEPLOY_HOST` | `51.15.207.5` (scw-base) | SSH target |
| `DEPLOY_USER` | `feline` (or dedicated deploy user) | SSH user |
| `DEPLOY_PATH` | `/opt/cat-id` | Server data dir |
| `APP_BASE_URL` | `https://app.cat-id.eu` | Verify/smoke base |
| `APP_HOST` | `app.cat-id.eu` | DNS check in verify |
| `EXPECTED_IPV4` | LAED flexible IP | Optional verify pin |

Image ref constructed as:

```text
${REGISTRY_HOST}/${REGISTRY_NAMESPACE}/${REGISTRY_IMAGE}
```

Pin prefers digest form:

```text
CAT_ID_IMAGE=rg.fr-par.scw.cloud/cat-id/app@sha256:<digest>
```

### Secrets — Environment `production` (prefer over naked repo secrets)

| Name | Current Scaleway mapping | Role |
|------|--------------------------|------|
| `REGISTRY_USERNAME` | `nologin` | `docker login` user |
| `REGISTRY_PASSWORD` | `SCW_SECRET_KEY` | `docker login` password |
| `DEPLOY_SSH_KEY` | **New dedicated deploy key** (not personal `id_ed25519-scaleway` long-term) | SSH private key |

Scaleway-specific names (`SCW_*`) may remain in **local** `scripts/scw-registry.local.env` for the Mac escape-hatch script. CI must not require `SCW_*`.

### Local escape hatch (kept)

`scripts/push_to_scaleway_registry.sh` stays for emergencies / laptop builds. Prefer aligning its default `TAG_STRATEGY` to `git_sha` over time. It is no longer the happy path once Deploy is live.

---

## 6. Files to add or change

### Add

| Path | Purpose |
|------|---------|
| `.github/workflows/ci.yml` | PR build-check; `main` build/push/pin |
| `.github/workflows/deploy.yml` | **Deploy** — SSH apply + verify (+ smoke) |
| `infrastructure/deploy/image.env` | Tracked pin (`CAT_ID_IMAGE=…`) |
| `docs/ci_cd_runbook.md` | Operator steps: merge → Run Deploy → read logs |
| `scripts/ci_deploy_remote.sh` (optional) | Single remote script: apply pin, compose up, verify — invoked over SSH for one clear entrypoint |

### Change

| Path | Change |
|------|--------|
| `infrastructure/deploy/docker-compose.yml` | Keep `${CAT_ID_IMAGE}`; remove reliance on `:latest` as the prod happy path (default can stay as fallback for empty env only) |
| `infrastructure/deploy/README.md` | Document pin file + “ship via GitHub → Deploy” |
| `docs/deploy_scaleway.md` | Point primary path at CI/CD; Mac push = fallback |
| `infrastructure/deploy/production.env.example` | Clarify: image pin lives in `image.env`, secrets stay in `production.env` |
| `.gitignore` | Ensure `production.env` stays ignored; `image.env` is tracked |

### Do not put in git / CI

- `production.env` (Spotify/Last.fm secrets)
- `scripts/scw-registry.local.env`
- Personal SSH keys
- Terraform state

---

## 7. Workflow responsibilities (detail)

### CI on `main` — build job

1. Checkout at the merge SHA.
2. `docker build --pull --platform linux/amd64 -t cat-id:build .`
3. Login: `docker login "$REGISTRY_HOST/$REGISTRY_NAMESPACE" -u "$REGISTRY_USERNAME" --password-stdin` <<< `"$REGISTRY_PASSWORD"`.
4. Tag/push: `$REGISTRY_HOST/$REGISTRY_NAMESPACE/$REGISTRY_IMAGE:sha-<short>`.
5. Resolve digest (immutable): `…/app@sha256:…`.
6. Write `infrastructure/deploy/image.env`:
   ```bash
   CAT_ID_IMAGE=rg.fr-par.scw.cloud/cat-id/app@sha256:…
   ```
7. Commit + push pin to `main` with a bot-style message, e.g. `chore(deploy): pin image sha-<short>`, using `github-actions[bot]` / `contents: write`.  
   **Guard:** skip pin commit if the only change on the commit is already an identical pin (avoid loops).

Alternative if pin-commits feel noisy: upload digest as a workflow artifact and have Deploy read “latest successful CI on main.” Prefer the tracked `image.env` for audit/rollback clarity in this pilot.

### Deploy — apply job

1. Checkout `git_ref` (default `main`).
2. Resolve image: input `image_ref` if set, else contents of `infrastructure/deploy/image.env`.
3. Configure SSH (`DEPLOY_SSH_KEY`, host key handling via known_hosts or `ssh-keyscan` pinned to `EXPECTED_IPV4` / `DEPLOY_HOST`).
4. Sync **only** runtime manifests that are safe:
   - `infrastructure/deploy/docker-compose.yml` → `$DEPLOY_PATH/docker-compose.yml`
   - `infrastructure/deploy/image.env` → `$DEPLOY_PATH/image.env`
   - Optional: `Caddyfile` only if standalone-tls profile is in use (default shared-LAED layout: skip)
   - **Never** overwrite `$DEPLOY_PATH/production.env` from CI
5. Remote (`scripts/ci_deploy_remote.sh`):

   ```bash
   cd "$DEPLOY_PATH"
   docker compose --env-file production.env --env-file image.env pull cat-id
   docker compose --env-file production.env --env-file image.env up -d --force-recreate cat-id
   ```

6. From the runner (public HTTPS):  
   `EXPECTED_IPV4=… BASE_URL=… ./scripts/verify_cat_id_deployment.sh`  
   and if `run_smoke`: `./scripts/smoke_test.sh`.
7. Fail the Deploy run on non-zero verify/smoke (no “success” on unhealthy prod).

Registry login on the **server** remains whatever LAED cloud-init already configured (`/root/.docker/config.json` or equivalent). Deploy assumes pull credentials already work on the host (document as precondition). If not, add a one-time server bootstrap step — not every Deploy.

---

## 8. Phased implementation

### Phase 0 — Preconditions (manual, once)

1. Confirm SSH to scw-base and `/opt/cat-id` layout (`docker-compose.yml`, `production.env`, running container).
2. Confirm server can `docker pull` from `rg.fr-par.scw.cloud/cat-id` without CI.
3. Create a **dedicated** SSH keypair for GitHub → server; authorize public key on deploy user; restrict to deploy needs where practical.
4. In GitHub repo settings:
   - Environment **`production`**
   - Variables + secrets from §5
   - (Recommended) branch protection on `main`: PR required, CI green required
5. Seed `infrastructure/deploy/image.env` with the **currently running** prod digest/tag so first Deploy is a no-op or controlled bump.

### Phase 1 — CI build/push/pin (no server automation yet)

1. Add `.github/workflows/ci.yml`. ✅
2. Add tracked `image.env` (seeded from live `/opt/cat-id` digest). ✅
3. GitHub Environment `production` with portable `REGISTRY_*` vars/secrets. ✅ (created during Phase 1 impl)
4. Merge Phase 1 PR; confirm PR **Build image** job works.
5. Merge to `main`; confirm **Build, push, and pin** pushes to Scaleway CR and pin commit lands (`[skip ci]`).
6. Keep using Mac/manual server apply until Phase 2 is trusted.

### Phase 2 — Deploy workflow

1. Add `.github/workflows/deploy.yml` with `name: Deploy`. ✅
2. Add short `docs/ci_cd_runbook.md`: “To ship: merge to main → Actions → **Deploy** → Run workflow.” ✅
3. Add `scripts/ci_deploy_remote.sh` remote entrypoint. ✅
4. Dry-run: Run **Deploy** with current pin (recreate + verify/smoke green).
5. Update `docs/deploy_scaleway.md` + deploy README: primary path = CI + Deploy; Mac script = fallback. ✅

### Phase 3 — Harden + template for other repos

1. Optional: require reviewer on Environment `production`.
2. Align local push script defaults (`TAG_STRATEGY=git_sha`) with CI tags.
3. Extract a short “portable deploy” checklist for cloning into **laed** (multi-image) and **openclaw** (needs git remote first).
4. Grep agent rules / `scaleway-push-template.md` for `TRANSITIONAL: push-to-scaleway` and update when cat-id is happy-path on CI.

---

## 9. Operator runbook (target UX)

### Ship a normal change

1. Open PR → wait for **CI** (build-check).
2. Merge to `main` → wait for **CI** (push + pin).
3. GitHub → **Actions** → **Deploy** → **Run workflow** (leave `image_ref` empty).
4. Watch Deploy logs until verify (and smoke) pass.
5. Smoke manually in browser if desired: `https://app.cat-id.eu`.

### Rollback

1. Identify prior good pin from `git log -- infrastructure/deploy/image.env`.
2. Actions → **Deploy** → Run workflow with `image_ref` set to that digest **or** revert the pin commit on `main` then Run **Deploy**.
3. Confirm verify/smoke green.

### Emergency laptop path

1. `./scripts/push_to_scaleway_registry.sh sha-<manual>` (local env with registry password).
2. SSH to host; set `CAT_ID_IMAGE` / update `image.env`; compose pull/up.
3. Afterward, reconcile git pin so the next Deploy does not stomp an undocumented image.

---

## 10. Security notes (public repo)

- cat-id is **public**. Deploy secrets live in Environment `production`, not exposed to `pull_request` from forks.
- **Deploy** must use `environment: production` and refuse to run on fork workflows.
- Do not echo registry password or SSH key in logs.
- Prefer a deploy-only SSH key; rotate if it ever lived in a personal laptop backup that was shared.
- `production.env` never synced from CI.

---

## 11. Success criteria

- [ ] Merge to `main` produces a Scaleway CR image tagged `sha-<short>` and an updated `infrastructure/deploy/image.env` without touching the server.
- [ ] Actions UI shows a workflow named **Deploy**; running it is the only normal way to change prod.
- [ ] Deploy applies pin over SSH, recreates `cat-id`, and fails if `verify_cat_id_deployment.sh` fails.
- [ ] No `SCW_*` required in GitHub Actions (only portable `REGISTRY_*` / `DEPLOY_*`).
- [ ] `production.env` never appears in the repo or in Actions logs.
- [ ] Docs describe Mac push as fallback only.
- [ ] One successful rollback drill via Deploy + prior digest.

---

## 12. Follow-up (out of scope, sequenced after pilot)

| Repo | Extra work before same pattern |
|------|--------------------------------|
| **laed** | Two images (backend + frontend); shared tag; private repo (simpler secret model) |
| **openclaw** | Initialize git + private GitHub remote; scrub local `terraform.tfstate` before first push; heavier scp sync set |
| **llm-gateway** | Same git bootstrap; start-order awareness (`gateway → laed → openclaw`) |

Pattern to copy: **CI builds/pins → Deploy applies over SSH → portable secrets → verify script is the gate.**

---

## 13. Open points (resolve during Phase 0–1)

1. **Pin commit vs artifact-only:** default = commit `image.env` (audit/rollback). Switch to artifact-only if pin commits are too noisy.
2. **Deploy user:** keep `feline` for pilot vs create `deploy` user with limited sudo/docker — prefer dedicated user before laed reuse.
3. **Smoke in Deploy:** default on; allow input `run_smoke=false` for infra-only recreates.
4. **Compose env-file order:** `--env-file production.env --env-file image.env` so the tracked pin always wins for `CAT_ID_IMAGE`; keep secrets only in `production.env`.

---

## 14. One-line summary

**Merge builds and pins; Run Deploy ships.** Scaleway is only the current registry/VM behind portable `REGISTRY_*` and `DEPLOY_*` settings.
