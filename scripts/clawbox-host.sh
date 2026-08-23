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

usage() {
  cat <<'EOF'
Usage:
  scripts/clawbox doctor
  scripts/clawbox up
  scripts/clawbox submit [options]

doctor  Print one concise readiness report. It never changes the host.
up      Start an already-provisioned host and reconcile the five services.
submit  Uses the normal CLI; on the host, token and port-forward are automatic.

`up` never partitions or erases disks and never runs the host bootstrap.
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

preflight_reconcile() {
  local path
  for secret in clawbox-control-plane clawbox-managed; do
    kubectl -n "${SYSTEM_NAMESPACE}" get secret "${secret}" >/dev/null 2>&1 \
      || die "missing Secret ${SYSTEM_NAMESPACE}/${secret}; complete README section 5 once"
  done
  kubectl -n "${TASK_NAMESPACE}" get secret clawbox-llm >/dev/null 2>&1 \
    || die "missing Secret ${TASK_NAMESPACE}/clawbox-llm; complete README section 5 once"

  for path in trace-ingester.yaml tune-kb.yaml cell-controller.yaml managed-control-plane.yaml; do
    [[ -f "${RENDERED_DIR}/${path}" ]] \
      || die "missing ${RENDERED_DIR}/${path}; render immutable images once using README section 4"
  done
}

run_migrations() {
  local database_url
  database_url="$(secret_value "${SYSTEM_NAMESPACE}" clawbox-managed database-url)"
  [[ -n "${database_url}" ]] || die "Secret clawbox-managed has an empty database-url"
  printf 'Applying database migrations...\n'
  (cd "${ROOT}" && DATABASE_URL="${database_url}" python3 -m alembic upgrade head)
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

up() {
  [[ "$(uname -s)" == Linux ]] || die "up must run on the Kunpeng Linux host"
  require sudo
  require kubectl
  require python3
  require base64
  require curl

  printf 'Starting host services...\n'
  sudo -v
  sudo systemctl start containerd kubelet docker
  wait_for_cluster
  ensure_node_label

  if all_deployments_exist; then
    printf 'ClawBox resources already exist; waiting for readiness...\n'
    wait_for_deployments
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
  python3 "${ROOT}/scripts/collect-node-capacity.py" --configmap | kubectl apply -f -
  kubectl apply -f "${RENDERED_DIR}/trace-ingester.yaml"
  kubectl apply -f "${RENDERED_DIR}/tune-kb.yaml"
  kubectl apply -f "${RENDERED_DIR}/cell-controller.yaml"
  kubectl apply -f "${RENDERED_DIR}/managed-control-plane.yaml"
  wait_for_deployments
  doctor
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
  up) up ;;
  local-api) local_api "$@" ;;
  -h|--help|help|"") usage ;;
  *) usage >&2; die "unknown host command: ${1}" ;;
esac
