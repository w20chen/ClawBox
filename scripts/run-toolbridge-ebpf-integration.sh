#!/usr/bin/env bash
# Exercise the actual SSH Tool Bridge lifecycle with native eBPF in Firecracker.
set -euo pipefail

RUNTIME_CLASS="${CLAWBOX_EBPF_RUNTIME_CLASS:-kata-fc-arm64-ebpf}"
IMAGE="${CLAWBOX_EBPF_IMAGE:-127.0.0.1:5000/clawbox/tool-telemetry-research:dev}"
NAMESPACE="${CLAWBOX_EBPF_NAMESPACE:-clawbox-toolbridge-ebpf-${RANDOM:-0}}"
OUTPUT="${CLAWBOX_EBPF_EVIDENCE:-}"
KEEP=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) IMAGE="${2:-}"; shift 2 ;;
    --namespace) NAMESPACE="${2:-}"; shift 2 ;;
    --output) OUTPUT="${2:-}"; shift 2 ;;
    --keep) KEEP=true; shift ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done
[[ "${RUNTIME_CLASS}" == kata-fc-arm64-ebpf ]] || exit 64

COMMAND='set -e
root=/testbed/.clawbox
keys=/tmp/tool-keys
mkdir -p "$root/tool-resource" "$keys" /run/clawtune
ssh-keygen -q -t ed25519 -N "" -f "$keys/client"
ssh-keygen -q -t ed25519 -N "" -f "$keys/host"
export TOOL_BRIDGE_LISTEN=127.0.0.1:2222
export TOOL_BRIDGE_WORKDIR=/testbed
export TOOL_BRIDGE_LOG_PATH="$root/tool-bridge.jsonl"
export TOOL_BRIDGE_HOST_KEY="$keys/host"
export TOOL_BRIDGE_AUTHORIZED_KEY="$keys/client.pub"
export TOOL_EXEC_TIMEOUT_SECONDS=3
export TOOL_MAX_CONCURRENCY=4
export CLAWTUNE_GUEST_ARTIFACT_ROOT="$root/tool-resource"
export CLAWBOX_REPOSITORY=clawbox/toolbridge-integration
/usr/local/bin/tool-bridge >/tmp/tool-bridge.stdout 2>/tmp/tool-bridge.stderr &
bridge=$!
cleanup_local() { rc=$?; kill "$bridge" 2>/dev/null || true; if [[ $rc != 0 ]]; then echo ==== FAILURE TOOL BRIDGE STDERR ====; cat /tmp/tool-bridge.stderr 2>/dev/null || true; echo ==== FAILURE ARTIFACT VALIDATION ====; /opt/clawtune/venv/bin/python /opt/clawbox/validate-toolbridge-guest-artifacts.py "$root" 2>&1 || true; fi; exit "$rc"; }
trap cleanup_local EXIT
ready=false
for _ in $(seq 1 300); do
  if ssh-keyscan -T 1 -p 2222 127.0.0.1 >/dev/null 2>&1; then ready=true; break; fi
  kill -0 "$bridge" 2>/dev/null || break
  sleep .1
done
[[ "$ready" == true ]]
ssh_base="-n -q -i $keys/client -p 2222 -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null executor@127.0.0.1"
run_exec() { id=$1; command=$2; payload=$(printf "__CBX_EXEC_1__%s\n%s" "$id" "$command"); ssh $ssh_base "$payload"; }
run_exec exec-long "dd if=/dev/zero of=/dev/null bs=1M count=32768" >/tmp/long.out
[[ $(run_exec exec-pipeline "seq 2 | wc -l") == 2 ]]
set +e
run_exec exec-exit "exit 7" >/dev/null 2>&1; exit_rc=$?
run_exec exec-timeout "sleep 5" >/dev/null 2>&1; timeout_rc=$?
set -e
echo "EXIT_RC=$exit_rc TIMEOUT_RC=$timeout_rc"
[[ $exit_rc == 7 && $timeout_rc == 124 ]]
run_exec exec-concurrent-a "dd if=/dev/zero of=/dev/null bs=1M count=16383" >/tmp/a.out 2>&1 & a=$!
run_exec exec-concurrent-b "dd if=/dev/zero of=/dev/null bs=1M count=16385" >/tmp/b.out 2>&1 & b=$!
wait "$a"; wait "$b"
helper_killed=false
for proc in /proc/[0-9]*; do
  helper_cmd=$(tr "\\000" " " < "$proc/cmdline" 2>/dev/null || true)
  case "$helper_cmd" in
    */opt/clawtune-guest/tools/guest_collector_server.py*) kill "${proc##*/}" 2>/dev/null || true; helper_killed=true ;;
  esac
done
[[ "$helper_killed" == true ]]
sleep .2
set +e
failopen_output=$(run_exec exec-helper-failopen "printf helper-fail-open")
failopen_rc=$?
set -e
echo "FAILOPEN_RC=$failopen_rc OUTPUT=$failopen_output BRIDGE_ALIVE=$(kill -0 "$bridge" 2>/dev/null && echo yes || echo no)"
[[ $failopen_rc == 0 && "$failopen_output" == helper-fail-open ]]
kill "$bridge" 2>/dev/null || true
wait "$bridge" 2>/dev/null || true
/opt/clawtune/venv/bin/python /opt/clawbox/validate-toolbridge-guest-artifacts.py "$root"
echo ==== TOOL BRIDGE STDERR ====
tail -n 80 /tmp/tool-bridge.stderr
echo INTEGRATION_RC=0'
COMMAND_B64="$(printf '%s' "${COMMAND}" | base64 | tr -d '\n')"

cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Namespace
metadata: {name: ${NAMESPACE}}
---
apiVersion: v1
kind: Pod
metadata: {name: toolbridge-ebpf-integration, namespace: ${NAMESPACE}}
spec:
  runtimeClassName: ${RUNTIME_CLASS}
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsUser: 0
    runAsGroup: 0
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: integration
      image: ${IMAGE}
      imagePullPolicy: Always
      command: ["/bin/sh", "-c"]
      args: ["printf %s '${COMMAND_B64}' | base64 -d | /bin/bash"]
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
deadline=$((SECONDS + 360))
while (( SECONDS < deadline )); do
  phase="$(kubectl -n "${NAMESPACE}" get pod/toolbridge-ebpf-integration -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  case "${phase}" in Succeeded|Failed) break ;; esac
  sleep 1
done
logs="$(kubectl -n "${NAMESPACE}" logs pod/toolbridge-ebpf-integration --tail=-1 2>&1 || true)"
[[ -z "${OUTPUT}" ]] || { mkdir -p "$(dirname "${OUTPUT}")"; printf '%s\n' "${logs}" >"${OUTPUT}"; }
printf '%s\n' "${logs}"
grep -q '^INTEGRATION_RC=0$' <<<"${logs}"
