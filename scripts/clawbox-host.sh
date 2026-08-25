#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
SYSTEM_NAMESPACE="clawbox-system"
TASK_NAMESPACE="clawbox-benchmarks"
RENDERED_DIR="${ROOT}/.artifacts/rendered-deploy"
DEPLOYMENTS=(
  clawbox-ingester
  clawbox-tune-kb
  clawbox-cell-controller
  clawbox-managed-api
  clawbox-managed-dispatcher
)
LOCAL_API_PORT_FORWARD_PID=""
LOCAL_API_LOG_FILE=""
SETUP_TEMP_DIR=""
CONFIG_DATABASE_URL="${CLAWBOX_DATABASE_URL:-}"
CONFIG_LLM_API_KEY="${CLAWBOX_LLM_API_KEY:-}"
CONFIG_LLM_BASE_URL="${CLAWBOX_LLM_BASE_URL:-}"
CONFIG_LLM_MODEL="${CLAWBOX_LLM_MODEL:-}"
CONFIG_OPENCLAW_MODEL_REF="${CLAWBOX_OPENCLAW_MODEL_REF:-}"
CONFIG_TOOL_IMAGE="${CLAWBOX_TOOL_IMAGE:-}"
CONFIG_LLM_EGRESS_CIDR="${CLAWBOX_LLM_EGRESS_CIDR:-}"
CONFIG_PROFILE="${CLAWBOX_PROFILE:-small}"
CONFIG_CONTROL_IMAGE="${CLAWBOX_CONTROL_IMAGE:-}"
CONFIG_RUNTIME_IMAGE="${CLAWBOX_RUNTIME_IMAGE:-}"
CONFIG_TOOL_BRIDGE_IMAGE="${CLAWBOX_TOOL_BRIDGE_IMAGE:-}"

usage() {
  cat <<'EOF'
Usage:
  scripts/clawbox doctor
  scripts/clawbox configure [options]
  scripts/clawbox install [options]
  scripts/clawbox up
  scripts/clawbox submit [options]

doctor  Print one concise readiness report. It never changes the host.
configure
        One-time configuration for a new host. Values may also be supplied
        with the corresponding CLAWBOX_* environment variables.
install One-time configure + deploy for an already bootstrapped new host.
up      Start an already-provisioned host and reconcile the five services.
submit  Uses the normal CLI; on the host, token and port-forward are automatic.

`up` never partitions or erases disks and never runs the host bootstrap.

configure options:
  --database-url URL             PostgreSQL psycopg URL
  --llm-api-key VALUE            LLM provider API key
  --llm-base-url URL             OpenAI-compatible upstream URL
  --llm-model NAME               Provider model name
  --openclaw-model-ref REF       OpenClaw model reference
  --tool-image IMAGE@sha256:...  ARM64 task image
  --llm-egress-cidr CIDR         Provider egress CIDR
  --profile small|medium|large   Fixed resource profile (default: small)
  --control-image IMAGE@sha256:...
  --runtime-image IMAGE@sha256:...
  --tool-bridge-image IMAGE@sha256:...
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
require() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

deployment_exists() {
  kubectl -n "${SYSTEM_NAMESPACE}" get deployment "$1" >/dev/null 2>&1
}

all_deployments_exist() {
  local deployment
  for deployment in "${DEPLOYMENTS[@]}"; do
    deployment_exists "${deployment}" || return 1
  done
}

wait_for_cluster() {
  local attempt
  for attempt in $(seq 1 60); do
    kubectl get nodes >/dev/null 2>&1 && return 0
    sleep 1
  done
  die "Kubernetes did not become reachable within 60 seconds"
}

start_host_services() {
  local service all_active=1
  for service in containerd kubelet docker; do
    systemctl is-active --quiet "${service}" || all_active=0
  done
  if (( all_active )) && kubectl get nodes >/dev/null 2>&1; then
    printf 'Host services are already running.\n'
    return
  fi
  require sudo
  sudo -v
  sudo systemctl start containerd kubelet docker
  wait_for_cluster
}

wait_for_deployments() {
  local deployment
  for deployment in "${DEPLOYMENTS[@]}"; do
    kubectl -n "${SYSTEM_NAMESPACE}" rollout status \
      "deployment/${deployment}" --timeout=300s
  done
}

doctor() {
  require kubectl
  local failed=0 arch node_ready label registry deployment ready desired
  arch="$(uname -m)"
  node_ready="$(kubectl get nodes --no-headers 2>/dev/null \
    | awk '$2 == "Ready" {count++} END {print count+0}' || true)"
  label="$(kubectl get nodes -l clawbox.openai.com/firecracker-ready=true \
    --no-headers 2>/dev/null | awk 'END {print NR+0}' || true)"
  node_ready="${node_ready:-0}"
  label="${label:-0}"

  printf '%-22s %s\n' CHECK RESULT
  if [[ "${arch}" == "aarch64" ]]; then
    printf '%-22s %s\n' architecture PASS
  else
    printf '%-22s %s\n' architecture "FAIL (${arch})"; failed=1
  fi
  if (( node_ready > 0 )); then
    printf '%-22s %s\n' kubernetes PASS
  else
    printf '%-22s %s\n' kubernetes FAIL; failed=1
  fi
  for runtime in kata-fc-arm64 kata-fc-arm64-ebpf; do
    if kubectl get runtimeclass "${runtime}" >/dev/null 2>&1; then
      printf '%-22s %s\n' "runtime/${runtime}" PASS
    else
      printf '%-22s %s\n' "runtime/${runtime}" MISSING; failed=1
    fi
  done
  if (( label > 0 )); then
    printf '%-22s %s\n' node-label PASS
  else
    printf '%-22s %s\n' node-label MISSING; failed=1
  fi

  registry=FAIL
  if command -v curl >/dev/null 2>&1 \
    && curl --noproxy '*' -fsS --max-time 2 http://127.0.0.1:5000/v2/ >/dev/null 2>&1; then
    registry=PASS
  else
    failed=1
  fi
  printf '%-22s %s\n' local-registry "${registry}"

  for deployment in "${DEPLOYMENTS[@]}"; do
    ready="$(kubectl -n "${SYSTEM_NAMESPACE}" get deployment "${deployment}" \
      -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
    desired="$(kubectl -n "${SYSTEM_NAMESPACE}" get deployment "${deployment}" \
      -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
    ready="${ready:-0}"
    desired="${desired:-0}"
    if [[ "${desired}" != "0" && "${ready}" == "${desired}" ]]; then
      printf '%-22s %s\n' "deploy/${deployment#clawbox-}" PASS
    else
      printf '%-22s %s\n' "deploy/${deployment#clawbox-}" "NOT READY (${ready}/${desired})"
      failed=1
    fi
  done

  if (( failed )); then
    printf '\nNot ready. Run: scripts/clawbox up\n' >&2
    return 1
  fi
  printf '\nClawBox is ready. Submit with: scripts/clawbox submit ...\n'
}

secret_value() {
  kubectl -n "$1" get secret "$2" -o "jsonpath={.data.$3}" | base64 -d
}

cleanup_local_api() {
  if [[ -n "${LOCAL_API_PORT_FORWARD_PID}" ]]; then
    kill "${LOCAL_API_PORT_FORWARD_PID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${LOCAL_API_LOG_FILE}" ]]; then
    rm -f "${LOCAL_API_LOG_FILE}"
  fi
}

cleanup_setup_files() {
  if [[ -n "${SETUP_TEMP_DIR}" && -d "${SETUP_TEMP_DIR}" ]]; then
    rm -f \
      "${SETUP_TEMP_DIR}/database-url" \
      "${SETUP_TEMP_DIR}/service-token" \
      "${SETUP_TEMP_DIR}/templates" \
      "${SETUP_TEMP_DIR}/llm-api-key" \
      "${SETUP_TEMP_DIR}/llm-upstream-base-url" \
      "${SETUP_TEMP_DIR}/llm-model" \
      "${SETUP_TEMP_DIR}/openclaw-model-ref" \
      "${SETUP_TEMP_DIR}/ingest-secret" \
      "${SETUP_TEMP_DIR}/migration.env" \
      "${SETUP_TEMP_DIR}/clawbox-managed.db" \
      "${SETUP_TEMP_DIR}/capacity.yaml" \
      "${SETUP_TEMP_DIR}/secret.yaml"
    rmdir "${SETUP_TEMP_DIR}" 2>/dev/null || true
  fi
  SETUP_TEMP_DIR=""
}

start_setup_dir() {
  cleanup_setup_files
  SETUP_TEMP_DIR="$(mktemp -d)"
  chmod 700 "${SETUP_TEMP_DIR}"
  trap cleanup_setup_files EXIT
}

immutable_image() {
  [[ "$1" =~ ^[a-zA-Z0-9._:/-]+@sha256:[a-f0-9]{64}$ ]]
}

resolve_image_digest() {
  local image="$1" repository digest
  if immutable_image "${image}"; then
    printf '%s\n' "${image}"
    return
  fi
  [[ "${image}" == *:* ]] || die "image has no tag or digest: ${image}"
  require docker
  docker image inspect "${image}" >/dev/null 2>&1 || docker pull "${image}" >/dev/null
  repository="${image%:*}"
  digest="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${image}" \
    | grep -E "^${repository//./\\.}@sha256:[a-f0-9]{64}$" | head -n 1 || true)"
  immutable_image "${digest}" || die "could not resolve immutable digest for ${image}"
  printf '%s\n' "${digest}"
}

deployment_container_image() {
  kubectl -n "${SYSTEM_NAMESPACE}" get deployment "$1" \
    -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true
}

controller_env_image() {
  local name="$1"
  kubectl -n "${SYSTEM_NAMESPACE}" get deployment clawbox-cell-controller -o json 2>/dev/null \
    | python3 -c 'import json,sys
name=sys.argv[1]
try: data=json.load(sys.stdin)
except json.JSONDecodeError: raise SystemExit
for container in data.get("spec",{}).get("template",{}).get("spec",{}).get("containers",[]):
    for item in container.get("env",[]):
        if item.get("name") == name and item.get("value"):
            print(item["value"]); raise SystemExit' "${name}"
}

ensure_rendered_manifests() {
  local path control runtime tool_bridge
  printf 'Rendering immutable deployment manifests...\n'
  control="${CONFIG_CONTROL_IMAGE:-$(deployment_container_image clawbox-cell-controller)}"
  runtime="${CONFIG_RUNTIME_IMAGE:-$(controller_env_image RUNTIME_IMAGE)}"
  tool_bridge="${CONFIG_TOOL_BRIDGE_IMAGE:-$(controller_env_image TOOL_BRIDGE_IMAGE)}"
  control="${control:-127.0.0.1:5000/clawbox/control-plane-arm64:dev}"
  runtime="${runtime:-127.0.0.1:5000/clawbox/runtime-arm64:dev}"
  tool_bridge="${tool_bridge:-127.0.0.1:5000/clawbox/tool-bridge-arm64:dev}"
  CONFIG_CONTROL_IMAGE="$(resolve_image_digest "${control}")"
  CONFIG_RUNTIME_IMAGE="$(resolve_image_digest "${runtime}")"
  CONFIG_TOOL_BRIDGE_IMAGE="$(resolve_image_digest "${tool_bridge}")"
  python3 "${ROOT}/scripts/render-kubernetes-images.py" \
    --control-image "${CONFIG_CONTROL_IMAGE}" \
    --runtime-image "${CONFIG_RUNTIME_IMAGE}" \
    --tool-bridge-image "${CONFIG_TOOL_BRIDGE_IMAGE}" >/dev/null
}

managed_database_url() {
  local url="${CONFIG_DATABASE_URL:-}"
  [[ -n "${url}" ]] || url="$(secret_value "${SYSTEM_NAMESPACE}" clawbox-control-plane database-url)"
  if [[ "${url}" == sqlite:* ]]; then
    printf '%s\n' 'sqlite:////data/clawbox-managed.db'
  else
    printf '%s\n' "${url}"
  fi
}

restart_managed_deployments_if_present() {
  local deployment
  for deployment in clawbox-managed-api clawbox-managed-dispatcher; do
    if deployment_exists "${deployment}"; then
      kubectl -n "${SYSTEM_NAMESPACE}" rollout restart "deployment/${deployment}" >/dev/null
    fi
  done
}

migrate_legacy_managed_secret() {
  kubectl -n "${SYSTEM_NAMESPACE}" get secret clawbox-managed >/dev/null 2>&1 && return
  require openssl

  local legacy task_name tool_image llm_secret llm_cidr profile runtime_image
  legacy="$(kubectl -n "${TASK_NAMESPACE}" get sandboxtasks -o json | python3 -c '
import ipaddress, json, re, sys
items = json.load(sys.stdin).get("items", [])
items.sort(key=lambda item: item.get("metadata", {}).get("creationTimestamp", ""), reverse=True)
for item in items:
    spec = item.get("spec") or {}
    image = str(spec.get("toolImage", ""))
    cidr = str(spec.get("llmEgressCIDR", ""))
    secret = str(spec.get("llmSecretName", ""))
    profile = str(spec.get("profile", "small"))
    try:
        network = ipaddress.ip_network(cidr, strict=True)
    except ValueError:
        continue
    if (re.fullmatch(r".+@sha256:[a-f0-9]{64}", image)
            and secret and profile in {"small", "medium", "large"}):
        print("\t".join((item["metadata"]["name"], image, secret, cidr, profile)))
        break
')"
  [[ -n "${legacy}" ]] || die \
    "missing Secret ${SYSTEM_NAMESPACE}/clawbox-managed and no prior SandboxTask has safe reusable template settings; complete README section 5 once"
  IFS=$'\t' read -r task_name tool_image llm_secret llm_cidr profile <<<"${legacy}"

  if [[ "${llm_cidr}" == "0.0.0.0/0" || "${llm_cidr}" == "::/0" ]]; then
    printf 'WARNING: preserving open LLM egress CIDR %s from legacy SandboxTask %s.\n' \
      "${llm_cidr}" "${task_name}" >&2
    printf '         Re-run scripts/clawbox configure --tool-image IMAGE@sha256:... --llm-egress-cidr CIDR to restrict it.\n' >&2
  fi

  kubectl -n "${TASK_NAMESPACE}" get secret "${llm_secret}" >/dev/null 2>&1 \
    || die "legacy task ${task_name} references missing Secret ${TASK_NAMESPACE}/${llm_secret}"
  runtime_image="${CONFIG_RUNTIME_IMAGE:-$(controller_env_image RUNTIME_IMAGE)}"
  runtime_image="$(resolve_image_digest "${runtime_image}")"

  SETUP_TEMP_DIR="$(mktemp -d)"
  chmod 700 "${SETUP_TEMP_DIR}"
  trap cleanup_setup_files EXIT
  managed_database_url >"${SETUP_TEMP_DIR}/database-url"
  openssl rand -hex 32 | tr -d '\n' >"${SETUP_TEMP_DIR}/service-token"
  python3 - "${tool_image}" "${llm_secret}" "${runtime_image}" "${llm_cidr}" "${profile}" \
    >"${SETUP_TEMP_DIR}/templates" <<'PY'
import json
import sys

tool_image, secret_name, runtime_image, llm_cidr, profile = sys.argv[1:]
json.dump({
    "swe-rebench-arm64": {
        "1": {
            "toolImage": tool_image,
            "secretName": secret_name,
            "runtimeImage": runtime_image,
            "llmEgressCIDR": llm_cidr,
            "profile": profile,
        }
    }
}, sys.stdout, separators=(",", ":"))
PY
  kubectl -n "${SYSTEM_NAMESPACE}" create secret generic clawbox-managed \
    --from-file="database-url=${SETUP_TEMP_DIR}/database-url" \
    --from-file="service-token=${SETUP_TEMP_DIR}/service-token" \
    --from-file="templates=${SETUP_TEMP_DIR}/templates" \
    --dry-run=client -o yaml >"${SETUP_TEMP_DIR}/secret.yaml"
  kubectl apply -f "${SETUP_TEMP_DIR}/secret.yaml" >/dev/null
  cleanup_setup_files
  trap - EXIT
  printf 'Created clawbox-managed by reusing safe settings from SandboxTask %s.\n' "${task_name}"
}

parse_configure_args() {
  while (( $# )); do
    case "$1" in
      --database-url) CONFIG_DATABASE_URL="${2:-}"; shift 2 ;;
      --llm-api-key) CONFIG_LLM_API_KEY="${2:-}"; shift 2 ;;
      --llm-base-url) CONFIG_LLM_BASE_URL="${2:-}"; shift 2 ;;
      --llm-model) CONFIG_LLM_MODEL="${2:-}"; shift 2 ;;
      --openclaw-model-ref) CONFIG_OPENCLAW_MODEL_REF="${2:-}"; shift 2 ;;
      --tool-image) CONFIG_TOOL_IMAGE="${2:-}"; shift 2 ;;
      --llm-egress-cidr) CONFIG_LLM_EGRESS_CIDR="${2:-}"; shift 2 ;;
      --profile) CONFIG_PROFILE="${2:-}"; shift 2 ;;
      --control-image) CONFIG_CONTROL_IMAGE="${2:-}"; shift 2 ;;
      --runtime-image) CONFIG_RUNTIME_IMAGE="${2:-}"; shift 2 ;;
      --tool-bridge-image) CONFIG_TOOL_BRIDGE_IMAGE="${2:-}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown configure option: $1" ;;
    esac
  done
  [[ "${CONFIG_PROFILE}" =~ ^(small|medium|large)$ ]] \
    || die "profile must be small, medium, or large"
}

ensure_namespaces() {
  kubectl create namespace "${SYSTEM_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl create namespace "${TASK_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

ensure_base_secrets() {
  local value
  if ! kubectl -n "${SYSTEM_NAMESPACE}" get secret clawbox-control-plane >/dev/null 2>&1; then
    [[ -n "${CONFIG_DATABASE_URL}" ]] || die \
      "new host needs --database-url (or CLAWBOX_DATABASE_URL)"
    require openssl
    start_setup_dir
    printf '%s' "${CONFIG_DATABASE_URL}" >"${SETUP_TEMP_DIR}/database-url"
    openssl rand -hex 32 | tr -d '\n' >"${SETUP_TEMP_DIR}/service-token"
    openssl rand -hex 32 | tr -d '\n' >"${SETUP_TEMP_DIR}/ingest-secret"
    kubectl -n "${SYSTEM_NAMESPACE}" create secret generic clawbox-control-plane \
      --from-file="database-url=${SETUP_TEMP_DIR}/database-url" \
      --from-file="service-token=${SETUP_TEMP_DIR}/service-token" \
      --from-file="ingest-secret=${SETUP_TEMP_DIR}/ingest-secret" \
      --dry-run=client -o yaml >"${SETUP_TEMP_DIR}/secret.yaml"
    kubectl apply -f "${SETUP_TEMP_DIR}/secret.yaml" >/dev/null
    cleanup_setup_files; trap - EXIT
    printf 'Created Secret %s/clawbox-control-plane.\n' "${SYSTEM_NAMESPACE}"
  fi

  if ! kubectl -n "${TASK_NAMESPACE}" get secret clawbox-llm >/dev/null 2>&1; then
    for value in CONFIG_LLM_API_KEY CONFIG_LLM_BASE_URL CONFIG_LLM_MODEL CONFIG_OPENCLAW_MODEL_REF; do
      [[ -n "${!value}" ]] || die "new host needs all LLM options; missing ${value}"
    done
    start_setup_dir
    printf '%s' "${CONFIG_LLM_API_KEY}" >"${SETUP_TEMP_DIR}/llm-api-key"
    printf '%s' "${CONFIG_LLM_BASE_URL}" >"${SETUP_TEMP_DIR}/llm-upstream-base-url"
    printf '%s' "${CONFIG_LLM_MODEL}" >"${SETUP_TEMP_DIR}/llm-model"
    printf '%s' "${CONFIG_OPENCLAW_MODEL_REF}" >"${SETUP_TEMP_DIR}/openclaw-model-ref"
    kubectl -n "${TASK_NAMESPACE}" create secret generic clawbox-llm \
      --from-file="llm-api-key=${SETUP_TEMP_DIR}/llm-api-key" \
      --from-file="llm-upstream-base-url=${SETUP_TEMP_DIR}/llm-upstream-base-url" \
      --from-file="llm-model=${SETUP_TEMP_DIR}/llm-model" \
      --from-file="openclaw-model-ref=${SETUP_TEMP_DIR}/openclaw-model-ref" \
      --dry-run=client -o yaml >"${SETUP_TEMP_DIR}/secret.yaml"
    kubectl apply -f "${SETUP_TEMP_DIR}/secret.yaml" >/dev/null
    cleanup_setup_files; trap - EXIT
    printf 'Created Secret %s/clawbox-llm.\n' "${TASK_NAMESPACE}"
  fi
}

create_managed_from_options() {
  local runtime_image
  [[ -n "${CONFIG_TOOL_IMAGE}" ]] || return 1
  immutable_image "${CONFIG_TOOL_IMAGE}" || die "--tool-image must use IMAGE@sha256:<64 lowercase hex>"
  [[ -n "${CONFIG_LLM_EGRESS_CIDR}" ]] || die "--llm-egress-cidr is required with --tool-image"
  python3 - "${CONFIG_LLM_EGRESS_CIDR}" <<'PY' >/dev/null
import ipaddress, sys
ipaddress.ip_network(sys.argv[1], strict=True)
PY
  runtime_image="${CONFIG_RUNTIME_IMAGE:-$(controller_env_image RUNTIME_IMAGE)}"
  runtime_image="$(resolve_image_digest "${runtime_image}")"
  require openssl
  start_setup_dir
  managed_database_url >"${SETUP_TEMP_DIR}/database-url"
  if kubectl -n "${SYSTEM_NAMESPACE}" get secret clawbox-managed >/dev/null 2>&1; then
    secret_value "${SYSTEM_NAMESPACE}" clawbox-managed service-token >"${SETUP_TEMP_DIR}/service-token"
  else
    openssl rand -hex 32 | tr -d '\n' >"${SETUP_TEMP_DIR}/service-token"
  fi
  python3 - "${CONFIG_TOOL_IMAGE}" clawbox-llm "${runtime_image}" \
    "${CONFIG_LLM_EGRESS_CIDR}" "${CONFIG_PROFILE}" >"${SETUP_TEMP_DIR}/templates" <<'PY'
import json, sys
tool, secret, runtime, cidr, profile = sys.argv[1:]
json.dump({"swe-rebench-arm64":{"1":{"toolImage":tool,"secretName":secret,
    "runtimeImage":runtime,"llmEgressCIDR":cidr,"profile":profile}}}, sys.stdout,
    separators=(",", ":"))
PY
  kubectl -n "${SYSTEM_NAMESPACE}" create secret generic clawbox-managed \
    --from-file="database-url=${SETUP_TEMP_DIR}/database-url" \
    --from-file="service-token=${SETUP_TEMP_DIR}/service-token" \
    --from-file="templates=${SETUP_TEMP_DIR}/templates" \
    --dry-run=client -o yaml >"${SETUP_TEMP_DIR}/secret.yaml"
  kubectl apply -f "${SETUP_TEMP_DIR}/secret.yaml" >/dev/null
  cleanup_setup_files; trap - EXIT
  restart_managed_deployments_if_present
  printf 'Configured Secret %s/clawbox-managed.\n' "${SYSTEM_NAMESPACE}"
}

normalize_managed_database_secret() {
  local current desired token encoded canonical
  current="$(secret_value "${SYSTEM_NAMESPACE}" clawbox-managed database-url)"
  desired="$(managed_database_url)"
  token="$(secret_value "${SYSTEM_NAMESPACE}" clawbox-managed service-token)"
  encoded="$(kubectl -n "${SYSTEM_NAMESPACE}" get secret clawbox-managed \
    -o jsonpath='{.data.service-token}')"
  canonical="$(printf '%s' "${token}" | base64 | tr -d '\n')"
  if [[ "${current}" == "${desired}" && "${encoded}" == "${canonical}" ]]; then
    return
  fi
  if [[ "${current}" != "${desired}" && "${current}" != sqlite:* ]]; then
    return
  fi
  start_setup_dir
  printf '%s' "${desired}" >"${SETUP_TEMP_DIR}/database-url"
  printf '%s' "${token}" >"${SETUP_TEMP_DIR}/service-token"
  secret_value "${SYSTEM_NAMESPACE}" clawbox-managed templates >"${SETUP_TEMP_DIR}/templates"
  kubectl -n "${SYSTEM_NAMESPACE}" create secret generic clawbox-managed \
    --from-file="database-url=${SETUP_TEMP_DIR}/database-url" \
    --from-file="service-token=${SETUP_TEMP_DIR}/service-token" \
    --from-file="templates=${SETUP_TEMP_DIR}/templates" \
    --dry-run=client -o yaml >"${SETUP_TEMP_DIR}/secret.yaml"
  kubectl apply -f "${SETUP_TEMP_DIR}/secret.yaml" >/dev/null
  cleanup_setup_files; trap - EXIT
  restart_managed_deployments_if_present
  printf 'Normalized the single-node Managed SQLite path.\n'
}

configure() {
  parse_configure_args "$@"
  require kubectl; require python3; require base64
  wait_for_cluster
  ensure_namespaces
  ensure_base_secrets
  ensure_rendered_manifests
  if [[ -n "${CONFIG_TOOL_IMAGE}" ]]; then
    create_managed_from_options
  elif ! kubectl -n "${SYSTEM_NAMESPACE}" get secret clawbox-managed >/dev/null 2>&1; then
    migrate_legacy_managed_secret
  fi
  normalize_managed_database_secret
  printf 'Configuration is ready. Run: scripts/clawbox up\n'
}

preflight_reconcile() {
  ensure_namespaces
  kubectl -n "${SYSTEM_NAMESPACE}" get secret clawbox-control-plane >/dev/null 2>&1 \
    || die "missing Secret ${SYSTEM_NAMESPACE}/clawbox-control-plane; run scripts/clawbox configure"
  ensure_base_secrets
  ensure_rendered_manifests
  migrate_legacy_managed_secret
  normalize_managed_database_secret
  kubectl -n "${TASK_NAMESPACE}" get secret clawbox-llm >/dev/null 2>&1 \
    || die "missing Secret ${TASK_NAMESPACE}/clawbox-llm; run scripts/clawbox configure"
}

run_migrations() {
  local database_url control_image
  database_url="$(secret_value "${SYSTEM_NAMESPACE}" clawbox-managed database-url)"
  [[ -n "${database_url}" ]] || die "Secret clawbox-managed has an empty database-url"
  control_image="${CONFIG_CONTROL_IMAGE:-$(deployment_container_image clawbox-cell-controller)}"
  control_image="$(resolve_image_digest "${control_image}")"
  start_setup_dir
  printf 'DATABASE_URL=%s\n' "${database_url}" >"${SETUP_TEMP_DIR}/migration.env"
  local docker_args=(--rm --network host --env-file "${SETUP_TEMP_DIR}/migration.env")
  if [[ "${database_url}" == sqlite:* ]]; then
    if [[ ! -f /var/lib/clawbox/managed/clawbox-managed.db ]] \
        && docker container inspect clawbox-m1-api >/dev/null 2>&1; then
      docker cp clawbox-m1-api:/data/clawbox-managed.db \
        "${SETUP_TEMP_DIR}/clawbox-managed.db" 2>/dev/null || true
      if [[ -f "${SETUP_TEMP_DIR}/clawbox-managed.db" ]]; then
        docker run --rm --user 0 \
          -v "${SETUP_TEMP_DIR}:/source:ro" -v /var/lib/clawbox/managed:/data \
          "${control_image}" sh -c \
          'cp /source/clawbox-managed.db /data/clawbox-managed.db && chown 10001:10001 /data/clawbox-managed.db'
        printf 'Imported the legacy Docker Managed database.\n'
      fi
    fi
    docker run --rm --user 0 -v /var/lib/clawbox/managed:/data \
      "${control_image}" chown -R 10001:10001 /data
    docker_args+=(-v /var/lib/clawbox/managed:/data)
  fi
  printf 'Applying database migrations...\n'
  docker run "${docker_args[@]}" "${control_image}" alembic upgrade head
  cleanup_setup_files; trap - EXIT
}

ensure_node_label() {
  if kubectl get nodes -l clawbox.openai.com/firecracker-ready=true \
    --no-headers 2>/dev/null | grep -q .; then
    return
  fi

  local kubeconfig="${KUBECONFIG:-${HOME}/.kube/config}"
  printf 'Validating the host before adding the Firecracker-ready label...\n'
  sudo env \
    KUBECONFIG="${kubeconfig}" \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    bash "${ROOT}/deploy/check-host.sh" --runtime-class kata-fc-arm64
  kubectl label nodes --all clawbox.openai.com/firecracker-ready=true --overwrite >/dev/null
}

ensure_capacity_configmap() {
  local kubeconfig
  kubectl -n "${SYSTEM_NAMESPACE}" get configmap clawbox-node-capacity >/dev/null 2>&1 && return
  require sudo
  kubeconfig="${KUBECONFIG:-${HOME}/.kube/config}"
  start_setup_dir
  printf 'Collecting the initial privileged node-capacity baseline...\n'
  sudo env KUBECONFIG="${kubeconfig}" \
    PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    python3 "${ROOT}/scripts/collect-node-capacity.py" --configmap \
    >"${SETUP_TEMP_DIR}/capacity.yaml"
  kubectl apply -f "${SETUP_TEMP_DIR}/capacity.yaml"
  cleanup_setup_files; trap - EXIT
}

retire_legacy_managed_containers() {
  local container
  command -v docker >/dev/null 2>&1 || return
  for container in clawbox-m1-api clawbox-m1-dispatcher; do
    if [[ "$(docker container inspect -f '{{.State.Running}}' "${container}" 2>/dev/null || true)" == true ]]; then
      printf 'Stopping superseded Docker container %s...\n' "${container}"
      docker stop "${container}" >/dev/null
    fi
  done
}

up() {
  [[ "$(uname -s)" == Linux ]] || die "up must run on the Kunpeng Linux host"
  require kubectl
  require python3
  require base64
  require curl

  printf 'Starting host services...\n'
  start_host_services
  ensure_node_label

  if all_deployments_exist; then
    printf 'ClawBox resources already exist; waiting for readiness...\n'
    wait_for_deployments
    retire_legacy_managed_containers
    doctor
    return
  fi

  preflight_reconcile
  run_migrations

  printf 'Reconciling ClawBox resources...\n'
  kubectl apply -f "${ROOT}/deploy/runtimeclass-firecracker.yaml"
  kubectl apply -f "${ROOT}/deploy/runtimeclass-firecracker-ebpf.yaml"
  kubectl apply -f "${ROOT}/deploy/sandboxtask-crd.yaml"
  kubectl apply -f "${ROOT}/deploy/control-plane-rbac.yaml"
  kubectl apply -f "${ROOT}/deploy/managed-rbac.yaml"
  ensure_capacity_configmap
  kubectl apply -f "${RENDERED_DIR}/trace-ingester.yaml"
  kubectl apply -f "${RENDERED_DIR}/tune-kb.yaml"
  kubectl apply -f "${RENDERED_DIR}/cell-controller.yaml"
  kubectl apply -f "${RENDERED_DIR}/managed-control-plane.yaml"
  wait_for_deployments
  retire_legacy_managed_containers
  doctor
}

install_host() {
  [[ "$(uname -s)" == Linux ]] || die "install must run on the Kunpeng Linux host"
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    return
  fi
  printf 'Preparing a new ClawBox installation...\n'
  start_host_services
  ensure_node_label
  configure "$@"
  up
}

local_api() {
  shift
  require kubectl
  require curl
  require base64
  require python3
  deployment_exists clawbox-managed-api \
    || die "Managed API is not deployed; run scripts/clawbox up"

  local token ready=0
  token="$(secret_value "${SYSTEM_NAMESPACE}" clawbox-managed service-token)"
  [[ -n "${token}" ]] || die "Secret clawbox-managed has an empty service-token"

  if ! curl --noproxy '*' -fsS --max-time 2 \
    http://127.0.0.1:8085/healthz >/dev/null 2>&1; then
    LOCAL_API_LOG_FILE="$(mktemp)"
    kubectl -n "${SYSTEM_NAMESPACE}" port-forward \
      service/clawbox-managed-api 8085:8085 >"${LOCAL_API_LOG_FILE}" 2>&1 &
    LOCAL_API_PORT_FORWARD_PID=$!
    trap cleanup_local_api EXIT
    for _ in $(seq 1 30); do
      if curl --noproxy '*' -fsS --max-time 2 \
        http://127.0.0.1:8085/healthz >/dev/null 2>&1; then
        ready=1
        break
      fi
      kill -0 "${LOCAL_API_PORT_FORWARD_PID}" >/dev/null 2>&1 || break
      sleep 1
    done
    if (( ! ready )); then
      sed -n '1,80p' "${LOCAL_API_LOG_FILE}" >&2
      die "Managed API port-forward did not become ready"
    fi
  fi

  export CLAWBOX_API_URL=http://127.0.0.1:8085
  export CLAWBOX_TOKEN="${token}"
  python3 -m clawbox.cli "$@"
}

case "${1:-}" in
  doctor) doctor ;;
  configure) shift; configure "$@" ;;
  install) shift; install_host "$@" ;;
  up) up ;;
  local-api) local_api "$@" ;;
  -h|--help|help|"") usage ;;
  *) usage >&2; die "unknown host command: ${1}" ;;
esac
