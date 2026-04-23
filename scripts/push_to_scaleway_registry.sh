#!/usr/bin/env bash
# Build cat-id, log in to Scaleway Container Registry, tag, and push.
# Credentials: copy scripts/scw-registry.env.example → scripts/scw-registry.local.env
# (gitignored) and set SCW_SECRET_KEY, or export variables in your shell.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOCAL_ENV="${SCRIPT_DIR}/scw-registry.local.env"

if [[ -f "${LOCAL_ENV}" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "${LOCAL_ENV}"
  set +a
fi

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_command docker

if [[ -z "${SCW_SECRET_KEY:-}" ]]; then
  echo "Set SCW_SECRET_KEY or create ${LOCAL_ENV} (see scripts/scw-registry.env.example)." >&2
  exit 1
fi

SCW_REGION="${SCW_REGION:-fr-par}"
SCW_REGISTRY_NAMESPACE="${SCW_REGISTRY_NAMESPACE:-cat-id}"
SCW_IMAGE_NAME="${SCW_IMAGE_NAME:-app}"
REGISTRY_HOST="rg.${SCW_REGION}.scw.cloud"

TAG="${1:-${IMAGE_TAG:-}}"
if [[ -z "${TAG}" ]]; then
  TAG="$(date -u +%Y%m%d-%H%M%S)"
  echo "No tag passed; using ${TAG} (pass a tag as first arg or set IMAGE_TAG)."
fi

FULL_IMAGE="${REGISTRY_HOST}/${SCW_REGISTRY_NAMESPACE}/${SCW_IMAGE_NAME}:${TAG}"

echo "Building image..."
docker build -t cat-id:build "${REPO_ROOT}"

echo "Logging in to ${REGISTRY_HOST}..."
printf '%s' "${SCW_SECRET_KEY}" | docker login "${REGISTRY_HOST}" -u nologin --password-stdin

echo "Tagging ${FULL_IMAGE}..."
docker tag cat-id:build "${FULL_IMAGE}"

echo "Pushing ${FULL_IMAGE}..."
docker push "${FULL_IMAGE}"

echo "Done. Use on the server, e.g.:"
echo "  export CAT_ID_IMAGE=${FULL_IMAGE}"
echo "  docker compose pull && docker compose up -d"
