#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/scaleway_dns_apply.sh --config infrastructure/dns/dns_records.json [--dry-run|--apply]

Requirements:
  - scw (Scaleway CLI) installed and authenticated (SCW_ACCESS_KEY + SCW_SECRET_KEY)
  - jq

Notes:
  - This script uses `scw dns record bulk-update` with explicit `changes.{i}.*` args.
  - It will create the apex DNS zone if missing:
      scw dns zone create domain=<dns_zone> subdomain=""

Config format:
  {
    "dns_zone": "cat-id.eu",
    "changes": [
      { "action": "add", "record": { "name": "app", "type": "A", "data": "1.2.3.4", "ttl": 300, "priority": 0 } }
    ]
  }

Supported actions:
  - add
  - set (requires record.id OR id_fields)
  - delete (requires id OR id_fields)
EOF
}

CONFIG_JSON=""
MODE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_JSON="${2:-}"
      shift 2
      ;;
    --dry-run)
      MODE="dry-run"
      shift
      ;;
    --apply)
      MODE="apply"
      shift
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

if [[ -z "${CONFIG_JSON}" || -z "${MODE}" ]]; then
  usage
  exit 2
fi

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

require_command scw
require_command jq

DOMAIN="$(jq -r '.dns_zone' "${CONFIG_JSON}")"
if [[ -z "${DOMAIN}" || "${DOMAIN}" == "null" ]]; then
  echo "Invalid config: missing .dns_zone"
  exit 1
fi

echo "Running DNS preflight..."
DOMAIN="${DOMAIN}" CONFIG_JSON="${CONFIG_JSON}" "${ROOT_DIR}/scripts/dns_preflight.sh"

zone_exists() {
  scw dns zone list domain="${DOMAIN}" -o json 2>/dev/null | jq -e --arg z "${DOMAIN}" 'map(.domain) | index($z) != null' >/dev/null 2>&1
}

ensure_zone() {
  if zone_exists; then
    echo "DNS zone already exists: ${DOMAIN}"
    return 0
  fi

  echo "Creating DNS zone: ${DOMAIN} (apex subdomain=\"\")"
  if [[ "${MODE}" == "dry-run" ]]; then
    echo "DRY RUN: scw dns zone create domain=${DOMAIN} subdomain=\"\""
    return 0
  fi

  scw dns zone create domain="${DOMAIN}" subdomain=""
}

append_add_change_args() {
  # args: change_idx record_idx -> writes into global array `bulk_args`
  local change_idx="$1"
  local record_idx="$2"
  local name type data ttl priority

  name="$(jq -r '.record.name // ""' <<<"${change_json}")"
  type="$(jq -r '.record.type' <<<"${change_json}")"
  data="$(jq -r '.record.data' <<<"${change_json}")"
  ttl="$(jq -r '.record.ttl // 300' <<<"${change_json}")"
  priority="$(jq -r '.record.priority // 0' <<<"${change_json}")"

  bulk_args+=("changes.${change_idx}.add.records.${record_idx}.name=${name}")
  bulk_args+=("changes.${change_idx}.add.records.${record_idx}.type=${type}")
  bulk_args+=("changes.${change_idx}.add.records.${record_idx}.data=${data}")
  bulk_args+=("changes.${change_idx}.add.records.${record_idx}.ttl=${ttl}")
  bulk_args+=("changes.${change_idx}.add.records.${record_idx}.priority=${priority}")
}

append_set_change_args() {
  local change_idx="$1"
  local record_idx="$2"

  local set_id
  set_id="$(jq -r '.set.id // empty' <<<"${change_json}")"
  if [[ -n "${set_id}" ]]; then
    bulk_args+=("changes.${change_idx}.set.id=${set_id}")
  else
    local n t d ttl
    n="$(jq -r '.set.id_fields.name // ""' <<<"${change_json}")"
    t="$(jq -r '.set.id_fields.type' <<<"${change_json}")"
    d="$(jq -r '.set.id_fields.data' <<<"${change_json}")"
    ttl="$(jq -r '.set.id_fields.ttl // empty' <<<"${change_json}")"
    bulk_args+=("changes.${change_idx}.set.id-fields.name=${n}")
    bulk_args+=("changes.${change_idx}.set.id-fields.type=${t}")
    bulk_args+=("changes.${change_idx}.set.id-fields.data=${d}")
    if [[ -n "${ttl}" ]]; then
      bulk_args+=("changes.${change_idx}.set.id-fields.ttl=${ttl}")
    fi
  fi

  local name type data ttl priority
  name="$(jq -r '.record.name // ""' <<<"${change_json}")"
  type="$(jq -r '.record.type' <<<"${change_json}")"
  data="$(jq -r '.record.data' <<<"${change_json}")"
  ttl="$(jq -r '.record.ttl // 300' <<<"${change_json}")"
  priority="$(jq -r '.record.priority // 0' <<<"${change_json}")"

  bulk_args+=("changes.${change_idx}.set.records.${record_idx}.name=${name}")
  bulk_args+=("changes.${change_idx}.set.records.${record_idx}.type=${type}")
  bulk_args+=("changes.${change_idx}.set.records.${record_idx}.data=${data}")
  bulk_args+=("changes.${change_idx}.set.records.${record_idx}.ttl=${ttl}")
  bulk_args+=("changes.${change_idx}.set.records.${record_idx}.priority=${priority}")
}

append_delete_change_args() {
  local change_idx="$1"

  local del_id
  del_id="$(jq -r '.delete.id // empty' <<<"${change_json}")"
  if [[ -n "${del_id}" ]]; then
    bulk_args+=("changes.${change_idx}.delete.id=${del_id}")
    return 0
  fi

  local n t d ttl
  n="$(jq -r '.delete.id_fields.name // ""' <<<"${change_json}")"
  t="$(jq -r '.delete.id_fields.type' <<<"${change_json}")"
  d="$(jq -r '.delete.id_fields.data' <<<"${change_json}")"
  ttl="$(jq -r '.delete.id_fields.ttl // empty' <<<"${change_json}")"
  bulk_args+=("changes.${change_idx}.delete.id-fields.name=${n}")
  bulk_args+=("changes.${change_idx}.delete.id-fields.type=${t}")
  bulk_args+=("changes.${change_idx}.delete.id-fields.data=${d}")
  if [[ -n "${ttl}" ]]; then
    bulk_args+=("changes.${change_idx}.delete.id-fields.ttl=${ttl}")
  fi
}

build_bulk_args() {
  bulk_args=()
  local change_idx=0
  while IFS= read -r line; do
    change_json="${line}"
    local action
    action="$(jq -r '.action' <<<"${change_json}")"

    case "${action}" in
      add)
        append_add_change_args "${change_idx}" 0
        ;;
      set)
        append_set_change_args "${change_idx}" 0
        ;;
      delete)
        append_delete_change_args "${change_idx}"
        ;;
      *)
        echo "Unsupported action: ${action}"
        exit 2
        ;;
    esac

    change_idx=$((change_idx + 1))
  done < <(jq -c '.changes[]' "${CONFIG_JSON}")
}

ensure_zone

change_json=""
bulk_args=()
build_bulk_args

if [[ "${#bulk_args[@]}" -eq 0 ]]; then
  echo "No changes found in config (.changes empty)."
  exit 0
fi

if [[ "${MODE}" == "dry-run" ]]; then
  printf "DRY RUN: scw dns record bulk-update %q" "${DOMAIN}"
  printf ' %q' "${bulk_args[@]}"
  printf "\n"
  exit 0
fi

scw dns record bulk-update "${DOMAIN}" "${bulk_args[@]}"

echo "Done."
