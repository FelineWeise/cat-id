#!/usr/bin/env bash
# Remote entrypoint for GitHub Deploy. Run on the server from DEPLOY_PATH.
# Usage (on server):
#   bash scripts/ci_deploy_remote.sh
# Expects in the current directory:
#   docker-compose.yml, image.env, production.env
set -euo pipefail

DEPLOY_PATH="$(pwd)"
COMPOSE=(docker compose --env-file production.env --env-file image.env)

if [[ ! -f "${DEPLOY_PATH}/docker-compose.yml" ]]; then
  echo "Missing docker-compose.yml in ${DEPLOY_PATH}" >&2
  exit 1
fi
if [[ ! -f "${DEPLOY_PATH}/image.env" ]]; then
  echo "Missing image.env in ${DEPLOY_PATH}" >&2
  exit 1
fi
if [[ ! -f "${DEPLOY_PATH}/production.env" ]]; then
  echo "Missing production.env in ${DEPLOY_PATH}" >&2
  exit 1
fi

# image.env must win for CAT_ID_IMAGE (loaded last above). Warn if production.env still pins.
if grep -qE '^CAT_ID_IMAGE=' "${DEPLOY_PATH}/production.env"; then
  echo "WARN: production.env still sets CAT_ID_IMAGE; image.env overrides it for this deploy."
  echo "      Remove CAT_ID_IMAGE from production.env when convenient so the pin lives in one place."
fi

echo "Deploying from ${DEPLOY_PATH}"
grep -E '^CAT_ID_IMAGE=' "${DEPLOY_PATH}/image.env" || {
  echo "image.env missing CAT_ID_IMAGE" >&2
  exit 1
}

"${COMPOSE[@]}" pull cat-id
"${COMPOSE[@]}" up -d --force-recreate --remove-orphans cat-id
"${COMPOSE[@]}" ps cat-id

echo "Remote deploy steps complete."
