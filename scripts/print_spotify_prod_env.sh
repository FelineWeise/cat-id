#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./scripts/print_spotify_prod_env.sh --public-host app.cat-id.eu

Prints the recommended production environment variables for Spotify OAuth + CORS.

Note:
  - This prints ONLY non-secret values.
  - Keep Spotify secrets in your secret manager / local .env (never commit them).
EOF
}

HOST=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --public-host)
      HOST="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown arg: $1"
      usage
      exit 2
      ;;
  esac
done

if [[ -z "${HOST}" ]]; then
  usage
  exit 2
fi

HOST="${HOST%.}"

cat <<EOF
APP_BASE_URL=https://${HOST}
SPOTIFY_REDIRECT_URI=https://${HOST}/api/spotify/callback
ALLOWED_ORIGINS=https://${HOST}
APP_ENV=production
ENABLE_DEBUG_ENDPOINT=false
EOF
