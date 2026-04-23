#!/usr/bin/env bash
set -euo pipefail

if ! command -v mkcert >/dev/null 2>&1; then
  echo "mkcert is required. Install with:"
  echo "  brew install mkcert nss"
  exit 1
fi

mkcert -install
mkcert -key-file key.pem -cert-file cert.pem localhost 127.0.0.1

echo "Generated key.pem and cert.pem in project root."
echo "Start app with: poetry run python run.py"
