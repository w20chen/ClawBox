#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ACTION="${1:-}"
[[ -n "${ACTION}" ]] || { echo "usage: cell.sh render|deploy|delete --task NAME ..." >&2; exit 64; }
shift

TASK_NAME=""
TOOL_IMAGE=""
PROBLEM_STATEMENT=""
PROBLEM_FILE=""
BASE_COMMIT=""
HINT_TEXT=""
LLM_SECRET_NAME="clawbox-llm"
LLM_EGRESS_CIDR=""
LLM_EGRESS_PORT="443"
TOOL_EGRESS_CIDRS="[]"
RESOURCE_PROFILE="small"
TASK_TIMEOUT_SECONDS="1800"
TOOL_EXEC_TIMEOUT_SECONDS="300"
TOOL_OUTPUT_LIMIT_BYTES="4194304"
NAMESPACE="clawbox-benchmarks"

usage() {
  cat >&2 <<'EOF'
usage: cell.sh render|deploy --task NAME --tool-image IMAGE@sha256:DIGEST \
  (--problem TEXT | --problem-file FILE) --llm-egress-cidr CIDR [options]
       cell.sh delete --task NAME [--namespace NS]

The Cell Controller owns credentials, NetworkPolicies, Tool Pod and Runtime
Job. Runtime image, Tool Bridge image and kata-fc-arm64 are controller-fixed.
EOF
  exit 64
}

tool_cidrs=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --task) TASK_NAME="${2:-}"; shift 2 ;;
    --tool-image) TOOL_IMAGE="${2:-}"; shift 2 ;;
    --problem) PROBLEM_STATEMENT="${2:-}"; shift 2 ;;
    --problem-file) PROBLEM_FILE="${2:-}"; shift 2 ;;
    --base-commit) BASE_COMMIT="${2:-}"; shift 2 ;;
    --hint) HINT_TEXT="${2:-}"; shift 2 ;;
    --llm-secret) LLM_SECRET_NAME="${2:-}"; shift 2 ;;
    --llm-egress-cidr) LLM_EGRESS_CIDR="${2:-}"; shift 2 ;;
    --llm-egress-port) LLM_EGRESS_PORT="${2:-}"; shift 2 ;;
    --tool-egress-cidr) tool_cidrs+=("${2:-}"); shift 2 ;;
    --profile) RESOURCE_PROFILE="${2:-}"; shift 2 ;;
    --timeout-seconds) TASK_TIMEOUT_SECONDS="${2:-}"; shift 2 ;;
    --namespace) NAMESPACE="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done

valid_name() { [[ "$1" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#1} -le 63 ]]; }
valid_cidr() { python3 -c 'import ipaddress,sys; ipaddress.ip_network(sys.argv[1], strict=True)' "$1" >/dev/null 2>&1; }
valid_name "${TASK_NAME}" && valid_name "${NAMESPACE}" && valid_name "${LLM_SECRET_NAME}" || usage
(( ${#TASK_NAME} <= 48 )) || { echo "task name must be at most 48 characters" >&2; exit 64; }

if [[ "${ACTION}" == delete ]]; then
  kubectl -n "${NAMESPACE}" delete sandboxtask "${TASK_NAME}" --wait=true --timeout=300s
  exit 0
fi
[[ "${ACTION}" == render || "${ACTION}" == deploy ]] || usage
[[ "${TOOL_IMAGE}" =~ @sha256:[a-f0-9]{64}$ ]] || { echo "immutable arm64 tool image digest is required" >&2; exit 64; }
[[ "${RESOURCE_PROFILE}" =~ ^(small|medium|large)$ ]] || usage
valid_cidr "${LLM_EGRESS_CIDR}" || { echo "invalid LLM egress CIDR" >&2; exit 64; }
if [[ -n "${PROBLEM_FILE}" ]]; then
  [[ -f "${PROBLEM_FILE}" ]] || { echo "problem file is missing" >&2; exit 66; }
  PROBLEM_STATEMENT="$(cat "${PROBLEM_FILE}")"
fi
[[ -n "${PROBLEM_STATEMENT}" ]] || usage
for cidr in "${tool_cidrs[@]}"; do valid_cidr "${cidr}" || { echo "invalid Tool egress CIDR: ${cidr}" >&2; exit 64; }; done

PROBLEM_STATEMENT="$(python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' <<<"${PROBLEM_STATEMENT}")"
BASE_COMMIT="$(python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().rstrip("\n")))' <<<"${BASE_COMMIT}")"
HINT_TEXT="$(python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().rstrip("\n")))' <<<"${HINT_TEXT}")"
TOOL_EGRESS_CIDRS="$(printf '%s\n' "${tool_cidrs[@]}" | python3 -c 'import json,sys; print(json.dumps([x.rstrip("\n") for x in sys.stdin if x.rstrip("\n")]))')"
export TASK_NAME TOOL_IMAGE PROBLEM_STATEMENT BASE_COMMIT HINT_TEXT LLM_SECRET_NAME LLM_EGRESS_CIDR
export LLM_EGRESS_PORT TOOL_EGRESS_CIDRS RESOURCE_PROFILE TASK_TIMEOUT_SECONDS TOOL_EXEC_TIMEOUT_SECONDS
export TOOL_OUTPUT_LIMIT_BYTES NAMESPACE
command -v envsubst >/dev/null 2>&1 || { echo "envsubst is required" >&2; exit 69; }
rendered="$(envsubst <"${SCRIPT_DIR}/cell.yaml")"
if [[ "${ACTION}" == render ]]; then printf '%s\n' "${rendered}"; else printf '%s\n' "${rendered}" | kubectl apply -f -; fi
