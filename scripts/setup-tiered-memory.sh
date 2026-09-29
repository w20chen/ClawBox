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
warm_mib=${CLAWBOX_WARM_MIB:-131072}
local_mib=${CLAWBOX_LOCAL_MIB:-65536}
local_node=${CLAWBOX_LOCAL_NODE:-0}
warm_node=${CLAWBOX_WARM_NODE:-1}
[[ $warm != / && $cold != / && $warm != "$cold" && $local_node != "$warm_node" ]] || {
  echo 'use distinct storage paths and distinct LOCAL/WARM nodes' >&2; exit 1;
}
[[ $warm =~ ^/[A-Za-z0-9_./-]+$ && $cold =~ ^/[A-Za-z0-9_./-]+$ ]] || {
  echo 'snapshot roots must use letters, digits, slash, dot, underscore or hyphen' >&2; exit 1;
}
[[ $local_mib =~ ^[1-9][0-9]*$ && $warm_mib =~ ^[1-9][0-9]*$ && $local_node =~ ^[0-9]+$ && $warm_node =~ ^[0-9]+$ ]] || {
  echo 'capacities must be positive MiB integers; NUMA nodes must be non-negative integers' >&2; exit 1;
}
[[ -d /sys/devices/system/node/node$local_node && -d /sys/devices/system/node/node$warm_node ]] || {
  echo 'selected NUMA node does not exist; inspect numactl --hardware' >&2; exit 1;
}
[[ $cold != "$warm/"* && $warm != "$cold/"* ]] || {
  echo 'WARM and COLD directories must not contain one another' >&2; exit 1;
}
# Check the idle prerequisite before mounting storage or changing the service.
grep -qx 'populated 0' /sys/fs/cgroup/cube_sandbox/sandbox/cgroup.events || {
  echo 'refusing setup while standalone VMs are running' >&2; exit 1;
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
[[ ,$options, == *,mpol=bind:${warm_node},* ]] || {
  echo "unexpected WARM mount policy: $options" >&2; exit 1;
}
if [[ ,$options, != *,noswap,* ]] && [[ $(wc -l < /proc/swaps) != 1 ]]; then
  echo 'WARM can swap: disable host swap or use a noswap tmpfs' >&2; exit 1
fi
chown "$owner:$(id -gn "$owner")" "$warm" "$cold"
install -D -m 644 "$script_dir/../deploy/cubesandbox/tiered-memory.conf" \
  /etc/systemd/system/cube-sandbox-cubelet.service.d/tiered-memory.conf
printf 'Environment=CLAWBOX_WARM_SNAPSHOT_ROOT=%s\nEnvironment=CLAWBOX_COLD_SNAPSHOT_ROOT=%s\n' \
  "$warm" "$cold" >> /etc/systemd/system/cube-sandbox-cubelet.service.d/tiered-memory.conf
# The standalone launchers source this file after systemd supplies Environment.
# Keep it consistent with the drop-in so changing paths actually takes effect.
python3 - "$warm" "$cold" <<'PY'
from pathlib import Path
import os, re, shlex, shutil, sys, time
p = Path('/usr/local/services/cubetoolbox/.one-click.env')
text = p.read_text()
updated = text
for key, value in {
    'CLAWBOX_WARM_SNAPSHOT_ROOT': sys.argv[1],
    'CLAWBOX_COLD_SNAPSHOT_ROOT': sys.argv[2],
    'CLAWBOX_KVM_DIRTY_TRACKING': '1',
    'CUBE_RESTORE_PRIVATE_COPY': '0',
    'CLAWBOX_WARM_PREALLOCATE': '1',
}.items():
    updated = re.sub(r'^(?:export )?' + key + r'=.*\n?', '', updated, flags=re.M)
    updated = updated.rstrip() + '\n' + key + '=' + shlex.quote(value) + '\n'
if text != updated:
    shutil.copy2(p, p.with_name(p.name + '.pre-host-' + str(time.time_ns())))
    temporary = p.with_name(p.name + '.host-tmp-' + str(os.getpid()))
    try:
        shutil.copy2(p, temporary)
        temporary.write_text(updated)
        os.replace(temporary, p)
    finally:
        temporary.unlink(missing_ok=True)
PY
systemctl daemon-reload
python3 "$script_dir/configure-tiered-local.py" --capacity-mib "$local_mib" --numa-node "$local_node" \
  --numa-nodes "${CLAWBOX_LOCAL_NODES:-$local_node}"
chown "$owner:$(id -gn "$owner")" /sys/fs/cgroup/cube_sandbox/sandbox/memory.reclaim
chmod u+w /sys/fs/cgroup/cube_sandbox/sandbox/memory.reclaim
findmnt -n -o TARGET,FSTYPE,OPTIONS --target "$warm"
echo 'Settings prepared. Restart Cubelet and its egress dependent if the environment flags are new.'
echo 'After a reboot, run this setup again before any experiment; the tmpfs and cgroup settings are not persistent.'
