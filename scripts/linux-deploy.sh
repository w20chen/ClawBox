#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_DIR="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
PROJECT_NAME="clawbox"
ENV_FILE="${PROJECT_DIR}/.env"
COMPOSE=(docker compose -p "${PROJECT_NAME}" --env-file "${ENV_FILE}")

usage() {
  cat <<'EOF'
Usage: bash scripts/linux-deploy.sh [command]

Commands:
  init       Check Linux prerequisites and create/update .env safely
  deploy     Run init, build images, start services, and wait for health
  verify     Execute one real Docker Tool task and check its result
  all        Deploy and verify (default)
  status     Show containers and service health
  logs       Follow control-plane logs
  down       Stop/remove containers and network; preserve the database volume

Environment overrides used by init:
  CLAWTUNE_DIR, NUMA_CAPACITY, RESERVED_CPU_FRACTION, TOOL_IMAGE
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }

require_linux() {
  [[ "$(uname -s)" == "Linux" ]] || die "this deployment script requires Linux"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

env_has_key() {
  [[ -f "${ENV_FILE}" ]] && grep -qE "^${1}=" "${ENV_FILE}"
}

append_env_if_missing() {
  local key="$1" value="$2"
  if ! env_has_key "${key}"; then
    printf '%s=%s\n' "${key}" "${value}" >>"${ENV_FILE}"
  fi
}

random_secret() {
  openssl rand -hex 32
}

cpu_list_count() {
  local list="$1" total=0 part start end
  IFS=',' read -ra parts <<<"${list}"
  for part in "${parts[@]}"; do
    if [[ "${part}" == *-* ]]; then
      start="${part%-*}"; end="${part#*-}"
      ((total += end - start + 1))
    elif [[ -n "${part}" ]]; then
      ((total += 1))
    fi
  done
  printf '%s' "${total}"
}

detect_numa_capacity() {
  local result="" node cpulist node_id count
  shopt -s nullglob
  local nodes=(/sys/devices/system/node/node[0-9]*)
  shopt -u nullglob
  if ((${#nodes[@]} == 0)); then
    printf '0:%s' "$(getconf _NPROCESSORS_ONLN)"
    return
  fi
  for node in "${nodes[@]}"; do
    node_id="${node##*node}"
    cpulist="$(<"${node}/cpulist")"
    count="$(cpu_list_count "${cpulist}")"
    [[ "${count}" -gt 0 ]] || continue
    [[ -z "${result}" ]] || result+="," 
    result+="${node_id}:${count}"
  done
  [[ -n "${result}" ]] || die "could not detect CPU capacity from sysfs"
  printf '%s' "${result}"
}

init_config() {
  require_linux
  require_command docker
  require_command openssl
  require_command curl
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is unavailable"
  docker info >/dev/null 2>&1 || die "Docker daemon is unavailable or current user lacks permission"
  [[ "$(stat -fc %T /sys/fs/cgroup)" == "cgroup2fs" ]] || die "cgroup v2 is required"

  local clawtune_dir="${CLAWTUNE_DIR:-${PROJECT_DIR}/../ClawTune}"
  [[ -f "${clawtune_dir}/services/sidecar/pyproject.toml" ]] || \
    die "ClawTune not found at ${clawtune_dir}; set CLAWTUNE_DIR or place it beside ClawBox"
  local expected_clawtune
  expected_clawtune="$(CDPATH= cd -- "${PROJECT_DIR}/../ClawTune" 2>/dev/null && pwd || true)"
  [[ "$(CDPATH= cd -- "${clawtune_dir}" && pwd)" == "${expected_clawtune}" ]] || \
    die "current docker-compose.yml mounts ../ClawTune; place ClawTune there"

  touch "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  append_env_if_missing CLAWBOX_SERVICE_TOKEN "$(random_secret)"
  append_env_if_missing CLAWBOX_GRANT_SECRET "$(random_secret)"
  append_env_if_missing CLAWBOX_INGEST_SECRET "$(random_secret)"
  append_env_if_missing POSTGRES_PASSWORD "$(random_secret)"
  append_env_if_missing CONTROLLER_BACKEND docker
  append_env_if_missing CONTROLLER_DOCKER_NETWORK "${PROJECT_NAME}_default"
  append_env_if_missing TOOL_IMAGE "${TOOL_IMAGE:-clawbox-tool-agent:latest}"
  append_env_if_missing NUMA_CAPACITY "${NUMA_CAPACITY:-$(detect_numa_capacity)}"
  append_env_if_missing RESERVED_CPU_FRACTION "${RESERVED_CPU_FRACTION:-0.05}"

  info "configuration ready: ${ENV_FILE}"
  grep -E '^(CONTROLLER_BACKEND|CONTROLLER_DOCKER_NETWORK|TOOL_IMAGE|NUMA_CAPACITY|RESERVED_CPU_FRACTION)=' "${ENV_FILE}"
  printf 'Secrets were generated/preserved and are not printed.\n'
}

wait_url() {
  local name="$1" url="$2" attempts="${3:-120}" body
  for ((i=1; i<=attempts; i++)); do
    if body="$(curl -fsS --max-time 2 "${url}" 2>/dev/null)"; then
      printf '%s ready: %s\n' "${name}" "${body}"
      return 0
    fi
    sleep 1
  done
  "${COMPOSE[@]}" ps >&2 || true
  "${COMPOSE[@]}" logs --tail=100 "${name}" >&2 || true
  die "${name} did not become healthy: ${url}"
}

deploy() {
  init_config
  cd "${PROJECT_DIR}"
  info "building Tool Agent image"
  "${COMPOSE[@]}" --profile build build tool-agent-image
  info "building control-plane images"
  "${COMPOSE[@]}" build tenant-scheduler allocator controller node-agent trace-ingester
  info "starting PostgreSQL and control-plane services"
  "${COMPOSE[@]}" up -d postgres allocator controller node-agent trace-ingester tenant-scheduler
  wait_url tenant-scheduler http://127.0.0.1:8080/healthz
  wait_url allocator http://127.0.0.1:8081/healthz
  wait_url controller http://127.0.0.1:8082/healthz
  wait_url node-agent http://127.0.0.1:8083/healthz
  wait_url trace-ingester http://127.0.0.1:8084/healthz
  local scheduler_health
  scheduler_health="$(curl -fsS http://127.0.0.1:8080/healthz)"
  [[ "${scheduler_health}" == *'"clawtune":"available"'* ]] || \
    die "Scheduler is running in degraded mode: ${scheduler_health}"
  info "deployment is healthy"
  "${COMPOSE[@]}" ps
}

verify() {
  [[ -f "${ENV_FILE}" ]] || die ".env does not exist; run init or deploy first"
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
  require_command curl
  require_command python3
  local execution_id payload output_file
  execution_id="linux-docker-$(date +%s)-${RANDOM}"
  payload="$(python3 - "${execution_id}" "${TOOL_IMAGE:-clawbox-tool-agent:latest}" <<'PY'
import json, sys
from datetime import datetime, timezone
print(json.dumps({
    "tenant_id": "tenant-a",
    "execution_id": sys.argv[1],
    "session_id": "deployment-check",
    "run_id": "deployment-check",
    "tool_name": "exec",
    "command": "python -c 'print(42)'",
    "argv": [],
    "repo_fingerprint": "clawbox-deployment-check",
    "tool_image": sys.argv[2],
    "workspace_id": "deployment-check-tenant-a",
    "timestamp": datetime.now(timezone.utc).isoformat(),
}))
PY
)"
  output_file="$(mktemp)"
  info "running real Docker Tool execution: ${execution_id}"
  curl -fsS --max-time 180 -X POST \
    -H "Authorization: Bearer ${CLAWBOX_SERVICE_TOKEN}" \
    -H 'Content-Type: application/json' \
    --data "${payload}" http://127.0.0.1:8080/v1/executions/run >"${output_file}" || {
      "${COMPOSE[@]}" logs --tail=150 tenant-scheduler controller allocator >&2
      die "Docker execution request failed"
    }
  python3 - "${output_file}" <<'PY'
import json, sys
result = json.load(open(sys.argv[1], encoding="utf-8"))
print(json.dumps(result, indent=2, ensure_ascii=False))
assert result["stdout"].strip() == "42", "unexpected command output"
assert result["lease_final_state"] == "RELEASED", "lease was not released"
assert result["kb_generation_after"] == result["kb_generation_before"] + 1, "KB did not advance"
print("\nPASS: Docker execution, telemetry, KB update, and lease release succeeded")
PY
  rm -f "${output_file}"
}

status() {
  [[ -f "${ENV_FILE}" ]] || die ".env does not exist"
  "${COMPOSE[@]}" ps
  for item in tenant-scheduler:8080 allocator:8081 controller:8082 node-agent:8083 trace-ingester:8084; do
    name="${item%:*}"; port="${item#*:}"
    printf '%-18s ' "${name}"
    curl -fsS --max-time 2 "http://127.0.0.1:${port}/healthz" || printf 'unavailable'
    printf '\n'
  done
}

command="${1:-all}"
case "${command}" in
  init) init_config ;;
  deploy) deploy ;;
  verify) verify ;;
  all) deploy; verify ;;
  status) status ;;
  logs) "${COMPOSE[@]}" logs -f --tail=200 tenant-scheduler allocator controller node-agent trace-ingester postgres ;;
  down) "${COMPOSE[@]}" down ;;
  -h|--help|help) usage ;;
  *) usage >&2; die "unknown command: ${command}" ;;
esac
