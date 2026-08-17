#!/usr/bin/env bash
# Single-image concurrency/scale gate.
#
# Creates N concurrent SandboxTasks that all run the SAME task image, then
# reports devmapper pressure and the terminal phase distribution. This is the
# recommended way to validate Kata/Firecracker + devmapper + controller
# scaling before committing to the full 128-task benchmark, and doubles as a
# local-registry -> devmapper -> Kata E2E for one task image.
#
# Steps:
#   1. pre-pull the immutable image into the devmapper snapshotter (avoids the
#      known same-image concurrent-unpack race that stalls at
#      ContainerCreating with "target snapshot already exists")
#   2. create N cells (unique names) via deploy/cell.sh, staggered
#   3. print devmapper status before/after creation
#   4. optionally wait for all cells to reach a terminal phase, then summarize
#
# usage: single-image-scale.sh --tool-image IMG@sha256:DIGEST \
#          (--problem TEXT | --problem-file FILE) --llm-egress-cidr CIDR \
#          [--count N] [--namespace NS] [--profile small|medium|large]
#          [--prefix NAME] [--stagger-seconds S] [--no-prepull]
#          [--wait-seconds W] [--delete]
set -euo pipefail

TOOL_IMAGE=""
PROBLEM=""
PROBLEM_FILE=""
LLM_CIDR=""
COUNT="8"
NAMESPACE="clawbox-benchmarks"
PROFILE="small"
PREFIX="single"
STAGGER="2"
PREPULL=1
WAIT_SECONDS=0
DO_DELETE=0

usage() {
  cat >&2 <<'EOF'
usage: single-image-scale.sh --tool-image IMG@sha256:DIGEST \
  (--problem TEXT | --problem-file FILE) --llm-egress-cidr CIDR [options]

options:
  --count N              number of concurrent identical cells (default 8, max 32)
  --namespace NS         cell namespace (default clawbox-benchmarks)
  --profile P            small|medium|large (default small)
  --prefix NAME          cell name prefix (default single)
  --stagger-seconds S    delay between cell creates (default 2)
  --no-prepull           skip the devmapper pre-pull step
  --wait-seconds W       wait up to W seconds for terminal phases (default 0 = no wait)
  --delete               delete all created cells after the summary
EOF
  exit 64
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tool-image) TOOL_IMAGE="${2:-}"; shift 2 ;;
    --problem) PROBLEM="${2:-}"; shift 2 ;;
    --problem-file) PROBLEM_FILE="${2:-}"; shift 2 ;;
    --llm-egress-cidr) LLM_CIDR="${2:-}"; shift 2 ;;
    --count) COUNT="${2:-}"; shift 2 ;;
    --namespace) NAMESPACE="${2:-}"; shift 2 ;;
    --profile) PROFILE="${2:-}"; shift 2 ;;
    --prefix) PREFIX="${2:-}"; shift 2 ;;
    --stagger-seconds) STAGGER="${2:-}"; shift 2 ;;
    --no-prepull) PREPULL=0; shift ;;
    --wait-seconds) WAIT_SECONDS="${2:-}"; shift 2 ;;
    --delete) DO_DELETE=1; shift ;;
    *) usage ;;
  esac
done

valid_cidr() { python3 -c 'import ipaddress,sys; ipaddress.ip_network(sys.argv[1], strict=True)' "$1" >/dev/null 2>&1; }
valid_name() { [[ "$1" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#1} -le 48 ]]; }

[[ "${TOOL_IMAGE}" =~ @sha256:[a-f0-9]{64}$ ]] \
  || { echo "immutable tool image digest is required (IMAGE@sha256:...)" >&2; exit 64; }
valid_cidr "${LLM_CIDR}" || { echo "invalid LLM egress CIDR" >&2; exit 64; }
[[ "${PROFILE}" =~ ^(small|medium|large)$ ]] || usage
[[ "${COUNT}" =~ ^[1-9][0-9]*$ && "${COUNT}" -le 32 ]] || { echo "--count must be 1..32" >&2; exit 64; }
valid_name "${PREFIX}" || { echo "invalid --prefix" >&2; exit 64; }
valid_name "${NAMESPACE}" || { echo "invalid --namespace" >&2; exit 64; }
[[ -n "${PROBLEM}" || -n "${PROBLEM_FILE}" ]] || usage
if [[ -n "${PROBLEM_FILE}" ]]; then
  [[ -f "${PROBLEM_FILE}" ]] || { echo "problem file is missing" >&2; exit 66; }
fi

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CELL="${ROOT}/deploy/cell.sh"
DEVSTATUS="${ROOT}/scripts/setup-devmapper-openeuler-arm64.sh"

devmapper_status() {
  echo "== devmapper status =="
  sudo bash "${DEVSTATUS}" status || true
}

# 1. pre-pull once into the devmapper snapshotter. Without this, a burst of
#    Pods pulling the same image concurrently can hit the known
#    "target snapshot already exists" unpack race and stall ContainerCreating.
if [[ "${PREPULL}" == 1 ]]; then
  echo "== pre-pulling ${TOOL_IMAGE} into the devmapper snapshotter =="
  # Best-effort: containerd 2.x ctr's transfer unpacker derives unpack
  # platforms from the manifest index, so single-platform (non-index) images
  # fail with "no unpack platforms defined" even with --platform. The cell
  # controller's budgeted, serialized admission already prevents the
  # same-image concurrent unpack race, so a failed pre-pull is not fatal.
  if ! err="$(sudo ctr -n k8s.io images pull --snapshotter devmapper \
      --platform linux/arm64 "${TOOL_IMAGE}" 2>&1)"; then
    echo "WARNING: ctr pre-pull failed (single-platform manifest); relying on serialized cell admission:" >&2
    printf '%s\n' "${err}" | sed 's/^/  /' >&2
  fi
fi

devmapper_status

# 2. create N identical cells with unique names, staggered
# SandboxTask spec is immutable, so a leftover terminal task makes kubectl
# apply a no-op; delete any stale cells with the target names first.
for ((i = 1; i <= COUNT; i++)); do
  stale="${PREFIX}-$(printf '%03d' "${i}")"
  kubectl -n "${NAMESPACE}" delete sandboxtask "${stale}" \
    --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
done
names=()
for ((i = 1; i <= COUNT; i++)); do
  name="${PREFIX}-$(printf '%03d' "${i}")"
  names+=("${name}")
  echo "== creating ${name} (${i}/${COUNT}) =="
  if [[ -n "${PROBLEM_FILE}" ]]; then
    bash "${CELL}" deploy --task "${name}" --tool-image "${TOOL_IMAGE}" \
      --problem-file "${PROBLEM_FILE}" --llm-egress-cidr "${LLM_CIDR}" \
      --namespace "${NAMESPACE}" --profile "${PROFILE}"
  else
    bash "${CELL}" deploy --task "${name}" --tool-image "${TOOL_IMAGE}" \
      --problem "${PROBLEM}" --llm-egress-cidr "${LLM_CIDR}" \
      --namespace "${NAMESPACE}" --profile "${PROFILE}"
  fi
  if (( STAGGER > 0 && i < COUNT )); then sleep "${STAGGER}"; fi
done

echo "== created ${COUNT} cells: ${names[*]} =="
devmapper_status

# 3. optionally wait for terminal phases
TERMINAL='Succeeded|Failed|TimedOut|Cleaned'
if (( WAIT_SECONDS > 0 )); then
  deadline=$(( $(date +%s) + WAIT_SECONDS ))
  while :; do
    pending=0
    for name in "${names[@]}"; do
      phase="$(kubectl -n "${NAMESPACE}" get sandboxtask "${name}" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
      if [[ -z "${phase}" || ! "${phase}" =~ ^(${TERMINAL})$ ]]; then pending=$((pending + 1)); fi
    done
    if (( pending == 0 )); then
      echo "== all cells reached a terminal phase =="
      break
    fi
    if (( $(date +%s) >= deadline )); then
      echo "WARNING: wait budget exhausted; ${pending} cell(s) not terminal" >&2
      break
    fi
    sleep 10
  done
fi

kubectl -n "${NAMESPACE}" get sandboxtasks \
  -l app.kubernetes.io/name=clawbox-cell 2>/dev/null || true

# 4. summary: every cell must reach a successful terminal phase
failed=0
for name in "${names[@]}"; do
  phase="$(kubectl -n "${NAMESPACE}" get sandboxtask "${name}" -o jsonpath='{.status.phase}' 2>/dev/null || echo Missing)"
  echo "cell ${name}: ${phase}"
  case "${phase}" in
    Succeeded|Cleaned) ;;
    *) failed=$((failed + 1)) ;;
  esac
done

if (( DO_DELETE == 1 )); then
  echo "== deleting cells =="
  for name in "${names[@]}"; do
    bash "${CELL}" delete --task "${name}" --namespace "${NAMESPACE}" || true
  done
fi

if (( failed > 0 )); then
  echo "RESULT: ${failed}/${COUNT} cells did not reach a successful terminal phase" >&2
  exit 1
fi
echo "RESULT: all ${COUNT} cells reached a successful terminal phase"
