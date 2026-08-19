#!/usr/bin/env bash
# Prevent generic filesystem probing from blocking ClawBox devmapper snapshots.
# The devmapper snapshotter mounts these devices directly; udev only needs to
# create /dev/mapper/<DM_NAME> and complete the dm cookie.
set -euo pipefail

RULE=/etc/udev/rules.d/10-z-clawbox-devmapper.rules
MARKER=clawbox-devmapper-noscan-v1

if [[ "$(id -u)" -ne 0 ]]; then
  echo "must run as root: sudo bash $0" >&2
  exit 1
fi

if [[ -e "$RULE" ]] && ! grep -q "$MARKER" "$RULE"; then
  echo "refusing to overwrite unrelated existing rule: $RULE" >&2
  exit 1
fi

cat >"$RULE" <<'EOF'
# clawbox-devmapper-noscan-v1
# 10-dm.rules has already populated DM_NAME. Run before 11/13-dm and generic
# persistent-storage rules so large thin snapshots are not probed by blkid.
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="dm-*", ENV{DM_NAME}=="clawbox-fc--pool-snap-*", ENV{DM_NOSCAN}="1", ENV{DM_UDEV_DISABLE_DISK_RULES_FLAG}="1", ENV{DM_UDEV_DISABLE_OTHER_RULES_FLAG}="1"
EOF
chmod 0644 "$RULE"
udevadm control --reload-rules

echo "installed $RULE"
echo "rollback: sudo rm -f $RULE && sudo udevadm control --reload-rules"
