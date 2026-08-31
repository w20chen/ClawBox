#!/bin/sh
# PID 1 for the paper Tool VM: the same SSH command server used in production.
set -eu
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mkdir -p /run /run/clawtune /tmp /dev/pts /dev/shm \
  /testbed/.clawbox/tool-resource /sys/fs/cgroup
mount -t devpts devpts /dev/pts 2>/dev/null || true
mount -t tmpfs tmpfs /dev/shm 2>/dev/null || true
# cgroup v2 delegation for per-execution tool resource telemetry.
if ! mountpoint -q /sys/fs/cgroup 2>/dev/null; then
  mount -t cgroup2 none /sys/fs/cgroup 2>/dev/null || true
fi
chmod 1777 /tmp
hostname tool-vm
printf '127.0.0.1 localhost tool-vm\n::1 localhost\n' >/etc/hosts
export TOOL_BRIDGE_LISTEN=0.0.0.0:2222
export TOOL_BRIDGE_WORKDIR=/testbed
export TOOL_BRIDGE_HOST_KEY=/etc/clawbox/ssh/ssh_host_ed25519_key
export TOOL_BRIDGE_AUTHORIZED_KEY=/etc/clawbox/ssh/id_ed25519.pub
# A container image's Docker ENV metadata is not present when its unpacked
# filesystem is booted directly as a Firecracker rootfs.  Recreate the pinned
# guest-collector environment explicitly for the paper runner.
export PYTHONPATH=/opt/clawtune-guest/services/sidecar/src
export XDG_CACHE_HOME=/opt/clawtune/cache
export BCC_KERNEL_SOURCE=/lib/modules/6.18.28/build
export CLAWTUNE_GUEST_COLLECTOR_HELPER=/opt/clawtune-guest/tools/guest_collector_server.py
export CLAWTUNE_GUEST_COLLECTOR_PYTHON=/opt/clawtune/venv/bin/python
export CLAWTUNE_GUEST_COLLECTOR_SOCKET=/run/clawtune/guest-collector.sock
export CLAWTUNE_GUEST_ARTIFACT_ROOT=/testbed/.clawbox/tool-resource
export CLAWBOX_REPOSITORY="${CLAWBOX_REPOSITORY:-openclaw}"
exec /usr/local/bin/tool-bridge
