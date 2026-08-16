#!/usr/bin/env bash
# SCALE-1: stop at the first failed concurrency step or thin-pool pressure gate.
set -euo pipefail

TASKS=""
MAPPING=""
LLM_CIDR=""
NAMESPACE="clawbox-benchmarks"
PROFILE="small"
STEPS="${CLAWBOX_SCALE_STEPS:-1 2 4 8 16 32}"

usage() {
  echo "usage: scale-swe-rebench.sh --tasks FILE --arm64-map FILE --llm-egress-cidr CIDR [--namespace NS] [--profile small|medium|large]" >&2
  exit 64
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tasks) TASKS="${2:-}"; shift 2 ;;
    --arm64-map) MAPPING="${2:-}"; shift 2 ;;
    --llm-egress-cidr) LLM_CIDR="${2:-}"; shift 2 ;;
    --namespace) NAMESPACE="${2:-}"; shift 2 ;;
    --profile) PROFILE="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done
[[ -f "${TASKS}" && -f "${MAPPING}" && -n "${LLM_CIDR}" ]] || usage
[[ "${PROFILE}" =~ ^(small|medium|large)$ ]] || usage

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
for parallelism in ${STEPS}; do
  [[ "${parallelism}" =~ ^[0-9]+$ ]] || { echo "invalid scale step: ${parallelism}" >&2; exit 64; }
  echo "== SCALE-1 parallelism=${parallelism} =="
  sudo bash "${ROOT}/scripts/setup-devmapper-openeuler-arm64.sh" status
  python3 -m clawbox.benchmark.kubernetes \
    --tasks "${TASKS}" --arm64-map "${MAPPING}" --namespace "${NAMESPACE}" \
    --llm-egress-cidr "${LLM_CIDR}" --profile "${PROFILE}" \
    --parallelism "${parallelism}" --sample "${parallelism}"
  sudo bash "${ROOT}/scripts/setup-devmapper-openeuler-arm64.sh" status
  kubectl get --namespace "${NAMESPACE}" sandboxtasks \
    -l app.kubernetes.io/managed-by=clawbox-benchmark-launcher
done
