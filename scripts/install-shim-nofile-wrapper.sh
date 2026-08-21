#!/usr/bin/env bash
# Install a containerd-shim-kata-v2 wrapper that raises RLIMIT_NOFILE to
# hard before exec'ing the real shim.
#
# Why: Kata runtime-rs resets the soft RLIMIT_NOFILE to 1024 (hard 524288),
# which exhausts file descriptors at ~19 concurrent cells
# with "No file descriptors available (os error 24)". Raising soft=hard in the
# wrapper gives a 32-cell run headroom.
#
# Run on the target (requires root):
#   sudo bash scripts/install-shim-nofile-wrapper.sh
#   bash scripts/audit-kata-firecracker-arm64.sh --root /opt/kata   # FC-0 gate
#
# Reversible: sudo mv /usr/local/bin/containerd-shim-kata-v2.real \
#                   /usr/local/bin/containerd-shim-kata-v2
set -euo pipefail

SHIM_DIR="/usr/local/bin"
SHIM="${SHIM_DIR}/containerd-shim-kata-v2"
REAL="${SHIM}.real"
MARKER="clawbox-shim-nofile-wrapper-v1"

if [[ "$(id -u)" != 0 ]]; then
  echo "must run as root (sudo); usage: sudo bash install-shim-nofile-wrapper.sh" >&2
  exit 1
fi
if [[ ! -e "${SHIM}" ]]; then
  echo "shim not found at ${SHIM}; install Kata first" >&2
  exit 1
fi
if grep -q "${MARKER}" "${SHIM}" 2>/dev/null; then
  expected_nofile="$(ulimit -Hn)"
  actual_nofile="$("${SHIM}" --_selfcheck 2>/dev/null || true)"
  if [[ "${actual_nofile}" == "${expected_nofile}" ]]; then
    echo "wrapper already installed at ${SHIM}"
    echo "selfcheck soft nofile: ${actual_nofile}"
    exit 0
  fi
  if [[ ! -x "${REAL}" ]]; then
    echo "wrapper selfcheck failed and real shim is missing or not executable: ${REAL}" >&2
    exit 1
  fi
  echo "repairing failed wrapper at ${SHIM}"
else
  if [[ -e "${REAL}" ]]; then
    echo "refusing to overwrite existing real-shim backup: ${REAL}" >&2
    exit 1
  fi
  # Move the real shim aside (keep its mode/permissions); the wrapper then execs it.
  mv "${SHIM}" "${REAL}"
fi

cat >"${SHIM}" <<EOF
#!/bin/sh
# ${MARKER}
# Kata runtime-rs resets the soft RLIMIT_NOFILE to 1024 (hard 524288), which
# exhausts FDs at ~19 concurrent cells.  Raise soft=hard before exec'ing the
# real shim so a 32-cell run has headroom.
raise_nofile() {
  hard_limit=\$(ulimit -Hn) || exit 1
  ulimit -Sn "\${hard_limit}" || {
    echo "failed to raise soft RLIMIT_NOFILE to \${hard_limit}" >&2
    exit 1
  }
}
if [ "\$1" = "--_selfcheck" ]; then
  raise_nofile
  ulimit -Sn
  exit 0
fi
raise_nofile
exec "${REAL}" "\$@"
EOF
chmod 0755 "${SHIM}"

echo "installed wrapper -> ${SHIM} (marker ${MARKER})"
echo "real shim preserved -> ${REAL}"
echo -n "selfcheck soft nofile: "
"${SHIM}" --_selfcheck
echo "verify real shim still runs:"
"${REAL}" --version 2>&1 | head -1 || true
