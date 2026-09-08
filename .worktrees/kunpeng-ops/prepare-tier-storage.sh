#!/usr/bin/env bash
set -euo pipefail

warm=/data/cubelet/clawbox-tiered-20260907/warm
cold=/data/cubelet/clawbox-tiered-20260907/cold
owner=${CLAWBOX_OWNER:-weitianc}

mkdir -p "$warm" "$cold"
if ! mountpoint -q "$warm"; then
  if find "$warm" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "refusing to cover non-empty WARM directory: $warm" >&2
    exit 65
  fi
  mount -t tmpfs -o size=64G,mpol=bind:1,noswap clawbox-warm "$warm"
fi
chown "$owner:$owner" "$warm" "$cold"
chmod 0755 "$warm" "$cold"
findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS "$warm"
stat -c '%U:%G %a %n' "$warm" "$cold"
