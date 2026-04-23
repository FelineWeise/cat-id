#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-https://app.cat-id.eu}"
SEED_TRACK_URL="${SEED_TRACK_URL:-https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT}"
CHECK_AUDIO="${CHECK_AUDIO:-1}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

post_json() {
  local url="$1"
  local payload="$2"
  curl -fsS -X POST "${url}" \
    -H "Content-Type: application/json" \
    -d "${payload}"
}

require_command curl

echo "Smoke test target: ${BASE_URL}"
echo "Checking health endpoint..."
curl -fsS "${BASE_URL%/}/api/health" >/dev/null
echo "Health endpoint OK"

echo "Checking /api/similar..."
post_json "${BASE_URL%/}/api/similar" "{\"url\":\"${SEED_TRACK_URL}\",\"limit\":5}" >/dev/null
echo "/api/similar OK"

if [[ "${CHECK_AUDIO}" == "1" ]]; then
  echo "Checking /api/similar/audio..."
  post_json "${BASE_URL%/}/api/similar/audio" "{\"url\":\"${SEED_TRACK_URL}\",\"limit\":5,\"weights\":{\"tempo\":0.8,\"energy\":0.5,\"valence\":0.5,\"danceability\":0.4,\"acousticness\":0.3,\"instrumentalness\":0.2}}" >/dev/null
  echo "/api/similar/audio OK"
fi

echo "Smoke test completed successfully."
