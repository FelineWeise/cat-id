#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${DOMAIN:-cat-id.eu}"
APP_HOST="${APP_HOST:-app.${DOMAIN}}"
CONFIG_JSON="${CONFIG_JSON:-}"
EXPECTED_PUBLIC_IPV4="${EXPECTED_PUBLIC_IPV4:-}"
EXPECTED_PUBLIC_IPV6="${EXPECTED_PUBLIC_IPV6:-}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

echo "DNS preflight"
echo "Domain: ${DOMAIN}"

require_command dig

if [[ -n "${CONFIG_JSON}" ]]; then
  require_command jq
  echo "Validating JSON config: ${CONFIG_JSON}"
  jq -e '.dns_zone and (.changes|type=="array")' "${CONFIG_JSON}" >/dev/null
fi

echo "Checking Scaleway auth env vars..."
if [[ -z "${SCW_SECRET_KEY:-}" ]]; then
  echo "Warning: SCW_SECRET_KEY is not set. Export it before running Scaleway DNS mutations (scw/curl/terraform apply)."
else
  echo "SCW_SECRET_KEY is set."
fi

if [[ -z "${SCW_ACCESS_KEY:-}" ]]; then
  echo "Warning: SCW_ACCESS_KEY is not set. Prefer setting both SCW_ACCESS_KEY and SCW_SECRET_KEY for Scaleway CLI/API calls."
else
  echo "SCW_ACCESS_KEY is set."
fi

echo "Checking authoritative nameservers for ${DOMAIN} ..."
ns_lines="$(dig +short NS "${DOMAIN}" || true)"
if [[ -z "${ns_lines}" ]]; then
  echo "No NS records returned (domain may be unregistered or not delegated yet)."
else
  echo "${ns_lines}" | sed 's/^/  - /'
  if echo "${ns_lines}" | grep -qiE 'scw-dns|dom\.scw\.cloud'; then
    echo "Looks delegated to Scaleway DNS (Scaleway nameserver hostname detected)."
  else
    echo "Nameservers do not look like Scaleway defaults yet."
    echo "If you intend Scaleway to be authoritative, update registrar NS delegation to the values shown by:"
    echo "  terraform output nameservers"
    echo "or:"
    echo "  scw dns zone list"
  fi
fi

echo "Checking DNS records for ${APP_HOST} ..."
current_ipv4="$(dig +short A "${APP_HOST}" || true)"
current_ipv6="$(dig +short AAAA "${APP_HOST}" || true)"

if [[ -z "${current_ipv4}" ]]; then
  echo "No A records found for ${APP_HOST}."
else
  echo "A records:"
  echo "${current_ipv4}" | sed 's/^/  - /'
fi

if [[ -z "${current_ipv6}" ]]; then
  echo "No AAAA records found for ${APP_HOST}."
else
  echo "AAAA records:"
  echo "${current_ipv6}" | sed 's/^/  - /'
fi

if [[ -n "${EXPECTED_PUBLIC_IPV4}" ]]; then
  if echo "${current_ipv4}" | rg -qx "${EXPECTED_PUBLIC_IPV4}"; then
    echo "A record match confirmed for expected IPv4: ${EXPECTED_PUBLIC_IPV4}"
  else
    echo "Expected IPv4 ${EXPECTED_PUBLIC_IPV4} not found in current A records."
    exit 1
  fi
fi

if [[ -n "${EXPECTED_PUBLIC_IPV6}" ]]; then
  if echo "${current_ipv6}" | rg -qx "${EXPECTED_PUBLIC_IPV6}"; then
    echo "AAAA record match confirmed for expected IPv6: ${EXPECTED_PUBLIC_IPV6}"
  else
    echo "Expected IPv6 ${EXPECTED_PUBLIC_IPV6} not found in current AAAA records."
    exit 1
  fi
fi

echo "Checking for accidental LAED references in DNS automation inputs..."
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v rg >/dev/null 2>&1; then
  matches="$(
    rg -n "laed" "${ROOT_DIR}/infrastructure/dns" "${ROOT_DIR}/scripts" \
      --glob '!scripts/dns_preflight.sh' \
      --glob '!scripts/preflight_isolation_check.sh' \
      || true
  )"
else
  matches="$(
    grep -RIn "laed" "${ROOT_DIR}/infrastructure/dns" "${ROOT_DIR}/scripts" 2>/dev/null \
      | grep -v "scripts/dns_preflight.sh" \
      | grep -v "scripts/preflight_isolation_check.sh" \
      || true
  )"
fi

if [[ -n "${matches}" ]]; then
  echo "Preflight failed: found blocked token references:"
  echo "${matches}"
  exit 1
fi

echo "DNS preflight completed."
