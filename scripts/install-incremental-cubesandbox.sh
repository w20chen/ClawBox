#!/usr/bin/env bash
# Install a tested four-component CubeSandbox build on an idle standalone host.
set -Eeuo pipefail
if [[ ${1:-} == --shim-only ]]; then
  (( $# == 3 )) || { echo 'usage: sudo bash scripts/install-incremental-cubesandbox.sh --shim-only BUILT_SHIM INSTALLED_SHIM' >&2; exit 64; }
  (( EUID == 0 )) || { echo 'run as root' >&2; exit 1; }
  source_shim=$(realpath "$2")
  target_shim=$(realpath "$3")
  [[ -x "$source_shim" && -x "$target_shim" && "$source_shim" != "$target_shim" && $(basename "$target_shim") == containerd-shim-cube-rs ]] || {
    echo 'supply distinct existing executable source and installed versioned Shim paths' >&2; exit 1;
  }
  install -d -m 700 /run/clawbox
  exec 9>/run/clawbox/base-cache.lock
  flock -n 9 || { echo 'experiment/cache maintenance is active; wait before installing' >&2; exit 1; }
  [[ ! -e /run/clawbox/base-cache.json ]] || { echo 'run experiment base-cache cleanup before replacing the Shim' >&2; exit 1; }
  python3 - <<'PY'
from pathlib import Path
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():
        continue
    try:
        name = Path((entry/'cmdline').read_bytes().split(b'\0')[0].decode()).name
        if name in {'containerd-shim-cube-rs', 'cloud-hypervisor', 'cube-hypervisor'}:
            raise SystemExit('live VM process ' + entry.name + '; finish/destroy VMs before installing')
    except (FileNotFoundError, ProcessLookupError):
        pass
PY
  backup_dir=$(mktemp -d /var/tmp/clawbox-shim-backup.XXXXXXXX)
  cp -p "$target_shim" "$backup_dir/containerd-shim-cube-rs"
  install -m 755 "$source_shim" "$target_shim.clawbox-new"
  mv -f "$target_shim.clawbox-new" "$target_shim"
  sha256sum "$backup_dir/containerd-shim-cube-rs" "$target_shim"
  printf 'Installed Shim only; existing templates and other components preserved.\nBackup: %s\n' "$backup_dir/containerd-shim-cube-rs"
  printf 'Rollback on an idle, cache-disabled host: sudo bash scripts/install-incremental-cubesandbox.sh --shim-only %q %q\n' "$backup_dir/containerd-shim-cube-rs" "$target_shim"
  exit 0
fi
if (( $# != 4 )); then
  echo "usage: sudo $0 CUBE_SOURCE_DIR CLAWBOX_DIR WARM_ROOT COLD_ROOT" >&2
  exit 64
fi
(( EUID == 0 )) || { echo 'run as root' >&2; exit 1; }
cube_source=$(realpath "$1")
project=$(realpath "$2")
warm=$(realpath -m "$3")
cold=$(realpath -m "$4")
grep -qx 'populated 0' /sys/fs/cgroup/cube_sandbox/sandbox/cgroup.events || {
  echo 'refusing deployment while standalone VMs are running' >&2; exit 1;
}
root=/usr/local/services/cubetoolbox
declare -A binaries=(
  [api]="$root/CubeAPI/bin/cube-api"
  [master]="$root/CubeMaster/bin/cubemaster"
  [cubelet]="$root/Cubelet/bin/cubelet"
  [shim]="/data/cubelet/root/component_versions/cube-shim/v0.7.0-clawbox-standalone/bin/containerd-shim-cube-rs"
)
declare -A builds=(
  [api]="$cube_source/CubeAPI/target/release/cube-api"
  [master]="$cube_source/CubeMaster/bin/cubemaster-incremental"
  [cubelet]="$cube_source/Cubelet/bin/cubelet-incremental"
  [shim]="$cube_source/CubeShim/target/release/containerd-shim-cube-rs"
)
for key in api master cubelet shim; do
  [[ -x ${binaries[$key]} && -x ${builds[$key]} ]] || {
    echo "missing installed or tested $key binary" >&2; exit 1;
  }
done
config=/etc/systemd/system/cube-sandbox-cubelet.service.d/tiered-memory.conf
install -d -m 700 /data/clawbox-backend-backups
backup=$(mktemp -d /data/clawbox-backend-backups/incremental-XXXXXXXX)
for key in api master cubelet shim; do cp -p "${binaries[$key]}" "$backup/$key"; done
had_config=0
if [[ -f $config ]]; then cp -p "$config" "$backup/tiered-memory.conf"; had_config=1; fi
restart_services() {
  systemctl daemon-reload
  systemctl start cube-sandbox-cubelet.service
  systemctl start cube-sandbox-cube-egress.service
  systemctl start cube-sandbox-cubemaster.service
  systemctl start cube-sandbox-cube-api.service
}
rollback() {
  trap - ERR
  echo "deployment failed; restoring binaries from $backup" >&2
  for key in api master cubelet shim; do
    install -m 755 "$backup/$key" "${binaries[$key]}.rollback"
    mv -f "${binaries[$key]}.rollback" "${binaries[$key]}"
  done
  if (( had_config )); then
    install -m 644 "$backup/tiered-memory.conf" "$config"
  else
    rm -f "$config"
  fi
  restart_services || true
}
trap rollback ERR
# The setup validates the NUMA tmpfs and writes the new lazy-restore flags.
/usr/bin/bash "$project/scripts/setup-tiered-memory.sh" "$warm" "$cold" "${SUDO_USER:-root}"
systemctl stop cube-sandbox-cube-api.service cube-sandbox-cubemaster.service \
  cube-sandbox-cube-egress.service cube-sandbox-cubelet.service
for key in api master cubelet shim; do
  install -m 755 "${builds[$key]}" "${binaries[$key]}.clawbox-new"
  mv -f "${binaries[$key]}.clawbox-new" "${binaries[$key]}"
done
restart_services
for service in cubelet cube-egress cubemaster cube-api; do
  systemctl is-active --quiet "cube-sandbox-$service.service"
done
trap - ERR
echo "Incremental CubeSandbox installed. Backup: $backup"
