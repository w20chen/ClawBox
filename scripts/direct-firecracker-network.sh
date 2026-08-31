#!/usr/bin/env bash
# Create one isolated L2 segment per direct-Firecracker replay session.
# Keep it alive across Firecracker snapshot/restore; only `down` removes it.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: direct-firecracker-network.sh up|down --sessions N [--network CIDR] [--reuse-existing]

For session i (zero based), creates bridge cbrIIII and TAPs crtIIII/ctlIIII.
Each session receives one /29 from the parent CIDR: host, Runtime, and Tool use
addresses +1, +2, and +3. The generated Firecracker config must use matching
taps and kernel ip= boot arguments. Requires root or passwordless sudo.

--reuse-existing is valid only with `up`. It reuses a session only after
validating its bridge address and both TAP-to-bridge memberships.
EOF
  exit 64
}

[[ $# -ge 1 ]] || usage
ACTION="$1"; shift
SESSIONS=""
NETWORK="172.30.0.0/16"
REUSE_EXISTING=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NETWORK_HELPER="${SCRIPT_DIR}/../clawbox/replay/network.py"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sessions) SESSIONS="${2:-}"; shift 2 ;;
    --network) NETWORK="${2:-}"; shift 2 ;;
    --reuse-existing) REUSE_EXISTING=1; shift ;;
    *) usage ;;
  esac
done
[[ "${ACTION}" == up || "${ACTION}" == down ]] || usage
(( REUSE_EXISTING == 0 )) || [[ "${ACTION}" == up ]] || usage
[[ "${SESSIONS}" =~ ^[1-9][0-9]*$ ]] || usage
CAPACITY="$(python3 "${NETWORK_HELPER}" capacity "${NETWORK}")" || usage
(( SESSIONS <= CAPACITY )) || usage

host_command() {
  if [[ "$(id -u)" == 0 ]]; then "$@"; else sudo "$@"; fi
}

device_exists() {
  host_command ip link show "$1" >/dev/null 2>&1
}

validate_existing_session() {
  local bridge="$1" runtime_tap="$2" tool_tap="$3" host_ip="$4"
  device_exists "${bridge}" && device_exists "${runtime_tap}" \
    && device_exists "${tool_tap}" || return 1
  host_command ip -o -4 addr show dev "${bridge}" \
    | grep -Fq " inet ${host_ip} " || return 1
  for tap in "${runtime_tap}" "${tool_tap}"; do
    host_command ip -o link show dev "${tap}" \
      | grep -Eq " master ${bridge}( |$)" || return 1
  done
}

for ((index=0; index<SESSIONS; index++)); do
  tag="$(printf '%04d' "${index}")"
  bridge="cbr${tag}"; runtime_tap="crt${tag}"; tool_tap="ctl${tag}"
  mapfile -t addresses < <(
    python3 "${NETWORK_HELPER}" session "${NETWORK}" "${index}"
  )
  [[ "${#addresses[@]}" -eq 4 ]] || exit 1
  host_ip="${addresses[0]}"
  if [[ "${ACTION}" == up ]]; then
    if device_exists "${bridge}" || device_exists "${runtime_tap}" \
        || device_exists "${tool_tap}"; then
      if (( REUSE_EXISTING == 1 )) \
          && validate_existing_session "${bridge}" "${runtime_tap}" "${tool_tap}" "${host_ip}"; then
        printf 'session=%d bridge=%s runtime_tap=%s tool_tap=%s inference_host=%s reused=true\n' \
          "${index}" "${bridge}" "${runtime_tap}" "${tool_tap}" "${host_ip%/*}"
        continue
      fi
      echo "refusing incomplete, mismatched, or unapproved existing session ${tag}" >&2
      exit 1
    fi
    host_command ip link add name "${bridge}" type bridge
    host_command ip addr add "${host_ip}" dev "${bridge}"
    host_command ip link set "${bridge}" up
    for tap in "${runtime_tap}" "${tool_tap}"; do
      host_command ip tuntap add dev "${tap}" mode tap user "${SUDO_USER:-$USER}"
      host_command ip link set "${tap}" master "${bridge}"
      host_command ip link set "${tap}" up
    done
    printf 'session=%d bridge=%s runtime_tap=%s tool_tap=%s inference_host=%s\n' \
      "${index}" "${bridge}" "${runtime_tap}" "${tool_tap}" "${host_ip%/*}"
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
