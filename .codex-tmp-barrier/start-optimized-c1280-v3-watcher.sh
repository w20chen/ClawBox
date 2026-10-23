#!/usr/bin/env bash
set -eu

root=/data/clawbox-performance-20261022T154600Z/launch/full-c1280-optimized-v6-create50-public-20261022T220200Z
chmod +x "$root/run.sh"
nohup "$root/run.sh" > "$root/host-launch.log" 2>&1 < /dev/null &
printf '%s\n' "$!" > "$root/launcher.pid"
