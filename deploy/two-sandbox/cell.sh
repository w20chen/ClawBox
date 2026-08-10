#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/cell.yaml"

usage() {
  cat >&2 <<'EOF'
Usage:
  cell.sh deploy --tenant TENANT --runtime-image IMAGE --tool-image IMAGE \
    --llm-secret SECRET --llm-egress-cidr CIDR [options]
  cell.sh render --tenant TENANT --runtime-image IMAGE --tool-image IMAGE \
    --llm-secret SECRET --llm-egress-cidr CIDR [options]
  cell.sh delete --tenant TENANT [--namespace NAMESPACE]

Options:
  --ssh-secret SECRET          Existing Secret with client id_ed25519 keypair and
                               ssh_host_ed25519_key keypair.
                               If omitted, deploy generates a demo key Secret.
  --runtime-class NAME         Runtime RuntimeClass; omit for cluster default.
  --tool-runtime-class NAME    Tool RuntimeClass; omit for cluster default.
  --llm-egress-port PORT       Default: 443.
  --tool-egress-cidr CIDR      Optional Tool internet egress; default is denied.
  --tool-egress-port PORT      Default: 443.
  --namespace NAME             Default: default.
EOF
  exit 64
}

[[ $# -ge 1 ]] || usage
ACTION="$1"
shift

TENANT_ID=""
RUNTIME_IMAGE=""
TOOL_IMAGE=""
LLM_SECRET_NAME=""
LLM_EGRESS_CIDR=""
LLM_EGRESS_PORT="443"
SSH_SECRET_NAME=""
RUNTIME_CLASS=""
TOOL_RUNTIME_CLASS=""
TOOL_EGRESS_CIDR=""
TOOL_EGRESS_PORT="443"
NAMESPACE="default"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant) TENANT_ID="${2:-}"; shift 2 ;;
    --runtime-image) RUNTIME_IMAGE="${2:-}"; shift 2 ;;
    --tool-image) TOOL_IMAGE="${2:-}"; shift 2 ;;
    --llm-secret) LLM_SECRET_NAME="${2:-}"; shift 2 ;;
    --llm-egress-cidr) LLM_EGRESS_CIDR="${2:-}"; shift 2 ;;
    --llm-egress-port) LLM_EGRESS_PORT="${2:-}"; shift 2 ;;
    --ssh-secret) SSH_SECRET_NAME="${2:-}"; shift 2 ;;
    --runtime-class) RUNTIME_CLASS="${2:-}"; shift 2 ;;
    --tool-runtime-class) TOOL_RUNTIME_CLASS="${2:-}"; shift 2 ;;
    --tool-egress-cidr) TOOL_EGRESS_CIDR="${2:-}"; shift 2 ;;
    --tool-egress-port) TOOL_EGRESS_PORT="${2:-}"; shift 2 ;;
    --namespace) NAMESPACE="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

valid_name() { [[ "$1" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#1} -le 40 ]]; }
valid_resource_name() { [[ "$1" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ && ${#1} -le 63 ]]; }
valid_image() { [[ "$1" =~ ^[A-Za-z0-9._/@:-]+$ ]]; }
valid_cidr() {
  [[ "$1" =~ ^[0-9A-Fa-f:.]+/[0-9]{1,3}$ && "$1" != "0.0.0.0/0" && "$1" != "::/0" ]]
}
valid_port() { [[ "$1" =~ ^[0-9]+$ ]] && (( 1 <= 10#$1 && 10#$1 <= 65535 )); }

[[ -n "${TENANT_ID}" ]] || { echo "--tenant is required" >&2; usage; }
valid_name "${TENANT_ID}" || { echo "tenant must be a lowercase DNS label (max 40 chars)" >&2; exit 64; }
valid_resource_name "${NAMESPACE}" || { echo "invalid namespace" >&2; exit 64; }

if [[ "${ACTION}" == "delete" ]]; then
  command -v kubectl >/dev/null 2>&1 || { echo "kubectl not found" >&2; exit 69; }
  selector="app.kubernetes.io/managed-by=claw-two-sandbox,claw.openai.com/tenant-id=${TENANT_ID}"
  kubectl -n "${NAMESPACE}" delete deployment,service,networkpolicy -l "${selector}" --ignore-not-found
  kubectl -n "${NAMESPACE}" delete secret -l "${selector},claw.openai.com/demo-key=true" --ignore-not-found
  echo "tenant cell deleted: namespace=${NAMESPACE} tenant_id=${TENANT_ID}"
  exit 0
fi

[[ "${ACTION}" == "deploy" || "${ACTION}" == "render" ]] || usage
[[ -n "${RUNTIME_IMAGE}" && -n "${TOOL_IMAGE}" && -n "${LLM_SECRET_NAME}" && -n "${LLM_EGRESS_CIDR}" ]] || usage
valid_image "${RUNTIME_IMAGE}" || { echo "invalid runtime image reference" >&2; exit 64; }
valid_image "${TOOL_IMAGE}" || { echo "invalid tool image reference" >&2; exit 64; }
valid_resource_name "${LLM_SECRET_NAME}" || { echo "invalid LLM Secret name" >&2; exit 64; }
valid_cidr "${LLM_EGRESS_CIDR}" || { echo "invalid LLM egress CIDR" >&2; exit 64; }
valid_port "${LLM_EGRESS_PORT}" || { echo "invalid LLM egress port" >&2; exit 64; }
[[ -z "${RUNTIME_CLASS}" ]] || valid_resource_name "${RUNTIME_CLASS}" || { echo "invalid Runtime RuntimeClass" >&2; exit 64; }
[[ -z "${TOOL_RUNTIME_CLASS}" ]] || valid_resource_name "${TOOL_RUNTIME_CLASS}" || { echo "invalid Tool RuntimeClass" >&2; exit 64; }
[[ -z "${TOOL_EGRESS_CIDR}" ]] || valid_cidr "${TOOL_EGRESS_CIDR}" || { echo "invalid Tool egress CIDR" >&2; exit 64; }
valid_port "${TOOL_EGRESS_PORT}" || { echo "invalid Tool egress port" >&2; exit 64; }

RUNTIME_ID="runtime-${TENANT_ID}"
SSH_SECRET_GENERATED=false
SSH_SECRET_CREATED=false
if [[ -z "${SSH_SECRET_NAME}" ]]; then
  SSH_SECRET_NAME="claw-${TENANT_ID}-ssh"
  SSH_SECRET_GENERATED=true
else
  valid_resource_name "${SSH_SECRET_NAME}" || { echo "invalid SSH Secret name" >&2; exit 64; }
fi

RUNTIME_RUNTIME_CLASS_LINE=""
TOOL_RUNTIME_CLASS_LINE=""
[[ -z "${RUNTIME_CLASS}" ]] || RUNTIME_RUNTIME_CLASS_LINE="runtimeClassName: ${RUNTIME_CLASS}"
[[ -z "${TOOL_RUNTIME_CLASS}" ]] || TOOL_RUNTIME_CLASS_LINE="runtimeClassName: ${TOOL_RUNTIME_CLASS}"

TOOL_EGRESS_POLICY=""
if [[ -n "${TOOL_EGRESS_CIDR}" ]]; then
  TOOL_EGRESS_POLICY="---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: claw-${TENANT_ID}-tool-egress
  labels:
    app.kubernetes.io/name: claw-two-sandbox
    app.kubernetes.io/managed-by: claw-two-sandbox
    claw.openai.com/tenant-id: ${TENANT_ID}
    claw.openai.com/runtime-id: ${RUNTIME_ID}
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: claw-two-sandbox
      app.kubernetes.io/component: tool
      claw.openai.com/tenant-id: ${TENANT_ID}
  policyTypes: [\"Egress\"]
  egress:
    - to:
        - namespaceSelector: {}
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - {port: 53, protocol: UDP}
        - {port: 53, protocol: TCP}
    - to:
        - ipBlock: {cidr: ${TOOL_EGRESS_CIDR}}
      ports:
        - {port: ${TOOL_EGRESS_PORT}, protocol: TCP}"
fi

export TENANT_ID RUNTIME_ID RUNTIME_IMAGE TOOL_IMAGE LLM_SECRET_NAME LLM_EGRESS_CIDR LLM_EGRESS_PORT
export SSH_SECRET_NAME RUNTIME_RUNTIME_CLASS_LINE TOOL_RUNTIME_CLASS_LINE TOOL_EGRESS_POLICY
export TOOL_EXEC_TIMEOUT_SECONDS="${TOOL_EXEC_TIMEOUT_SECONDS:-300}" TOOL_PIDS_LIMIT="${TOOL_PIDS_LIMIT:-128}"
export TOOL_CPU_REQUEST="${TOOL_CPU_REQUEST:-250m}" TOOL_CPU_LIMIT="${TOOL_CPU_LIMIT:-1}"
export TOOL_MEMORY_REQUEST="${TOOL_MEMORY_REQUEST:-256Mi}" TOOL_MEMORY_LIMIT="${TOOL_MEMORY_LIMIT:-1Gi}"
export TOOL_STORAGE_REQUEST="${TOOL_STORAGE_REQUEST:-256Mi}" TOOL_STORAGE_LIMIT="${TOOL_STORAGE_LIMIT:-1Gi}"
export RUNTIME_CPU_REQUEST="${RUNTIME_CPU_REQUEST:-500m}" RUNTIME_CPU_LIMIT="${RUNTIME_CPU_LIMIT:-2}"
export RUNTIME_MEMORY_REQUEST="${RUNTIME_MEMORY_REQUEST:-512Mi}" RUNTIME_MEMORY_LIMIT="${RUNTIME_MEMORY_LIMIT:-2Gi}"
export RUNTIME_STORAGE_REQUEST="${RUNTIME_STORAGE_REQUEST:-512Mi}" RUNTIME_STORAGE_LIMIT="${RUNTIME_STORAGE_LIMIT:-2Gi}"

command -v envsubst >/dev/null 2>&1 || { echo "envsubst not found (install gettext/gettext-base)" >&2; exit 69; }
render() {
  envsubst '${TENANT_ID} ${RUNTIME_ID} ${RUNTIME_IMAGE} ${TOOL_IMAGE} ${LLM_SECRET_NAME} ${LLM_EGRESS_CIDR} ${LLM_EGRESS_PORT} ${SSH_SECRET_NAME} ${RUNTIME_RUNTIME_CLASS_LINE} ${TOOL_RUNTIME_CLASS_LINE} ${TOOL_EGRESS_POLICY} ${TOOL_EXEC_TIMEOUT_SECONDS} ${TOOL_PIDS_LIMIT} ${TOOL_CPU_REQUEST} ${TOOL_CPU_LIMIT} ${TOOL_MEMORY_REQUEST} ${TOOL_MEMORY_LIMIT} ${TOOL_STORAGE_REQUEST} ${TOOL_STORAGE_LIMIT} ${RUNTIME_CPU_REQUEST} ${RUNTIME_CPU_LIMIT} ${RUNTIME_MEMORY_REQUEST} ${RUNTIME_MEMORY_LIMIT} ${RUNTIME_STORAGE_REQUEST} ${RUNTIME_STORAGE_LIMIT}' <"${TEMPLATE}"
}

if [[ "${ACTION}" == "render" ]]; then
  render
  exit 0
fi

command -v kubectl >/dev/null 2>&1 || { echo "kubectl not found" >&2; exit 69; }
secret_has_key() {
  [[ "$(kubectl -n "${NAMESPACE}" get secret "$1" -o "go-template={{if index .data \"$2\"}}present{{end}}")" == "present" ]]
}
kubectl -n "${NAMESPACE}" get secret "${LLM_SECRET_NAME}" >/dev/null
for key in openai-base-url openai-api-key openclaw-model; do
  secret_has_key "${LLM_SECRET_NAME}" "${key}" || {
    echo "LLM Secret ${LLM_SECRET_NAME} is missing key ${key}" >&2; exit 65;
  }
done

for name in "claw-${TENANT_ID}-runtime" "claw-${TENANT_ID}-tool"; do
  existing_tenant="$(kubectl -n "${NAMESPACE}" get deployment "${name}" -o 'jsonpath={.metadata.labels.claw\.openai\.com/tenant-id}' 2>/dev/null || true)"
  [[ -z "${existing_tenant}" || "${existing_tenant}" == "${TENANT_ID}" ]] || {
    echo "refusing to overwrite deployment ${name} owned by tenant ${existing_tenant}" >&2; exit 73;
  }
done

tmp_dir="$(mktemp -d)"
cleanup_tmp() { rm -rf -- "${tmp_dir}"; }
trap cleanup_tmp EXIT

if [[ "${SSH_SECRET_GENERATED}" == true ]]; then
  existing_secret="$(kubectl -n "${NAMESPACE}" get secret "${SSH_SECRET_NAME}" -o name 2>/dev/null || true)"
  if [[ -n "${existing_secret}" ]]; then
    existing_tenant="$(kubectl -n "${NAMESPACE}" get secret "${SSH_SECRET_NAME}" -o 'jsonpath={.metadata.labels.claw\.openai\.com/tenant-id}')"
    existing_demo="$(kubectl -n "${NAMESPACE}" get secret "${SSH_SECRET_NAME}" -o 'jsonpath={.metadata.labels.claw\.openai\.com/demo-key}')"
    [[ "${existing_tenant}" == "${TENANT_ID}" && "${existing_demo}" == "true" ]] || {
      echo "refusing to overwrite existing non-demo SSH Secret ${SSH_SECRET_NAME}" >&2; exit 73;
    }
    for key in id_ed25519 id_ed25519.pub ssh_host_ed25519_key ssh_host_ed25519_key.pub; do
      secret_has_key "${SSH_SECRET_NAME}" "${key}" || {
        echo "demo SSH Secret ${SSH_SECRET_NAME} is missing key ${key}" >&2; exit 65;
      }
    done
  else
    command -v ssh-keygen >/dev/null 2>&1 || { echo "ssh-keygen required for demo key generation" >&2; exit 69; }
    ssh-keygen -q -t ed25519 -N '' -C "claw-${TENANT_ID}-demo" -f "${tmp_dir}/id_ed25519"
    ssh-keygen -q -t ed25519 -N '' -C "claw-${TENANT_ID}-host" -f "${tmp_dir}/ssh_host_ed25519_key"
    kubectl -n "${NAMESPACE}" create secret generic "${SSH_SECRET_NAME}" \
      --from-file=id_ed25519="${tmp_dir}/id_ed25519" \
      --from-file=id_ed25519.pub="${tmp_dir}/id_ed25519.pub" \
      --from-file=ssh_host_ed25519_key="${tmp_dir}/ssh_host_ed25519_key" \
      --from-file=ssh_host_ed25519_key.pub="${tmp_dir}/ssh_host_ed25519_key.pub" \
      --dry-run=client -o yaml | kubectl -n "${NAMESPACE}" apply -f -
    kubectl -n "${NAMESPACE}" label secret "${SSH_SECRET_NAME}" --overwrite \
      app.kubernetes.io/managed-by=claw-two-sandbox \
      claw.openai.com/tenant-id="${TENANT_ID}" \
      claw.openai.com/demo-key=true >/dev/null
    SSH_SECRET_CREATED=true
  fi
else
  kubectl -n "${NAMESPACE}" get secret "${SSH_SECRET_NAME}" >/dev/null
  for key in id_ed25519 id_ed25519.pub ssh_host_ed25519_key ssh_host_ed25519_key.pub; do
    secret_has_key "${SSH_SECRET_NAME}" "${key}" || {
      echo "SSH Secret ${SSH_SECRET_NAME} is missing key ${key}" >&2; exit 65;
    }
  done
fi

render >"${tmp_dir}/cell.yaml"
kubectl -n "${NAMESPACE}" apply -f "${tmp_dir}/cell.yaml"
echo "tenant cell applied: namespace=${NAMESPACE} tenant_id=${TENANT_ID} runtime_id=${RUNTIME_ID}"
if [[ "${SSH_SECRET_CREATED}" == true ]]; then
  echo "demo SSH key Secret ${SSH_SECRET_NAME} will be deleted by: cell.sh delete --tenant ${TENANT_ID} --namespace ${NAMESPACE}"
fi
