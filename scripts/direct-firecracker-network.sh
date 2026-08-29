#!/usr/bin/env bash
# Create one isolated L2 segment per direct-Firecracker replay session.
# Keep it alive across Firecracker snapshot/restore; only `down` removes it.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: direct-firecracker-network.sh up|down --sessions N [--prefix A.B]

For session i (zero based), creates bridge cbrIIII and TAPs crtIIII/ctlIIII.
Runtime is A.B.(i+1).2, Tool is A.B.(i+1).3, and the host inference service
binds A.B.(i+1).1.  The generated Firecracker config must use matching taps
and kernel ip= boot arguments.  Requires root or passwordless sudo.
EOF
  exit 64
}

[[ $# -ge 1 ]] || usage
ACTION="$1"; shift
SESSIONS=""
PREFIX="172.30"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sessions) SESSIONS="${2:-}"; shift 2 ;;
    --prefix) PREFIX="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done
[[ "${ACTION}" == up || "${ACTION}" == down ]] || usage
[[ "${SESSIONS}" =~ ^[1-9][0-9]*$ && "${SESSIONS}" -le 253 ]] || usage
[[ "${PREFIX}" =~ ^([0-9]{1,3})\.([0-9]{1,3})$ ]] || usage
IFS=. read -r FIRST SECOND <<<"${PREFIX}"
(( FIRST <= 255 && SECOND <= 255 )) || usage

host_command() {
  if [[ "$(id -u)" == 0 ]]; then "$@"; else sudo "$@"; fi
}

for ((index=0; index<SESSIONS; index++)); do
  tag="$(printf '%04d' "${index}")"
  bridge="cbr${tag}"; runtime_tap="crt${tag}"; tool_tap="ctl${tag}"
  subnet=$((index + 1))
  host_ip="${PREFIX}.${subnet}.1/24"
  if [[ "${ACTION}" == up ]]; then
    host_command ip link show "${bridge}" >/dev/null 2>&1 && {
      echo "refusing to reuse existing ${bridge}" >&2; exit 1;
    }
    host_command ip link add name "${bridge}" type bridge
    host_command ip addr add "${host_ip}" dev "${bridge}"
    host_command ip link set "${bridge}" up
    for tap in "${runtime_tap}" "${tool_tap}"; do
      host_command ip tuntap add dev "${tap}" mode tap user "${SUDO_USER:-$USER}"
      host_command ip link set "${tap}" master "${bridge}"
      host_command ip link set "${tap}" up
    done
    printf 'session=%d bridge=%s runtime_tap=%s tool_tap=%s inference_host=%s\n' \
      "${index}" "${bridge}" "${runtime_tap}" "${tool_tap}" "${host_ip%/24}"
  else
    # A failed `up` can leave a TAP behind before it is enslaved to its bridge.
    # Remove only the three deterministic names for this session; deleting a
    # bridge normally removes its ports, and the later checks become no-ops.
    for device in "${bridge}" "${runtime_tap}" "${tool_tap}"; do
      host_command ip link show "${device}" >/dev/null 2>&1 || continue
      host_command ip link del "${device}"
    done
  fi
done
