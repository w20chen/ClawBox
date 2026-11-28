#!/usr/bin/env bash
# Install a tested four-component CubeSandbox build on an idle standalone host.
set -Eeuo pipefail
if [[ ${1:-} == --cubelet-only ]]; then
  (( $# == 2 )) || { echo 'usage: sudo bash scripts/install-incremental-cubesandbox.sh --cubelet-only BUILT_CUBELET' >&2; exit 64; }
  (( EUID == 0 )) || { echo 'run as root' >&2; exit 1; }
  source_cubelet=$(realpath "$2")
  target_cubelet=/usr/local/services/cubetoolbox/Cubelet/bin/cubelet
  [[ -x "$source_cubelet" && -x "$target_cubelet" && "$source_cubelet" != "$target_cubelet" ]] || {
    echo 'supply a distinct tested Cubelet executable; install CubeSandbox first' >&2; exit 1;
  }
  install -d -m 700 /run/clawbox
  exec 9>/run/clawbox/base-cache.lock
  flock -n 9 || { echo 'experiment/cache maintenance is active' >&2; exit 1; }
  grep -qx 'populated 0' /sys/fs/cgroup/cube_sandbox/sandbox/cgroup.events || {
    echo 'finish or abort owned experiments before replacing Cubelet' >&2; exit 1;
  }
  python3 - <<'PY'
from pathlib import Path
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit(): continue
    try: name = Path((entry/'cmdline').read_bytes().split(b'\0')[0].decode()).name
    except (FileNotFoundError, ProcessLookupError): continue
    if name in {'containerd-shim-cube-rs', 'cloud-hypervisor', 'cube-hypervisor'}:
        raise SystemExit('live VM process ' + entry.name + '; finish/clean VMs before installation')
PY
  if cmp -s "$source_cubelet" "$target_cubelet" &&
      systemctl is-active --quiet cube-sandbox-cubelet.service cube-sandbox-cube-egress.service; then
    echo 'Cubelet binary already installed and services active; no restart.'
    exit 0
  fi
  install -d -m 700 /data/clawbox-backend-backups
  backup=$(mktemp -d /data/clawbox-backend-backups/cubelet-XXXXXXXX)
  cp -p "$target_cubelet" "$backup/cubelet"
  sha256sum "$source_cubelet" "$backup/cubelet" > "$backup/before.sha256"
  rollback_cubelet() {
    trap - ERR
    systemctl stop cube-sandbox-cube-egress.service
    systemctl stop cube-sandbox-cubelet.service
    install -m 755 "$backup/cubelet" "$target_cubelet.rollback"
    mv -f "$target_cubelet.rollback" "$target_cubelet"
    systemctl start cube-sandbox-cubelet.service
    systemctl start cube-sandbox-cube-egress.service
    echo "Cubelet installation failed; restored $backup/cubelet" >&2
  }
  trap rollback_cubelet ERR
  systemctl stop cube-sandbox-cube-egress.service
  systemctl stop cube-sandbox-cubelet.service
  install -m 755 "$source_cubelet" "$target_cubelet.clawbox-new"
  mv -f "$target_cubelet.clawbox-new" "$target_cubelet"
  systemctl start cube-sandbox-cubelet.service
  systemctl start cube-sandbox-cube-egress.service
  systemctl is-active --quiet cube-sandbox-cubelet.service cube-sandbox-cube-egress.service
  sha256sum "$target_cubelet" > "$backup/installed.sha256"
  trap - ERR
  echo "Cubelet installed; verify real VM checkpoint/spill/restore before experiments. Backup: $backup"
  exit 0
fi
if [[ ${1:-} == --lifecycle-components ]]; then
  (( $# == 5 )) || { echo 'usage: sudo bash scripts/install-incremental-cubesandbox.sh --lifecycle-components CUBE_SOURCE CLAWBOX_DIR WARM_ROOT COLD_ROOT' >&2; exit 64; }
  (( EUID == 0 )) || { echo 'run as root' >&2; exit 1; }
  cube_source=$(realpath "$2")
  project=$(realpath "$3")
  warm=$(realpath "$4")
  cold=$(realpath "$5")
  [[ "$warm" != "$cold" && $(findmnt -n -o FSTYPE --target "$warm") == tmpfs && $(stat -c %d "$warm") != $(stat -c %d "$cold") ]] || {
    echo 'supply existing WARM tmpfs and COLD disk roots on distinct filesystems' >&2; exit 1;
  }
  grep -q 'CLAWBOX_CROSS_TIER_LINEAGE' "$cube_source/hypervisor/vmm/src/snapshot_lineage.rs" || {
    echo 'prepare and build the current managed-lineage patch set first' >&2; exit 1;
  }
  install -d -m 700 /run/clawbox
  exec 9>/run/clawbox/base-cache.lock
  flock -n 9 || { echo 'experiment/cache maintenance is active' >&2; exit 1; }
  [[ ! -e /run/clawbox/base-cache.json ]] || { echo 'run experiment base-cache cleanup before replacing the Shim' >&2; exit 1; }
  grep -qx 'populated 0' /sys/fs/cgroup/cube_sandbox/sandbox/cgroup.events || {
    echo 'finish or abort experiments before installing lifecycle components' >&2; exit 1;
  }
  python3 - <<'PY'
from pathlib import Path
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit(): continue
    try: name = Path((entry/'cmdline').read_bytes().split(b'\0')[0].decode()).name
    except (FileNotFoundError, ProcessLookupError): continue
    if name in {'containerd-shim-cube-rs', 'cloud-hypervisor', 'cube-hypervisor'}:
        raise SystemExit('live VM process ' + entry.name + '; clean VMs before installation')
PY
  declare -A builds=(
    [cubelet]="$cube_source/_output/bin/cubelet"
    [shim]="$cube_source/CubeShim/target/release/containerd-shim-cube-rs"
    [runtime]="$cube_source/CubeShim/target/release/cube-runtime"
  )
  declare -A targets=(
    [cubelet]="/usr/local/services/cubetoolbox/Cubelet/bin/cubelet"
    [shim]="/usr/local/services/cubetoolbox/cube-shim/bin/containerd-shim-cube-rs"
    [runtime]="/usr/local/services/cubetoolbox/cube-shim/bin/cube-runtime"
    [component-shim]="/data/cubelet/root/component_versions/cube-shim/v0.7.0-clawbox-standalone/bin/containerd-shim-cube-rs"
    [component-runtime]="/data/cubelet/root/component_versions/cube-shim/v0.7.0-clawbox-standalone/bin/cube-runtime"
    [codec]="/usr/local/bin/clawbox-zstd-page-snapshot"
  )
  builds[component-shim]=${builds[shim]}
  builds[component-runtime]=${builds[runtime]}
  for key in cubelet shim runtime component-shim component-runtime; do
    [[ -x ${builds[$key]} && -x ${targets[$key]} ]] || { echo "missing built/installed $key" >&2; exit 1; }
  done
  install -d -m 700 /data/clawbox-backend-backups
  backup=$(mktemp -d /data/clawbox-backend-backups/lifecycle-XXXXXXXX)
  bash "$project/scripts/build-zstd-page-snapshot.sh" "$backup/codec-new"
  builds[codec]="$backup/codec-new"
  for key in "${!targets[@]}"; do cp -p "${targets[$key]}" "$backup/$key"; done
  config=/etc/systemd/system/cube-sandbox-cubelet.service.d/lifecycle-memory.conf
  had_config=0
  if [[ -f "$config" ]]; then cp -p "$config" "$backup/lifecycle-memory.conf"; had_config=1; fi
  printf '[Service]\nEnvironment=CLAWBOX_CROSS_TIER_LINEAGE=1\nEnvironment=CLAWBOX_KVM_DIRTY_TRACKING=1\nEnvironment=CUBE_RESTORE_PRIVATE_COPY=0\nEnvironment="CLAWBOX_WARM_SNAPSHOT_ROOT=%s"\nEnvironment="CLAWBOX_COLD_SNAPSHOT_ROOT=%s"\nEnvironment=CLAWBOX_CHECKPOINT_LOG=/data/log/clawbox-checkpoint-phases.jsonl\n' "$warm" "$cold" > "$backup/proposed.conf"
  cat "$backup/proposed.conf"
  restart_cubelet=1
  if cmp -s "${builds[cubelet]}" "${targets[cubelet]}" &&
      cmp -s "$backup/proposed.conf" "$config" &&
      systemctl is-active --quiet cube-sandbox-cubelet.service cube-sandbox-cube-egress.service; then
    restart_cubelet=0
    echo 'Cubelet binary and settings match; keeping the existing TAP pool and service process.'
  fi
  rollback_lifecycle() {
    trap - ERR
    for key in "${!targets[@]}"; do
      install -m 755 "$backup/$key" "${targets[$key]}.rollback"
      mv -f "${targets[$key]}.rollback" "${targets[$key]}"
    done
    if (( had_config )); then install -m 644 "$backup/lifecycle-memory.conf" "$config"; else rm -f "$config"; fi
    systemctl daemon-reload
    systemctl start cube-sandbox-cubelet.service
    systemctl start cube-sandbox-cube-egress.service
  }
  trap rollback_lifecycle ERR
  if (( restart_cubelet )); then
    systemctl stop cube-sandbox-cube-egress.service
    systemctl stop cube-sandbox-cubelet.service
  fi
  for key in "${!targets[@]}"; do
    install -m 755 "${builds[$key]}" "${targets[$key]}.clawbox-new"
    mv -f "${targets[$key]}.clawbox-new" "${targets[$key]}"
  done
  install -D -m 644 "$backup/proposed.conf" "$config"
  systemctl daemon-reload
  systemctl start cube-sandbox-cubelet.service
  systemctl start cube-sandbox-cube-egress.service
  systemctl is-active --quiet cube-sandbox-cubelet.service cube-sandbox-cube-egress.service
  sha256sum "${targets[@]}" > "$backup/installed.sha256"
  trap - ERR
  echo "Lifecycle components installed; qualify real VM restore before experiments. Backup: $backup"
  exit 0
fi
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
