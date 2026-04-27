#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-https://app.cat-id.eu}"
SEED_TRACK_URL="${SEED_TRACK_URL:-https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT}"
CHECK_AUDIO="${CHECK_AUDIO:-1}"
MAX_REQUEST_SECONDS="${MAX_REQUEST_SECONDS:-20}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

post_json() {
  local url="$1"
  local payload="$2"
  curl -fsS --retry 2 --retry-delay 1 --max-time "${MAX_REQUEST_SECONDS}" -X POST "${url}" \
    -H "Content-Type: application/json" \
    -d "${payload}"
}

require_command curl
require_command python3

echo "Smoke test target: ${BASE_URL}"
echo "Checking health endpoint..."
curl -fsS "${BASE_URL%/}/api/health" >/dev/null
echo "Health endpoint OK"

echo "Checking /api/similar..."
post_json "${BASE_URL%/}/api/similar" "{\"url\":\"${SEED_TRACK_URL}\",\"limit\":5,\"strict_mapped_only\":true,\"use_metadata_fallback\":true}" >/tmp/cat_id_similar.json
python3 - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("/tmp/cat_id_similar.json").read_text())
if "seed_track" not in payload or "similar_tracks" not in payload:
    raise SystemExit("/api/similar payload missing required keys")
# When the server supports strict mapping (this repo), enforce queue-safe rows.
if "strict_mapped_only" in payload:
    if payload.get("strict_mapped_only") is not True:
        raise SystemExit("/api/similar strict_mapped_only must be true for this smoke payload")
    for i, t in enumerate(payload["similar_tracks"]):
        if not t.get("spotify_id"):
            raise SystemExit(f"/api/similar strict_mapped_only but track {i} missing spotify_id")
    mc = payload.get("mapped_count", 0)
    uc = payload.get("unmapped_count", -1)
    if uc != 0:
        raise SystemExit(f"/api/similar strict_mapped_only but unmapped_count={uc}")
    if mc != len(payload["similar_tracks"]):
        raise SystemExit("/api/similar mapped_count must equal len(similar_tracks) in strict mode")
PY
echo "/api/similar OK"

if [[ "${CHECK_AUDIO}" == "1" ]]; then
  echo "Checking /api/similar/audio..."
  post_json "${BASE_URL%/}/api/similar/audio" "{\"url\":\"${SEED_TRACK_URL}\",\"limit\":5,\"strict_mapped_only\":true,\"use_metadata_fallback\":true,\"weights\":{\"tempo\":0.8,\"energy\":0.5,\"valence\":0.5,\"danceability\":0.4,\"acousticness\":0.3,\"instrumentalness\":0.2}}" >/tmp/cat_id_audio.json
  python3 - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("/tmp/cat_id_audio.json").read_text())
if "seed_track" not in payload or "similar_tracks" not in payload:
    raise SystemExit("/api/similar/audio payload missing required keys")
if "strict_mapped_only" in payload:
    if payload.get("strict_mapped_only") is not True:
        raise SystemExit("/api/similar/audio strict_mapped_only must be true for this smoke payload")
    for i, t in enumerate(payload["similar_tracks"]):
        if not t.get("spotify_id"):
            raise SystemExit(f"/api/similar/audio strict_mapped_only but track {i} missing spotify_id")
    if payload.get("unmapped_count", -1) != 0:
        raise SystemExit("/api/similar/audio strict_mapped_only but unmapped_count != 0")
    if payload.get("mapped_count", -1) != len(payload["similar_tracks"]):
        raise SystemExit("/api/similar/audio mapped_count must equal len(similar_tracks) in strict mode")
PY
  echo "/api/similar/audio OK"
fi

echo "Smoke test completed successfully."
