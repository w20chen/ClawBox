#!/usr/bin/env bash
set -euo pipefail

TENANT_ID=""
OTHER_TENANT_ID=""
NAMESPACE="default"
VERIFY_DELETE=false

usage() {
  echo "usage: smoke-test.sh --tenant TENANT [--other-tenant TENANT] [--namespace NS] [--verify-delete]" >&2
  exit 64
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant) TENANT_ID="${2:-}"; shift 2 ;;
    --other-tenant) OTHER_TENANT_ID="${2:-}"; shift 2 ;;
    --namespace) NAMESPACE="${2:-}"; shift 2 ;;
    --verify-delete) VERIFY_DELETE=true; shift ;;
    *) usage ;;
  esac
done

[[ "${TENANT_ID}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#TENANT_ID} -le 40 ]] || usage
if [[ -n "${OTHER_TENANT_ID}" ]]; then
  [[ "${OTHER_TENANT_ID}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#OTHER_TENANT_ID} -le 40 ]] || usage
fi
command -v kubectl >/dev/null 2>&1 || { echo "kubectl not found" >&2; exit 69; }

selector_base="app.kubernetes.io/name=claw-two-sandbox,claw.openai.com/tenant-id=${TENANT_ID}"
kubectl -n "${NAMESPACE}" rollout status "deployment/claw-${TENANT_ID}-tool" --timeout=180s
kubectl -n "${NAMESPACE}" rollout status "deployment/claw-${TENANT_ID}-runtime" --timeout=300s

runtime_pod="$(kubectl -n "${NAMESPACE}" get pod -l "${selector_base},app.kubernetes.io/component=runtime" -o jsonpath='{.items[0].metadata.name}')"
tool_pod="$(kubectl -n "${NAMESPACE}" get pod -l "${selector_base},app.kubernetes.io/component=tool" -o jsonpath='{.items[0].metadata.name}')"
[[ -n "${runtime_pod}" && -n "${tool_pod}" && "${runtime_pod}" != "${tool_pod}" ]]

runtime_host="$(kubectl -n "${NAMESPACE}" exec "${runtime_pod}" -- hostname)"
tool_host="$(kubectl -n "${NAMESPACE}" exec "${tool_pod}" -- hostname)"
[[ "${runtime_host}" != "${tool_host}" ]]

runtime_pid_ns="$(kubectl -n "${NAMESPACE}" exec "${runtime_pod}" -- readlink /proc/self/ns/pid)"
tool_pid_ns="$(kubectl -n "${NAMESPACE}" exec "${tool_pod}" -- readlink /proc/self/ns/pid)"
[[ "${runtime_pid_ns}" != "${tool_pid_ns}" ]]

kubectl -n "${NAMESPACE}" exec "${runtime_pod}" -- test ! -S /var/run/docker.sock
kubectl -n "${NAMESPACE}" exec "${tool_pod}" -- test ! -S /var/run/docker.sock

if kubectl -n "${NAMESPACE}" exec "${tool_pod}" -- /bin/bash -ec \
  'env | grep -Eq "^(OPENAI_API_KEY|OPENCLAW_GATEWAY_TOKEN|OPENCLAW_TOKEN)="'; then
  echo "Tool Pod contains a forbidden long-lived credential" >&2
  exit 1
fi

model="$(kubectl -n "${NAMESPACE}" exec "${runtime_pod}" -- /bin/bash -ec 'printf %s "$OPENCLAW_MODEL"')"
exec_prompt="Use the exec tool exactly once. Run this exact command and return its stdout verbatim: hostname; printf 'created-by-tool\\n' > exec-proof.txt; cat exec-proof.txt"
exec_json="$(kubectl -n "${NAMESPACE}" exec "${runtime_pod}" -- env CLAW_REPO_KEY="tenant-${TENANT_ID}" \
  openclaw agent --local --agent main --session-id "smoke-exec-${TENANT_ID}" \
  --model "vllm/${model}" --message "${exec_prompt}" --timeout 180 --json)"
printf '%s\n' "${exec_json}" | grep -F "${tool_host}" >/dev/null
printf '%s\n' "${exec_json}" | grep -F 'created-by-tool' >/dev/null

file_prompt="Use these tools in order on file-tools-proof.txt: write with content alpha, read it, edit alpha to beta, apply_patch changing beta to gamma, then read it. Do not use exec for file changes. Return the final content."
file_json="$(kubectl -n "${NAMESPACE}" exec "${runtime_pod}" -- env CLAW_REPO_KEY="tenant-${TENANT_ID}" \
  openclaw agent --local --agent main --session-id "smoke-files-${TENANT_ID}" \
  --model "vllm/${model}" --message "${file_prompt}" --timeout 240 --json)"
printf '%s\n' "${file_json}" | grep -F 'gamma' >/dev/null

tool_file="$(kubectl -n "${NAMESPACE}" exec "${tool_pod}" -- /bin/bash -ec \
  'find /tmp/openclaw-sandboxes -type f -name file-tools-proof.txt -print -quit')"
[[ -n "${tool_file}" ]]
[[ "$(kubectl -n "${NAMESPACE}" exec "${tool_pod}" -- cat "${tool_file}")" == *gamma* ]]
kubectl -n "${NAMESPACE}" exec "${runtime_pod}" -- test ! -e /workspace/file-tools-proof.txt
kubectl -n "${NAMESPACE}" exec "${runtime_pod}" -- test ! -e /workspace/exec-proof.txt

for tool_name in write read edit apply_patch; do
  kubectl -n "${NAMESPACE}" exec "${runtime_pod}" -- /bin/bash -ec \
    "grep -R -E '\"(tool_name|toolName)\"[[:space:]]*:[[:space:]]*\"${tool_name}\"' /state/${TENANT_ID}/traces >/dev/null"
done

if [[ -n "${OTHER_TENANT_ID}" ]]; then
  if kubectl -n "${NAMESPACE}" exec "${runtime_pod}" -- /bin/bash -ec \
    "timeout 4 bash -c '</dev/tcp/claw-${OTHER_TENANT_ID}-tool/2222'" 2>/dev/null; then
    echo "cross-tenant SSH unexpectedly succeeded" >&2
    exit 1
  fi
else
  echo "SKIP cross-tenant check: pass --other-tenant with a deployed second cell" >&2
fi

runtime_class="$(kubectl -n "${NAMESPACE}" get pod "${runtime_pod}" -o jsonpath='{.spec.runtimeClassName}')"
tool_runtime_class="$(kubectl -n "${NAMESPACE}" get pod "${tool_pod}" -o jsonpath='{.spec.runtimeClassName}')"
echo "PASS tenant=${TENANT_ID} runtime_pod=${runtime_pod} tool_pod=${tool_pod} runtimeClass=${runtime_class:-default} toolRuntimeClass=${tool_runtime_class:-default}"
echo "PASS hostnames runtime=${runtime_host} tool=${tool_host}; pid_namespaces runtime=${runtime_pid_ns} tool=${tool_pid_ns}"

if [[ "${VERIFY_DELETE}" == true ]]; then
  script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
  bash "${script_dir}/cell.sh" delete --tenant "${TENANT_ID}" --namespace "${NAMESPACE}"
  [[ -z "$(kubectl -n "${NAMESPACE}" get deployment,service,networkpolicy -l "${selector_base}" -o name)" ]]
  echo "PASS tenant cleanup"
fi
