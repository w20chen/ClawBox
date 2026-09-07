#!/usr/bin/env bash
# Configure the NUMA-backed study on an already installed, patched CubeSandbox.
set -euo pipefail
if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: sudo $0 WARM_ROOT COLD_ROOT [EXPERIMENT_USER]" >&2
  exit 64
fi
[[ $(id -u) == 0 ]] || { echo 'run as root' >&2; exit 1; }
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
warm=$(realpath -m "$1")
cold=$(realpath -m "$2")
owner=${3:-${SUDO_USER:-root}}
warm_mib=${CLAWBOX_WARM_MIB:-65536}
local_mib=${CLAWBOX_LOCAL_MIB:-65536}
local_node=${CLAWBOX_LOCAL_NODE:-0}
warm_node=${CLAWBOX_WARM_NODE:-1}
[[ $warm != / && $cold != / && $warm != "$cold" && $local_node != "$warm_node" ]] || {
  echo 'use distinct storage paths and distinct LOCAL/WARM nodes' >&2; exit 1;
}
install -d -o "$owner" -g "$(id -gn "$owner")" -m 755 "$warm" "$cold"
if ! mountpoint -q "$warm"; then
  [[ -z $(find "$warm" -mindepth 1 -maxdepth 1 -print -quit) ]] || {
    echo "refusing to cover existing files under $warm" >&2; exit 1;
  }
  mount -t tmpfs -o "size=${warm_mib}M,mpol=bind:${warm_node},noswap" clawbox-warm "$warm"
fi
[[ $(findmnt -n -o FSTYPE --target "$warm") == tmpfs ]] || {
  echo 'WARM must be tmpfs' >&2; exit 1;
}
options=$(findmnt -n -o OPTIONS --target "$warm")
[[ ,$options, == *,mpol=bind:${warm_node},* && ,$options, == *,noswap,* ]] || {
  echo "unexpected WARM mount policy: $options" >&2; exit 1;
}
chown "$owner:$(id -gn "$owner")" "$warm" "$cold"
install -D -m 644 "$script_dir/../deploy/cubesandbox/tiered-memory.conf" \
  /etc/systemd/system/cube-sandbox-cubelet.service.d/tiered-memory.conf
systemctl daemon-reload
python3 "$script_dir/configure-tiered-local.py" --capacity-mib "$local_mib" --numa-node "$local_node"
findmnt -n -o TARGET,FSTYPE,OPTIONS --target "$warm"
echo 'Settings prepared. Restart Cubelet and its egress dependent if the environment flags are new.'
echo 'After a reboot, run this setup again before any experiment; the tmpfs and cgroup settings are not persistent.'
