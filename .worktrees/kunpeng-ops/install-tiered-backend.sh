#!/usr/bin/env bash
set -euo pipefail
source_dir=/home/weitianc/CubeSandbox-standalone-20260907
backup_dir=/data/clawbox-backend-backups/20260907-private-copy
cubelet=/usr/local/services/cubetoolbox/Cubelet/bin/cubelet
shim=/data/cubelet/root/component_versions/cube-shim/v0.7.0-clawbox-standalone/bin/containerd-shim-cube-rs
grep -qx 'populated 0' /sys/fs/cgroup/cube_sandbox/sandbox/cgroup.events || {
  echo 'Refusing deployment while standalone VMs are running' >&2; exit 1;
}
test -x "$source_dir/_output/bin/cubelet-warm-preallocate"
test -x "$source_dir/CubeShim/target/release/containerd-shim-cube-rs"
mkdir -p "$backup_dir"
test -f "$backup_dir/cubelet" || cp -p "$cubelet" "$backup_dir/cubelet"
test -f "$backup_dir/containerd-shim-cube-rs" || cp -p "$shim" "$backup_dir/containerd-shim-cube-rs"
systemctl stop cube-sandbox-cubelet.service
install -m 755 "$source_dir/_output/bin/cubelet-warm-preallocate" "$cubelet.clawbox-new"
mv -f "$cubelet.clawbox-new" "$cubelet"
install -m 755 "$source_dir/CubeShim/target/release/containerd-shim-cube-rs" "$shim.clawbox-new"
mv -f "$shim.clawbox-new" "$shim"
install -D -m 644 /home/weitianc/tiered-memory.conf \
  /etc/systemd/system/cube-sandbox-cubelet.service.d/tiered-memory.conf
systemctl daemon-reload
systemctl start cube-sandbox-cubelet.service
# Requires=Cubelet stops egress during the update; starting Cubelet alone
# does not restart its dependents.
systemctl start cube-sandbox-cube-egress.service
python3 /home/weitianc/ClawBox-experiment-8bc19e6/scripts/configure-tiered-local.py
systemctl is-active cube-sandbox-cubelet.service
