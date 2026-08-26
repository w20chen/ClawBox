#!/usr/bin/env bash
# Read-only FC-0/FC-2 host gate for openEuler arm64.
set -uo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_CLASS="${CLAWBOX_RUNTIME_CLASS:-kata-fc-arm64}"
MIN_SAFE_KATA_VERSION="${CLAWBOX_MIN_SAFE_KATA_VERSION:-3.31.0}"
FIRECRACKER_VERSION="${FIRECRACKER_VERSION:-1.12.1}"
REQUIRE_READY_LABEL=false
PASS=0 WARN=0 FAIL=0

usage() { echo "usage: check-host.sh [--runtime-class kata-fc-arm64] [--require-ready-label]" >&2; exit 64; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --runtime-class) RUNTIME_CLASS="${2:-}"; shift 2 ;;
    --require-ready-label) REQUIRE_READY_LABEL=true; shift ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done
[[ "${RUNTIME_CLASS}" == kata-fc-arm64 ]] || { echo "only kata-fc-arm64 is supported" >&2; exit 64; }

say() { printf '%-72s %s\n' "$1" "$2"; }
pass() { say "$1" PASS; PASS=$((PASS + 1)); }
warn() { say "$1" WARN; WARN=$((WARN + 1)); }
fail() { say "$1" FAIL; FAIL=$((FAIL + 1)); }
have() { command -v "$1" >/dev/null 2>&1; }
# /dev/kvm, the containerd socket and /opt/kata are root-owned; fall back to
# passwordless sudo for the privileged probes so an unprivileged caller gets
# the same result as root (identical helper as scripts/arm64-kata-smoke.sh).
host_command() {
  if [[ "$(id -u)" == 0 ]]; then "$@"; else sudo -n "$@"; fi
}

echo "== ClawBox Firecracker host gate: openEuler/arm64 + ${RUNTIME_CLASS} =="
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${ID,,}" == openeuler ]] && pass "operating system = ${PRETTY_NAME:-${ID}}" \
    || fail "operating system is not openEuler (${PRETTY_NAME:-unknown})"
else
  fail "/etc/os-release is readable"
fi

arch="$(uname -m 2>/dev/null || echo unknown)"
[[ "${arch}" =~ ^(aarch64|arm64)$ ]] && pass "host architecture = ${arch}" \
  || fail "host architecture = ${arch}; native arm64 is required"
host_command test -c /dev/kvm -a -r /dev/kvm -a -w /dev/kvm && pass "/dev/kvm is usable" \
  || fail "/dev/kvm is not readable/writable (run the gate as root or with sudo)"
[[ -r /sys/fs/cgroup/cgroup.controllers ]] && pass "cgroup v2 unified hierarchy" \
  || fail "cgroup v2 unified hierarchy is unavailable"
grep -Eq '(^|[[:space:]])(0|1)$' /sys/module/kvm/parameters/* 2>/dev/null \
  && pass "KVM module parameters are readable" || warn "KVM module parameters could not be inspected"

# timeout is an external binary, so it cannot invoke the host_command shell
# function; run it inside the function so sudo executes `timeout 5 ctr version`.
if have containerd && host_command timeout 5 ctr version >/dev/null 2>&1; then
  pass "containerd socket is reachable"
  containerd_version="$(host_command containerd --version 2>/dev/null || true)"
  grep -Eq 'containerd .* (v)?2\.' <<<"${containerd_version}" \
    && pass "containerd 2.x = ${containerd_version}" \
    || fail "containerd 2.x is required (${containerd_version:-unknown})"
  plugins="$(host_command ctr plugins ls 2>/dev/null || true)"
  awk '$1 ~ /snapshotter/ && $2 == "devmapper" && $NF == "ok" {found=1} END {exit !found}' <<<"${plugins}" \
    && pass "containerd devmapper snapshotter = ok" \
    || fail "containerd devmapper snapshotter is not healthy"
  config_dump="$(host_command containerd config dump 2>/dev/null || true)"
  grep -F "runtimes.${RUNTIME_CLASS}" <<<"${config_dump}" >/dev/null \
    && pass "containerd handler ${RUNTIME_CLASS} exists" \
    || fail "containerd handler ${RUNTIME_CLASS} is missing"
  # Extract the full handler section (header plus its nested options table),
  # not just the first 12 lines: containerd 2.x dumps many runtime fields and
  # the options/ConfigPath live past line 12.  Section headers are indented
  # (SetIndentTables), so match '[ ' with optional leading whitespace.
  handler_block="$(awk -v cls="${RUNTIME_CLASS}" '
    /^[[:space:]]*\[/ {
      if (in_block && $0 !~ cls) exit
      if (!in_block && $0 ~ cls) in_block = 1
    }
    in_block { print }
  ' <<<"${config_dump}")"
  # containerd's config dump quotes values with single quotes; accept both.
  grep -Eq "snapshotter[[:space:]]*=[[:space:]]*['\"]devmapper['\"]" <<<"${handler_block}" \
    && pass "handler selects devmapper" || fail "handler does not select devmapper"
  grep -F 'configuration-fc-arm64.toml' <<<"${handler_block}" >/dev/null \
    && pass "handler selects the audited Firecracker config" \
    || fail "handler ConfigPath is not the audited arm64 Firecracker config"
  if grep -Eiq 'runtimes\.[^]]*(qemu|cloud-hypervisor)' <<<"${config_dump}"; then
    fail "an alternate hypervisor handler remains active in containerd"
  else
    pass "no alternate hypervisor handler is active"
  fi
else
  fail "containerd/ctr is installed and reachable"
fi

if host_command bash "${ROOT}/scripts/audit-kata-firecracker-arm64.sh" \
  --root /opt/kata --kata-version "${MIN_SAFE_KATA_VERSION}" --firecracker-version "${FIRECRACKER_VERSION}" >/dev/null; then
  pass "FC-0 artifact audit (Kata >= ${MIN_SAFE_KATA_VERSION}; CVE-2026-47243 gate)"
else
  fail "FC-0 artifact audit"
fi

if have kubectl && kubectl cluster-info >/dev/null 2>&1; then
  pass "kubectl can reach Kubernetes"
  handler="$(kubectl get runtimeclass "${RUNTIME_CLASS}" -o jsonpath='{.handler}' 2>/dev/null || true)"
  overhead_cpu="$(kubectl get runtimeclass "${RUNTIME_CLASS}" -o jsonpath='{.overhead.podFixed.cpu}' 2>/dev/null || true)"
  overhead_memory="$(kubectl get runtimeclass "${RUNTIME_CLASS}" -o jsonpath='{.overhead.podFixed.memory}' 2>/dev/null || true)"
  [[ "${handler}" == "${RUNTIME_CLASS}" ]] && pass "RuntimeClass handler = ${handler}" \
    || fail "RuntimeClass ${RUNTIME_CLASS} is missing or points elsewhere"
  [[ -n "${overhead_cpu}" && -n "${overhead_memory}" ]] \
    && pass "RuntimeClass overhead = cpu ${overhead_cpu}, memory ${overhead_memory}" \
    || fail "RuntimeClass Pod overhead is missing"
  node_arches="$(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"="}{.status.nodeInfo.architecture}{" "}{end}' 2>/dev/null || true)"
  [[ -n "${node_arches}" ]] && ! grep -Eq '=(amd64|x86_64)( |$)' <<<"${node_arches}" \
    && pass "Kubernetes node architectures: ${node_arches}" \
    || fail "all sandbox nodes must be arm64 (${node_arches:-unknown})"
  if kubectl get nodes -l clawbox.openai.com/firecracker-ready=true -o name 2>/dev/null | grep -q .; then
    pass "at least one node passed the Firecracker install gate"
  elif [[ "${REQUIRE_READY_LABEL}" == true ]]; then
    fail "no node has the Firecracker-ready label"
  else
    warn "Firecracker-ready label is not present yet; add it only after this pre-label gate passes"
  fi
  kubectl api-resources --api-group=networking.k8s.io -o name 2>/dev/null | grep -q '^networkpolicies' \
    && pass "NetworkPolicy API is available" || fail "NetworkPolicy API is unavailable"
else
  fail "kubectl is installed and reaches Kubernetes"
fi

echo
echo "Summary: ${PASS} pass, ${WARN} warn, ${FAIL} fail"
if (( FAIL > 0 )); then
  echo "Firecracker host preflight FAILED. RuntimeClass use is blocked."
  exit 1
fi
echo "Static gate passed. Authoritative live gate: scripts/arm64-kata-smoke.sh --runtime-class ${RUNTIME_CLASS}"
