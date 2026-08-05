#!/usr/bin/env bash
# check-host.sh — read-only preflight for the Kata + Firecracker host.
# It only checks and prints PASS / WARN / FAIL. It never modifies the system.
#
# Kata, Firecracker, devmapper and the containerd runtime handler are all
# host-environment specific, so no auto-install is attempted here.
set -u

PASS=0
WARN=0
FAIL=0

say()  { printf '%-56s %s\n' "$1" "$2"; }
pass() { say "$1" "PASS"; PASS=$((PASS + 1)); }
warn() { say "$1" "WARN"; WARN=$((WARN + 1)); }
fail() { say "$1" "FAIL"; FAIL=$((FAIL + 1)); }

echo "== OpenClaw multi-tenant / Kata-Firecracker host preflight =="

# 1. Architecture
arch="$(uname -m 2>/dev/null || echo unknown)"
case "${arch}" in
  aarch64|arm64) pass "arch = ${arch} (ARM64 target)" ;;
  x86_64|amd64)  warn "arch = ${arch} (works, but not the Kunpeng ARM64 target)" ;;
  *)             warn "arch = ${arch} (unexpected)" ;;
esac

# 2. KVM
if [ -e /dev/kvm ]; then
  if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
    pass "/dev/kvm exists and is accessible"
  else
    warn "/dev/kvm exists but is not rw-accessible to this user"
  fi
else
  fail "/dev/kvm is missing (KVM must be enabled in the kernel/BIOS)"
fi

# 3. kubectl
if command -v kubectl >/dev/null 2>&1; then
  pass "kubectl found: $(command -v kubectl)"
else
  fail "kubectl not found"
fi

# 4. containerd
if command -v ctr >/dev/null 2>&1 && ctr version >/dev/null 2>&1; then
  pass "containerd reachable (ctr version)"
elif [ -S /run/containerd/containerd.sock ]; then
  pass "containerd socket present (/run/containerd/containerd.sock)"
elif command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet containerd; then
  pass "containerd service active"
else
  fail "containerd not found / not running"
fi

# 5. Kata runtime / shim
if command -v kata-runtime >/dev/null 2>&1; then
  pass "kata-runtime found: $(kata-runtime --version 2>/dev/null | head -1)"
elif command -v containerd-shim-kata-v2 >/dev/null 2>&1; then
  pass "kata shim found: $(command -v containerd-shim-kata-v2)"
elif [ -x /opt/kata/bin/kata-runtime ] || [ -x /usr/local/bin/kata-runtime ]; then
  pass "kata-runtime found in a well-known path"
else
  fail "kata-runtime / containerd-shim-kata-v2 not found"
fi

# 6. Firecracker binary
if command -v firecracker >/dev/null 2>&1; then
  pass "firecracker found: $(command -v firecracker)"
elif [ -x /usr/local/bin/firecracker ] || [ -x /usr/bin/firecracker ] || [ -x /opt/firecracker/bin/firecracker ]; then
  pass "firecracker found in a well-known path"
else
  fail "firecracker binary not found"
fi

# 7. RuntimeClass kata-fc
if kubectl get runtimeclass kata-fc >/dev/null 2>&1; then
  pass "RuntimeClass kata-fc exists"
else
  fail "RuntimeClass kata-fc missing (kubectl apply -f runtimeclass.yaml)"
fi

# 8. containerd config contains a kata-fc handler
found=0
for f in /etc/containerd/config.toml /etc/containerd/config.toml.d/*.toml; do
  [ -f "${f}" ] || continue
  if grep -q "kata-fc" "${f}" 2>/dev/null; then
    found=1
    break
  fi
done
if [ "${found}" = 1 ]; then
  pass "containerd config references a kata-fc handler"
else
  fail "containerd config does not reference a kata-fc handler (see runtimeclass.yaml comment)"
fi

# 9. Kubernetes node Ready
if kubectl get nodes 2>/dev/null | awk 'NR>1 && $2=="Ready" { r=1 } END { exit !r }'; then
  pass "Kubernetes node(s) Ready"
else
  warn "no Ready node detected (run: kubectl get nodes)"
fi

echo
echo "Summary: ${PASS} pass, ${WARN} warn, ${FAIL} fail"
if [ "${FAIL}" -gt 0 ]; then
  echo "Fix FAIL items before running the Job. WARN items are informational."
  exit 1
fi
exit 0
