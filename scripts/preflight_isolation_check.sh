#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BLOCKED_PATTERN="${BLOCKED_PATTERN:-laed}"

echo "Running isolation preflight in ${ROOT_DIR}"
echo "Blocked token: ${BLOCKED_PATTERN}"

if command -v rg >/dev/null 2>&1; then
  matches="$(
    rg -n "${BLOCKED_PATTERN}" "${ROOT_DIR}" \
      --glob 'backend/**' \
      --glob 'scripts/**' \
      --glob '.env.example' \
      --glob 'Dockerfile' \
      --glob '!scripts/preflight_isolation_check.sh' \
      --glob '!scripts/dns_preflight.sh' || true
  )"
else
  candidates=(
    "${ROOT_DIR}/.env.example"
    "${ROOT_DIR}/Dockerfile"
  )
  while IFS= read -r file_path; do
    candidates+=("${file_path}")
  done < <(find "${ROOT_DIR}/backend" "${ROOT_DIR}/scripts" -type f)

  matches=""
  for file_path in "${candidates[@]}"; do
    [[ "${file_path}" == *"scripts/preflight_isolation_check.sh" ]] && continue
    [[ "${file_path}" == *"scripts/dns_preflight.sh" ]] && continue
    [[ -f "${file_path}" ]] || continue
    while IFS= read -r line; do
      matches+="${line}"$'\n'
    done < <(grep -in "${BLOCKED_PATTERN}" "${file_path}" || true)
  done
fi

if [[ -n "${matches}" ]]; then
  echo "Isolation preflight failed. Found blocked references:"
  echo "${matches}"
  exit 1
fi

echo "Isolation preflight passed."
