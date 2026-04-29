#!/usr/bin/env bash
# Build/push cat-id image to Scaleway CR and print exact deploy commands.
# Usage:
#   ./scripts/push_to_scaleway_registry.sh [tag]
# Credentials:
#   set SCW_SECRET_KEY or create scripts/scw-registry.local.env (gitignored).
# Shared standard:
#   /Users/feline/Desktop/Repositories/scaleway-push-template.md
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOCAL_ENV="${SCRIPT_DIR}/scw-registry.local.env"
TERRAFORM_DIR="${REPO_ROOT}/infrastructure/registry"

DEFAULT_SCW_REGION="fr-par"
DEFAULT_SCW_NAMESPACE="cat-id"
DEFAULT_IMAGE_NAME="app"
LOCAL_BUILD_TAG="cat-id:build"
DEPLOY_ENV_VAR_NAME="CAT_ID_IMAGE"
DEPLOY_DIR_DEFAULT="/opt/cat-id"
PUSH_LATEST="${PUSH_LATEST:-false}"
TAG_STRATEGY="${TAG_STRATEGY:-timestamp}"
CURRENT_STEP="bootstrap"
USAGE="Usage: ./scripts/push_to_scaleway_registry.sh [tag] [--validate-only]"

on_error() {
  local line="$1"
  echo "ERROR: Step '${CURRENT_STEP}' failed at line ${line}." >&2
}
trap 'on_error "${LINENO}"' ERR

require_command() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "Missing required command: ${cmd}" >&2
    exit 1
  fi
}

is_true() {
  case "$1" in
    1|true|TRUE|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

ensure_docker_ready() {
  CURRENT_STEP="docker-ready-check"
  require_command docker
  if ! docker info >/dev/null 2>&1; then
    echo "Docker daemon is not reachable. Start Docker and retry." >&2
    exit 1
  fi
}

load_local_env() {
  CURRENT_STEP="load-local-env"
  if [[ -f "${LOCAL_ENV}" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "${LOCAL_ENV}"
    set +a
  fi
}

read_terraform_output() {
  local output_name="$1"
  if [[ ! -d "${TERRAFORM_DIR}" || ! -f "${TERRAFORM_DIR}/.terraform.lock.hcl" ]]; then
    return
  fi
  if ! command -v terraform >/dev/null 2>&1; then
    return
  fi
  (
    cd "${TERRAFORM_DIR}" && terraform output -raw "${output_name}" 2>/dev/null
  ) || true
}

resolve_registry_endpoint() {
  local tf_endpoint env_region env_namespace expected_endpoint endpoint_host endpoint_namespace
  env_region="${SCW_REGION:-${DEFAULT_SCW_REGION}}"
  env_namespace="${SCW_REGISTRY_NAMESPACE:-${DEFAULT_SCW_NAMESPACE}}"
  tf_endpoint="$(read_terraform_output registry_endpoint)"

  if [[ -n "${tf_endpoint}" ]]; then
    REGISTRY_ENDPOINT="${tf_endpoint}"
  else
    REGISTRY_ENDPOINT="rg.${env_region}.scw.cloud/${env_namespace}"
  fi

  endpoint_host="${REGISTRY_ENDPOINT%%/*}"
  endpoint_namespace="${REGISTRY_ENDPOINT#*/}"
  if [[ "${endpoint_host}" != rg.*.scw.cloud || -z "${endpoint_namespace}" ]]; then
    echo "Invalid registry endpoint: ${REGISTRY_ENDPOINT}" >&2
    exit 1
  fi

  if [[ -n "${tf_endpoint}" ]]; then
    expected_endpoint="rg.${env_region}.scw.cloud/${env_namespace}"
    if [[ "${expected_endpoint}" != "${tf_endpoint}" ]]; then
      echo "Mismatch between env/default endpoint and Terraform output." >&2
      echo "Expected from env/defaults: ${expected_endpoint}" >&2
      echo "Terraform output:          ${tf_endpoint}" >&2
      exit 1
    fi
  fi
}

resolve_data_dir() {
  DATA_DIR="${DEPLOY_DATA_DIR:-${DEPLOY_DIR_DEFAULT}}"
}

validate_env() {
  CURRENT_STEP="validate-env"
  if [[ -z "${SCW_SECRET_KEY:-}" ]]; then
    echo "Set SCW_SECRET_KEY or create ${LOCAL_ENV} (see scripts/scw-registry.env.example)." >&2
    exit 1
  fi
}

resolve_tag() {
  local arg_tag="${1:-}"
  if [[ -n "${arg_tag}" ]]; then
    printf '%s' "${arg_tag}"
    return
  fi
  if [[ -n "${IMAGE_TAG:-}" ]]; then
    printf '%s' "${IMAGE_TAG}"
    return
  fi
  if [[ "${TAG_STRATEGY}" == "git_sha" ]] && command -v git >/dev/null 2>&1; then
    CURRENT_STEP="resolve-tag-git"
    (
      cd "${REPO_ROOT}" && git rev-parse --short HEAD
    )
    return
  fi
  date -u +%Y%m%d-%H%M%S
}

docker_login() {
  CURRENT_STEP="docker-login"
  local registry_host="${REGISTRY_ENDPOINT%%/*}"
  local registry_namespace="${REGISTRY_ENDPOINT#*/}"
  echo "Logging in to ${registry_host}/${registry_namespace}..."
  printf '%s' "${SCW_SECRET_KEY}" | docker login "${registry_host}/${registry_namespace}" -u nologin --password-stdin >/dev/null
}

build_image() {
  CURRENT_STEP="docker-build"
  echo "Building ${LOCAL_BUILD_TAG} for linux/amd64 (context: ${REPO_ROOT})..."
  docker build --platform linux/amd64 -t "${LOCAL_BUILD_TAG}" "${REPO_ROOT}"
}

tag_and_push() {
  CURRENT_STEP="docker-push"
  local image_repo="$1"
  local image_tag="$2"
  FULL_IMAGE="${image_repo}:${image_tag}"
  echo "Pushing ${FULL_IMAGE}..."
  docker tag "${LOCAL_BUILD_TAG}" "${FULL_IMAGE}"
  docker push "${FULL_IMAGE}"

  if is_true "${PUSH_LATEST}" && [[ "${image_tag}" != "latest" ]]; then
    LATEST_IMAGE="${image_repo}:latest"
    echo "Also pushing ${LATEST_IMAGE}..."
    docker tag "${LOCAL_BUILD_TAG}" "${LATEST_IMAGE}"
    docker push "${LATEST_IMAGE}"
  else
    LATEST_IMAGE=""
  fi
}

resolve_digest() {
  CURRENT_STEP="resolve-digest"
  IMAGE_DIGEST="$(docker image inspect "${FULL_IMAGE}" --format '{{index .RepoDigests 0}}' 2>/dev/null || true)"
  if [[ -z "${IMAGE_DIGEST}" ]]; then
    IMAGE_DIGEST="${FULL_IMAGE}"
  fi
}

print_deploy_commands() {
  cat <<EOF
Use on server:
  cd ${DATA_DIR}
  export ${DEPLOY_ENV_VAR_NAME}=${FULL_IMAGE}
  docker compose pull
  docker compose up -d

With explicit env file:
  cd ${DATA_DIR}
  docker compose --env-file production.env pull
  docker compose --env-file production.env up -d

Recommended immutable reference:
  export ${DEPLOY_ENV_VAR_NAME}=${IMAGE_DIGEST}
EOF
}

print_usage() {
  cat <<EOF
${USAGE}

Options:
  --help           Show this message
  --validate-only  Resolve config and print targets without building/pushing
EOF
}

main() {
  local requested_tag image_name image_repo image_tag validate_only
  requested_tag="${1:-}"
  validate_only="false"

  if [[ "${requested_tag}" == "--help" ]]; then
    print_usage
    exit 0
  fi
  if [[ "${requested_tag}" == "--validate-only" ]]; then
    validate_only="true"
    requested_tag="${2:-}"
  fi

  load_local_env
  validate_env
  resolve_registry_endpoint
  resolve_data_dir

  image_name="${SCW_IMAGE_NAME:-${DEFAULT_IMAGE_NAME}}"
  image_repo="${REGISTRY_ENDPOINT}/${image_name}"
  image_tag="$(resolve_tag "${requested_tag}")"

  if is_true "${validate_only}"; then
    echo "Validation successful."
    echo "Registry endpoint: ${REGISTRY_ENDPOINT}"
    echo "Image repo:        ${image_repo}"
    echo "Tag:               ${image_tag}"
    echo "Deploy data dir:   ${DATA_DIR}"
    exit 0
  fi

  ensure_docker_ready
  build_image
  docker_login
  tag_and_push "${image_repo}" "${image_tag}"
  resolve_digest

  echo "Push complete."
  echo "Image tag:    ${FULL_IMAGE}"
  if [[ -n "${LATEST_IMAGE}" ]]; then
    echo "Latest tag:   ${LATEST_IMAGE}"
  fi
  echo "Digest:       ${IMAGE_DIGEST}"
  echo
  print_deploy_commands
}

main "$@"
