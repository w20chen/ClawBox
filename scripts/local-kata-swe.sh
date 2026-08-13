#!/usr/bin/env bash
# One-command local Kubernetes + Kata/Firecracker SWE-Rebench deployment.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
CLAWTUNE_ROOT="${CLAWTUNE_ROOT:-${ROOT}/../ClawTune}"
NAMESPACE="${CLAWBOX_NAMESPACE:-clawbox-benchmarks}"
BUNDLE_IMAGE="${BUNDLE_IMAGE:-clawbox/clawtune-swe-bundle:dev}"
TASKS="${TASKS:-${CLAWTUNE_ROOT}/swe_rebench/tasks.json}"
SAMPLE="${SAMPLE:-1}"
PARALLELISM="${PARALLELISM:-1}"
CPU="${CPU:-2}"
MEMORY="${MEMORY:-4Gi}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-1800}"
LLM_SECRET="${LLM_SECRET:-clawbox-llm}"
COMMAND="run"
API_KEY_FILE="${LLM_API_KEY_FILE:-}"
BASE_URL="${LLM_BASE_URL:-}"
MODEL="${LLM_MODEL:-}"
OPENCLAW_MODEL="${OPENCLAW_MODEL_REF:-}"
REBUILD="${REBUILD:-0}"
SKIP_SMOKE="${SKIP_KATA_SMOKE:-0}"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/local-kata-swe.sh [run] [options]
  bash scripts/local-kata-swe.sh smoke|status|logs

First run:
  bash scripts/local-kata-swe.sh \
    --api-key-file ~/llm-api-key.txt \
    --base-url https://api.example.com/v1 \
    --model provider-model

Options:
  --api-key-file PATH  File containing only the LLM API key (required first run)
  --base-url URL       OpenAI-compatible upstream URL (required first run)
  --model NAME         Provider model name (required first run)
  --openclaw-model REF OpenClaw model ref; default: vllm/<model>
  --tasks PATH         Task JSON; default: ../ClawTune/swe_rebench/tasks.json
  --sample N           Number of tasks; default: 1
  --parallelism N      Concurrent tasks; default: 1
  --cpu VALUE          CPU per task; default: 2
  --memory VALUE       Memory per task; default: 4Gi
  --rebuild            Rebuild the ClawTune bundle and bundle image
  --skip-smoke         Skip the kata-fc Alpine smoke Pod
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    run|smoke|status|logs) COMMAND="$1"; shift ;;
    --api-key-file) API_KEY_FILE="${2:?missing value}"; shift 2 ;;
    --base-url) BASE_URL="${2:?missing value}"; shift 2 ;;
    --model) MODEL="${2:?missing value}"; shift 2 ;;
    --openclaw-model) OPENCLAW_MODEL="${2:?missing value}"; shift 2 ;;
    --tasks) TASKS="${2:?missing value}"; shift 2 ;;
    --sample) SAMPLE="${2:?missing value}"; shift 2 ;;
    --parallelism) PARALLELISM="${2:?missing value}"; shift 2 ;;
    --cpu) CPU="${2:?missing value}"; shift 2 ;;
    --memory) MEMORY="${2:?missing value}"; shift 2 ;;
    --rebuild) REBUILD=1; shift ;;
    --skip-smoke) SKIP_SMOKE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done

log() { printf '\n==> %s\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required"; }

latest_pod() {
  kubectl -n "${NAMESPACE}" get pods \
    -l app.kubernetes.io/name=clawbox-swe-rebench \
    --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null || true
}

show_status() {
  kubectl -n "${NAMESPACE}" get jobs,pods \
    -l app.kubernetes.io/name=clawbox-swe-rebench -o wide || true
}

show_logs() {
  local pod="${1:-$(latest_pod)}"
  [[ -n "${pod}" ]] || die "no SWE-Rebench Pod found in ${NAMESPACE}"
  echo "Pod: ${pod}"
  kubectl -n "${NAMESPACE}" logs "${pod}" -c clawtune-bundle --tail=100 2>/dev/null || true
  kubectl -n "${NAMESPACE}" logs "${pod}" -c openclaw-runtime --tail=200 2>/dev/null || true
}

diagnostics() {
  echo >&2
  echo "Deployment failed; recent Kubernetes state:" >&2
  show_status >&2
  local pod
  pod="$(latest_pod)"
  if [[ -n "${pod}" ]]; then
    kubectl -n "${NAMESPACE}" describe pod "${pod}" >&2 || true
    show_logs "${pod}" >&2 || true
  fi
  kubectl -n "${NAMESPACE}" get events --sort-by=.lastTimestamp 2>/dev/null | tail -30 >&2 || true
}

need kubectl
kubectl cluster-info >/dev/null 2>&1 || die "kubectl cannot reach a Kubernetes cluster"

case "${COMMAND}" in
  status) show_status; exit 0 ;;
  logs) show_logs; exit 0 ;;
esac

run_smoke() {
  log "Verifying kata-fc can boot a microVM"
  kubectl delete pod kata-fc-smoke --ignore-not-found --wait=true >/dev/null
  kubectl run kata-fc-smoke --image=alpine:3.22 --restart=Never \
    --overrides='{"spec":{"runtimeClassName":"kata-fc","containers":[{"name":"kata-fc-smoke","image":"alpine:3.22","command":["sh","-c","echo kata-fc-ok; sleep 2"]}]}}' >/dev/null
  if ! kubectl wait pod/kata-fc-smoke --for=jsonpath='{.status.phase}'=Succeeded --timeout=180s >/dev/null; then
    kubectl describe pod kata-fc-smoke >&2 || true
    kubectl logs kata-fc-smoke >&2 || true
    die "kata-fc smoke Pod failed; RuntimeClass alone is not enough—check the containerd kata-fc handler"
  fi
  kubectl logs kata-fc-smoke
}

if [[ "${COMMAND}" == "smoke" ]]; then
  kubectl apply -f "${ROOT}/deploy/runtimeclass.yaml" >/dev/null
  run_smoke
  exit 0
fi

need docker
need python3
docker info >/dev/null 2>&1 || die "Docker is not reachable as the current user"
[[ -d "${CLAWTUNE_ROOT}" ]] || die "ClawTune not found at ${CLAWTUNE_ROOT}; set CLAWTUNE_ROOT"
[[ -f "${TASKS}" ]] || die "task file not found: ${TASKS}"

BUNDLE_DIR="${CLAWTUNE_ROOT}/swe_rebench/.runtime/bundle"
if [[ "${REBUILD}" == "1" || ! -x "${BUNDLE_DIR}/entrypoint.sh" ]]; then
  log "Preparing ClawTune runtime bundle"
  (cd "${CLAWTUNE_ROOT}" && python3 -m swe_rebench.runner prepare) || {
    echo "If an earlier sudo run owns .runtime, fix it with:" >&2
    echo "  sudo chown -R \"$(id -u):$(id -g)\" ${CLAWTUNE_ROOT}/swe_rebench/.runtime" >&2
    exit 1
  }
fi

if [[ "${REBUILD}" == "1" ]] || ! docker image inspect "${BUNDLE_IMAGE}" >/dev/null 2>&1; then
  log "Building ${BUNDLE_IMAGE}"
  docker build --build-context clawtune="${CLAWTUNE_ROOT}" \
    -f "${ROOT}/docker/Dockerfile.clawtune-bundle" \
    -t "${BUNDLE_IMAGE}" "${ROOT}"
fi

log "Applying Kubernetes resources"
kubectl apply -f "${ROOT}/deploy/runtimeclass.yaml" >/dev/null
kubectl apply -f "${ROOT}/deploy/control-plane-rbac.yaml" >/dev/null
kubectl apply -f "${ROOT}/deploy/benchmark-networkpolicy.yaml" >/dev/null

import_image() {
  local context runtime tar_file cluster
  context="$(kubectl config current-context 2>/dev/null || true)"
  runtime="$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.containerRuntimeVersion}' 2>/dev/null || true)"
  log "Loading ${BUNDLE_IMAGE} into the local cluster (${context:-unknown}, ${runtime:-unknown})"
  case "${context}" in
    kind-*) need kind; kind load docker-image "${BUNDLE_IMAGE}" --name "${context#kind-}" ;;
    minikube) need minikube; minikube image load "${BUNDLE_IMAGE}" ;;
    k3d-*) need k3d; cluster="${context#k3d-}"; k3d image import "${BUNDLE_IMAGE}" -c "${cluster}" ;;
    *)
      tar_file="$(mktemp --suffix=.tar)"
      trap 'rm -f -- "${tar_file:-}"' EXIT
      docker save "${BUNDLE_IMAGE}" -o "${tar_file}"
      if command -v k3s >/dev/null 2>&1; then
        sudo k3s ctr images import "${tar_file}"
      elif command -v ctr >/dev/null 2>&1 || [[ -S /run/containerd/containerd.sock ]]; then
        sudo ctr -n k8s.io images import "${tar_file}"
      else
        die "cannot identify a supported local image loader (kind/minikube/k3d/k3s/containerd)"
      fi
      rm -f -- "${tar_file}"
      trap - EXIT
      ;;
  esac
}
import_image

if [[ -n "${API_KEY_FILE}" ]]; then
  [[ -r "${API_KEY_FILE}" ]] || die "API key file is not readable: ${API_KEY_FILE}"
  [[ -n "${BASE_URL}" && -n "${MODEL}" ]] || die "--base-url and --model are required when creating the Secret"
  [[ -n "${OPENCLAW_MODEL}" ]] || OPENCLAW_MODEL="vllm/${MODEL}"
  log "Creating/updating ${NAMESPACE}/${LLM_SECRET}"
  kubectl -n "${NAMESPACE}" create secret generic "${LLM_SECRET}" \
    --from-file=llm-api-key="${API_KEY_FILE}" \
    --from-literal=llm-upstream-base-url="${BASE_URL}" \
    --from-literal=llm-model="${MODEL}" \
    --from-literal=openclaw-model-ref="${OPENCLAW_MODEL}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
elif ! kubectl -n "${NAMESPACE}" get secret "${LLM_SECRET}" >/dev/null 2>&1; then
  die "LLM Secret is missing; supply --api-key-file, --base-url and --model on the first run"
fi

python3 -c 'import kubernetes' >/dev/null 2>&1 || {
  log "Installing ClawBox Python dependencies"
  python3 -m pip install -e "${ROOT}"
}

if [[ "${SKIP_SMOKE}" != "1" ]]; then
  run_smoke
fi

log "Running ${SAMPLE} SWE-Rebench task(s), parallelism=${PARALLELISM}"
set +e
python3 -m clawbox.benchmark.kubernetes \
  --tasks "${TASKS}" --sample "${SAMPLE}" --parallelism "${PARALLELISM}" \
  --bundle-image "${BUNDLE_IMAGE}" --llm-secret "${LLM_SECRET}" \
  --cpu "${CPU}" --memory "${MEMORY}" --timeout-seconds "${TIMEOUT_SECONDS}"
status=$?
set -e
if [[ "${status}" -ne 0 ]]; then
  diagnostics
  exit "${status}"
fi

log "SWE-Rebench completed"
show_status
echo "Logs: bash scripts/local-kata-swe.sh logs"
