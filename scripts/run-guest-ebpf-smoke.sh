#!/usr/bin/env bash
# Run ClawTune's real native collector inside a debug-kernel Firecracker guest.
set -euo pipefail

RUNTIME_CLASS="${CLAWBOX_EBPF_RUNTIME_CLASS:-kata-fc-arm64-ebpf}"
IMAGE="${CLAWBOX_EBPF_IMAGE:-127.0.0.1:5000/clawbox/tool-telemetry-research:dev}"
NAMESPACE="${CLAWBOX_EBPF_NAMESPACE:-clawbox-ebpf-smoke-${RANDOM:-0}}"
POD="guest-ebpf-smoke"
OUTPUT="${CLAWBOX_EBPF_EVIDENCE:-}"
KEEP=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) IMAGE="${2:-}"; shift 2 ;;
    --namespace) NAMESPACE="${2:-}"; shift 2 ;;
    --output) OUTPUT="${2:-}"; shift 2 ;;
    --keep) KEEP=true; shift ;;
    -h|--help)
      echo "usage: run-guest-ebpf-smoke.sh [--image IMAGE] [--namespace NAME] [--output FILE] [--keep]"
      exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

[[ "${RUNTIME_CLASS}" == kata-fc-arm64-ebpf ]] || {
  echo "only kata-fc-arm64-ebpf is accepted" >&2
  exit 64
}
command -v kubectl >/dev/null 2>&1 || { echo "kubectl is required" >&2; exit 69; }

COMMAND='set +e
mkdir -p /evidence
/opt/clawtune/venv/bin/python /opt/clawtune-guest/tools/check_guest_ebpf.py \
  --artifact /evidence/clause-telemetry-v2.json \
  --report /evidence/guest-ebpf-report.json
rc=$?
echo "==== GUEST REPORT ===="
cat /evidence/guest-ebpf-report.json 2>/dev/null || true
echo "==== ARTIFACT SHA256 ===="
sha256sum /evidence/clause-telemetry-v2.json 2>/dev/null || true
echo "==== ARTIFACT BASE64 ===="
base64 /evidence/clause-telemetry-v2.json 2>/dev/null || true
echo "==== END ARTIFACT BASE64 ===="
echo "SMOKE_RC=$rc"
exit "$rc"'
COMMAND_B64="$(printf '%s' "${COMMAND}" | base64 | tr -d '\n')"

cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
  labels: {app.kubernetes.io/managed-by: clawbox-ebpf-smoke}
---
apiVersion: v1
kind: Pod
metadata:
  name: ${POD}
  namespace: ${NAMESPACE}
spec:
  runtimeClassName: ${RUNTIME_CLASS}
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsUser: 0
    runAsGroup: 0
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: smoke
      image: ${IMAGE}
      imagePullPolicy: Always
      command: ["/bin/sh", "-c"]
      args: ["printf %s '${COMMAND_B64}' | base64 -d | /bin/sh"]
      securityContext:
        capabilities:
          add: [SYS_ADMIN, NET_ADMIN, NET_RAW, SYS_PTRACE]
EOF

cleanup() {
  if [[ "${KEEP}" != true ]]; then
    kubectl delete namespace "${NAMESPACE}" --wait=true --timeout=120s >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

echo "namespace=${NAMESPACE} pod=${POD} image=${IMAGE}" >&2
deadline=$((SECONDS + 300))
while (( SECONDS < deadline )); do
  phase="$(kubectl -n "${NAMESPACE}" get "pod/${POD}" \
    -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  case "${phase}" in
    Succeeded|Failed) break ;;
  esac
  sleep 1
done
logs="$(kubectl -n "${NAMESPACE}" logs "pod/${POD}" --tail=-1 2>&1 || true)"
if [[ -n "${OUTPUT}" ]]; then
  mkdir -p "$(dirname "${OUTPUT}")"
  printf '%s\n' "${logs}" >"${OUTPUT}"
fi
printf '%s\n' "${logs}"
grep -q '^SMOKE_RC=0$' <<<"${logs}"
