#!/usr/bin/env bash
# Probe the Kata + Firecracker guest OS from inside a live kata-fc pod.
#
# Answers the feasibility questions for per-execution cgroup v2 + eBPF
# collection inside the Tool VM:
#   * is /sys/fs/cgroup cgroup2fs and writable (create dir + write pid)?
#   * what is the container's own cgroup path (needed for cgroup scoping)?
#   * can we read cpu.stat / memory.current / memory.peak?
#   * is the guest kernel BTF present (/sys/kernel/btf/vmlinux) -> CO-RE eBPF?
#   * are kernel headers present (BCC runtime compilation)?
#   * seccomp / capabilities that gate BPF loading?
#
# Run on the target Kunpeng node:  bash scripts/probe-kata-guest.sh
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_CLASS="${CLAWBOX_RUNTIME_CLASS:-kata-fc-arm64}"
# Default to the loopback registry busybox (kubelet/containerd already trust
# 127.0.0.1:5000); external pulls are blocked/proxied on the target host.
IMAGE="${CLAWBOX_SMOKE_IMAGE:-127.0.0.1:5000/clawbox/test:busybox}"
NAMESPACE="clawbox-guest-probe-${RANDOM:-0}"
KEEP=false
POD="guest-probe"
# Capabilities requested for the probe container (empty = pod defaults).
# The pivotal test: does Kata honour securityContext.capabilities in the
# guest so that cgroup becomes writable and BPF becomes loadable?
# NOTE: this Kata build's shim (serde enum) rejects PERFMON/BPF capability
# names ("no variant for ... PERFMON").  CAP_SYS_ADMIN is still sufficient to
# gate the bpf() syscall and perf_event_open (kernel checks CAP_BPF||CAP_SYS_ADMIN
# and CAP_PERFMON||CAP_SYS_ADMIN), so keep the legacy names only.
CAPABILITIES="${CAPABILITIES:-SYS_ADMIN NET_ADMIN NET_RAW SYS_PTRACE}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) NAMESPACE="${2:-}"; shift 2 ;;
    --image) IMAGE="${2:-}"; shift 2 ;;
    --keep) KEEP=true; shift ;;
    --no-caps) CAPABILITIES=""; shift ;;
    -h|--help)
      echo "usage: probe-kata-guest.sh [--namespace NAME] [--image IMAGE] [--keep] [--no-caps]"
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done

[[ "${RUNTIME_CLASS}" == kata-fc-arm64 ]] || { echo "only kata-fc-arm64 is accepted" >&2; exit 64; }
command -v kubectl >/dev/null 2>&1 || { echo "kubectl is required" >&2; exit 69; }

PROBE='set -x
echo "== cgroup fs type =="
stat -fc %T /sys/fs/cgroup
echo "== self cgroup path =="
cat /proc/self/cgroup
echo "== can we create an execution cgroup? =="
mkdir -p /sys/fs/cgroup/clawbox-probe && echo mkdir-ok
ls /sys/fs/cgroup/clawbox-probe
echo "== own cgroup cpu.stat =="
cat /sys/fs/cgroup/cpu.stat 2>&1 | head -4
echo "== own cgroup memory =="
cat /sys/fs/cgroup/memory.current 2>&1 | head -1
cat /sys/fs/cgroup/memory.peak 2>&1 | head -1
cat /sys/fs/cgroup/memory.events 2>&1 | head -5
echo "== pids =="
cat /sys/fs/cgroup/pids.current 2>&1 | head -1
echo "== io.stat =="
cat /sys/fs/cgroup/io.stat 2>&1 | head -2
echo "== guest kernel =="
uname -r
uname -m
echo "== BTF (CO-RE eBPF) =="
ls -l /sys/kernel/btf/vmlinux 2>&1 || echo "no-btf"
echo "== kernel headers =="
ls /usr/src 2>&1 || echo "no-/usr/src"
ls /lib/modules 2>&1 | head -3 || echo "no-/lib/modules"
echo "== caps / seccomp =="
grep -E "^(Seccomp|NoNewPrivs|CapEff|CapBnd)" /proc/self/status
echo "== perf_event_paranoid =="
cat /proc/sys/kernel/perf_event_paranoid 2>&1 || echo "n/a"
echo "== unprivileged_bpf_disabled =="
cat /proc/sys/kernel/unprivileged_bpf_disabled 2>&1 || echo "n/a"
echo "== kprobe availability (kprobes/blacklist) =="
ls /sys/kernel/debug/tracing/kprobe_events 2>&1 || echo "no-debugfs"
ls /sys/kernel/tracing/kprobe_events 2>&1 || echo "no-tracefs-kprobes"
echo "== cgroup writability retest =="
mkdir -p /sys/fs/cgroup/clawbox-probe 2>&1 && echo mkdir-ok || echo mkdir-fail
echo "== procfs sampling basics =="
ls /proc | grep -E "^[0-9]+$" | head -5
echo "probe-done"'

# Encode the probe as base64 so it survives YAML/ssh quoting as a single
# token; busybox decodes it in the guest and pipes it to /bin/sh.
PROBE_B64="$(printf '%s' "${PROBE}" | base64 | tr -d '\n')"

if [[ -n "${CAPABILITIES}" ]]; then
  # CAPABILITIES is space-separated; the YAML flow list needs commas or the
  # whole string becomes ONE scalar and the shim serde fails ("no variant for
  # SYS_ADMIN NET_ADMIN ...").
  CAPS_YAML="$(printf '%s' "${CAPABILITIES}" | tr ' ' ',')"
  SECURITY_CTX="      securityContext:
        capabilities:
          add: [${CAPS_YAML}]
"
else
  SECURITY_CTX=""
fi

cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
  labels: {app.kubernetes.io/managed-by: clawbox-guest-probe}
---
apiVersion: v1
kind: Pod
metadata:
  name: ${POD}
  namespace: ${NAMESPACE}
spec:
  runtimeClassName: ${RUNTIME_CLASS}
  restartPolicy: Never
  containers:
    - name: probe
      image: ${IMAGE}
      command: ["/bin/sh", "-c"]
      args: ["printf %s '${PROBE_B64}' | base64 -d | /bin/sh"]
${SECURITY_CTX}
EOF

echo "namespace=${NAMESPACE} pod=${POD}" >&2
cleanup() {
  if [[ "${KEEP}" != true ]]; then
    kubectl delete namespace "${NAMESPACE}" --wait=true --timeout=120s >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

echo "== waiting for probe pod =="
kubectl -n "${NAMESPACE}" wait --for=condition=Ready "pod/${POD}" --timeout=240s \
  || kubectl -n "${NAMESPACE}" get "pod/${POD}" -o wide
echo "== probe logs =="
kubectl -n "${NAMESPACE}" logs "pod/${POD}" --tail=200
