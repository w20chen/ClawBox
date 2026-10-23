#!/usr/bin/env bash
set -Eeuo pipefail

if (( $# != 2 )); then
  echo "usage: sudo $0 SOURCE_BINARY EVIDENCE_ROOT" >&2
  exit 64
fi

source_binary=$(realpath "$1")
evidence_root=$(realpath "$2")
installed=/usr/local/services/cubetoolbox/CubeAPI/bin/cube-api
dropin=/etc/systemd/system/cube-sandbox-cube-api.service.d/70-clawbox-create-timeout.conf

grep -qx 'populated 0' /sys/fs/cgroup/cube_sandbox/sandbox/cgroup.events || {
  echo "refusing API deployment while CubeSandbox VMs are running" >&2
  exit 1
}
[[ -x "$source_binary" ]] || { echo "missing built CubeAPI: $source_binary" >&2; exit 1; }
grep -aFq snapshot_mechanism "$source_binary" || {
  echo "built CubeAPI lacks tiered snapshot mechanism field" >&2
  exit 1
}
grep -aFq memory_snapshot_path "$source_binary" || {
  echo "built CubeAPI lacks tiered snapshot path field" >&2
  exit 1
}

mkdir -p "$evidence_root"
cp -p "$installed" "$evidence_root/cube-api.before"
[[ -f "$dropin" ]] || { echo "expected create-timeout override is absent: $dropin" >&2; exit 1; }
cp -p "$dropin" "$evidence_root/70-clawbox-create-timeout.conf.before"
sha256sum "$installed" "$source_binary" > "$evidence_root/binary-sha256.txt"

rollback() {
  trap - ERR
  install -m 755 "$evidence_root/cube-api.before" "$installed.rollback"
  mv -f "$installed.rollback" "$installed"
  install -m 644 "$evidence_root/70-clawbox-create-timeout.conf.before" "$dropin"
  systemctl daemon-reload
  systemctl restart cube-sandbox-cube-api.service || true
}
trap rollback ERR

systemctl stop cube-sandbox-cube-api.service
install -m 755 "$source_binary" "$installed.clawbox-new"
mv -f "$installed.clawbox-new" "$installed"
mv "$dropin" "$evidence_root/70-clawbox-create-timeout.conf.deactivated"
systemctl daemon-reload
systemctl start cube-sandbox-cube-api.service
systemctl is-active --quiet cube-sandbox-cube-api.service
systemctl show cube-sandbox-cube-api.service -p ExecMainStartTimestamp -p MainPID \
  > "$evidence_root/service-state.txt"
trap - ERR
echo "merged CubeAPI deployed"
