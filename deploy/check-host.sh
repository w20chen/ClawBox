#!/usr/bin/env bash
# Read-only openEuler/arm64 gate for Kubernetes + Kata.
set -u

RUNTIME_CLASS="${CLAWBOX_RUNTIME_CLASS:-kata-qemu-runtime-rs}"
MIN_SAFE_KATA_VERSION="${CLAWBOX_MIN_SAFE_KATA_VERSION:-3.31.0}"
PASS=0
WARN=0
FAIL=0

usage() {
  echo "usage: check-host.sh [--runtime-class NAME]" >&2
  exit 64
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runtime-class) RUNTIME_CLASS="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

[[ "${RUNTIME_CLASS}" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]] || usage

say()  { printf '%-64s %s\n' "$1" "$2"; }
pass() { say "$1" "PASS"; PASS=$((PASS + 1)); }
warn() { say "$1" "WARN"; WARN=$((WARN + 1)); }
fail() { say "$1" "FAIL"; FAIL=$((FAIL + 1)); }

echo "== ClawBox stage-0 host gate: openEuler/arm64 + ${RUNTIME_CLASS} =="

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  os_id="${ID:-unknown}"
  os_version="${VERSION_ID:-unknown}"
  if [[ "${os_id}" == "openEuler" || "${os_id,,}" == "openeuler" ]]; then
    pass "operating system = ${os_id} ${os_version}"
  else
    warn "operating system = ${os_id} ${os_version} (target is openEuler)"
  fi
else
  fail "/etc/os-release is readable"
fi

arch="$(uname -m 2>/dev/null || echo unknown)"
case "${arch}" in
  aarch64|arm64) pass "host architecture = ${arch}" ;;
  *) fail "host architecture = ${arch} (required: aarch64/arm64)" ;;
esac

kernel="$(uname -r 2>/dev/null || echo unknown)"
case "${kernel%%.*}" in
  ''|*[!0-9]*) warn "kernel = ${kernel} (cannot parse major version)" ;;
  *)
    kernel_major="${kernel%%.*}"
    kernel_minor="${kernel#*.}"; kernel_minor="${kernel_minor%%.*}"
    if (( kernel_major > 5 || (kernel_major == 5 && kernel_minor >= 10) )); then
      pass "kernel = ${kernel} (>= 5.10)"
    else
      fail "kernel = ${kernel} (Kata baseline requires >= 5.10)"
    fi
    ;;
esac

if [[ -c /dev/kvm ]]; then
  pass "/dev/kvm character device exists"
  [[ -r /dev/kvm && -w /dev/kvm ]] \
    && pass "/dev/kvm is accessible to this account" \
    || warn "/dev/kvm is not rw-accessible to this account; the runtime service still must have access"
else
  fail "/dev/kvm is missing (enable ARM Hyp/KVM)"
fi

[[ -f /sys/fs/cgroup/cgroup.controllers ]] \
  && pass "cgroup v2 unified hierarchy is active" \
  || fail "cgroup v2 unified hierarchy is not active"

if command -v containerd >/dev/null 2>&1; then
  pass "containerd binary is installed"
else
  fail "containerd binary is missing"
fi

containerd_reachable=false
containerd_service_active=false
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet containerd; then
  containerd_service_active=true
  pass "containerd service is active"
else
  fail "containerd service is not active"
fi

if command -v ctr >/dev/null 2>&1; then
  if timeout 5 ctr version >/dev/null 2>&1; then
    containerd_reachable=true
    pass "containerd socket is reachable"
  elif [[ "${containerd_service_active}" == true ]]; then
    warn "containerd socket is not accessible to this account; rerun with sudo for CRI inspection"
  else
    fail "containerd socket is not reachable within 5s"
  fi
else
  fail "ctr is unavailable; containerd cannot be verified"
fi

if [[ "${containerd_reachable}" == true ]]; then
  if timeout 5 ctr plugins ls 2>/dev/null | awk '
    $1 == "io.containerd.grpc.v1" && $2 == "cri" && $NF == "ok" { found=1 }
    $1 == "io.containerd.cri.v1" && $2 == "runtime" && $NF == "ok" { found=1 }
    END { exit(found ? 0 : 1) }
  '; then
    pass "containerd CRI plugin reports ok"
  else
    fail "containerd CRI plugin is absent or unhealthy"
  fi
fi

kata_shim=""
if command -v containerd-shim-kata-v2 >/dev/null 2>&1; then
  kata_shim="$(command -v containerd-shim-kata-v2)"
else
  for candidate in \
    /opt/kata/runtime-rs/bin/containerd-shim-kata-v2 \
    /opt/kata/bin/containerd-shim-kata-v2 \
    /usr/bin/containerd-shim-kata-v2; do
    if [[ -x "${candidate}" ]]; then
      kata_shim="${candidate}"
      break
    fi
  done
fi

if [[ -n "${kata_shim}" ]]; then
  pass "containerd-shim-kata-v2 is installed (${kata_shim})"
  if [[ "${RUNTIME_CLASS}" == *runtime-rs* ]]; then
    kata_version_output="$("${kata_shim}" --version 2>&1 || true)"
    kata_version="$(grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' <<<"${kata_version_output}" | head -1)"
    if [[ -z "${kata_version}" ]]; then
      fail "Kata runtime-rs version could not be established (must be >= ${MIN_SAFE_KATA_VERSION})"
    elif [[ "$(printf '%s\n%s\n' "${MIN_SAFE_KATA_VERSION}" "${kata_version}" | sort -V | head -1)" == "${MIN_SAFE_KATA_VERSION}" ]]; then
      pass "Kata runtime-rs = ${kata_version} (>= ${MIN_SAFE_KATA_VERSION})"
    else
      fail "Kata runtime-rs = ${kata_version} (< ${MIN_SAFE_KATA_VERSION}; CVE-2026-47243)"
    fi
  fi
else
  fail "containerd-shim-kata-v2 is missing"
fi

if command -v kata-runtime >/dev/null 2>&1; then
  kata-runtime kata-check >/dev/null 2>&1 \
    && pass "kata-runtime kata-check" \
    || warn "kata-runtime exists but kata-check did not pass for the current account"
elif [[ -x /opt/kata/bin/kata-runtime ]]; then
  /opt/kata/bin/kata-runtime kata-check >/dev/null 2>&1 \
    && pass "/opt/kata/bin/kata-runtime kata-check" \
    || warn "Kata runtime exists but kata-check did not pass"
else
  warn "kata-runtime CLI is unavailable; shim and live Pod gates remain authoritative"
fi

if ! command -v kubectl >/dev/null 2>&1; then
  fail "kubectl is missing"
else
  pass "kubectl is installed"
  if kubectl cluster-info >/dev/null 2>&1; then
    pass "kubectl can reach the cluster"
  else
    fail "kubectl cannot reach the cluster"
  fi

  runtime_handler="$(kubectl get runtimeclass "${RUNTIME_CLASS}" -o jsonpath='{.handler}' 2>/dev/null || true)"
  if [[ -n "${runtime_handler}" ]]; then
    pass "RuntimeClass ${RUNTIME_CLASS} exists (handler=${runtime_handler})"
  else
    fail "RuntimeClass ${RUNTIME_CLASS} is missing"
  fi

  node_arches="$(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"="}{.status.nodeInfo.architecture}{" "}{end}' 2>/dev/null || true)"
  if [[ -n "${node_arches}" ]] && ! grep -Eq '=(amd64|x86_64)( |$)' <<<"${node_arches}"; then
    pass "Kubernetes node architectures: ${node_arches}"
  elif [[ -n "${node_arches}" ]]; then
    warn "cluster contains non-arm64 nodes: ${node_arches}; use node selectors for sandbox Pods"
  else
    fail "Kubernetes node architecture could not be read"
  fi

  ready_nodes="$(kubectl get nodes --no-headers 2>/dev/null | awk '$2 == "Ready" {count++} END {print count+0}')"
  (( ready_nodes > 0 )) && pass "Ready Kubernetes nodes = ${ready_nodes}" || fail "no Ready Kubernetes nodes"

  if kubectl api-resources --api-group=networking.k8s.io -o name 2>/dev/null | grep -qx networkpolicies; then
    pass "Kubernetes NetworkPolicy API is available"
    warn "NetworkPolicy API presence does not prove CNI enforcement; run scripts/arm64-kata-smoke.sh"
  else
    fail "Kubernetes NetworkPolicy API is unavailable"
  fi
fi

echo
echo "Summary: ${PASS} pass, ${WARN} warn, ${FAIL} fail"
if (( FAIL > 0 )); then
  echo "Stage 0 host preflight FAILED. Fix every FAIL before the live smoke gate."
  exit 1
fi
echo "Static preflight passed. Next: scripts/arm64-kata-smoke.sh --runtime-class ${RUNTIME_CLASS}"
