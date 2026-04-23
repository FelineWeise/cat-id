#!/usr/bin/env bash
set -euo pipefail

# Post-deploy checks for app.cat-id.eu (public DNS + HTTPS health).
# Usage:
#   EXPECTED_IPV4=51.15.207.5 BASE_URL=https://app.cat-id.eu ./scripts/verify_cat_id_deployment.sh

BASE_URL="${BASE_URL:-https://app.cat-id.eu}"
EXPECTED_IPV4="${EXPECTED_IPV4:-}"
APP_HOST="${APP_HOST:-app.cat-id.eu}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

require_command dig
require_command curl

echo "Checking A record for ${APP_HOST} ..."
ipv4="$(dig +short A "${APP_HOST}" | head -1 || true)"
if [[ -z "${ipv4}" ]]; then
  echo "No A record returned for ${APP_HOST}."
  exit 1
fi
echo "  A record: ${ipv4}"

if [[ -n "${EXPECTED_IPV4}" && "${ipv4}" != "${EXPECTED_IPV4}" ]]; then
  echo "Expected A record ${EXPECTED_IPV4}, got ${ipv4}."
  exit 1
fi

health_url="${BASE_URL%/}/api/health"
echo "Checking ${health_url} ..."
curl -fsS "${health_url}" >/dev/null
echo "Health OK."

echo "Verify Spotify Developer Dashboard redirect URI matches:"
echo "  ${BASE_URL%/}/api/spotify/callback"
echo "All checks passed."
