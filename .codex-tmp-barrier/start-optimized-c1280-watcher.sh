#!/usr/bin/env bash
set -eu

root=/data/clawbox-performance-20261022T154600Z/launch/full-c1280-optimized-public-20261022T211500Z
chmod +x "$root/run.sh"
nohup "$root/run.sh" > "$root/host-launch.log" 2>&1 < /dev/null &
printf '%s\n' "$!" > "$root/launcher.pid"
