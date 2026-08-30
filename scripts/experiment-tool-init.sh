#!/bin/sh
# PID 1 for the paper Tool VM: the same SSH command server used in production.
set -eu
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mkdir -p /run /tmp /dev/pts /dev/shm /testbed/.clawbox /sys/fs/cgroup
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
exec /usr/local/bin/tool-bridge
