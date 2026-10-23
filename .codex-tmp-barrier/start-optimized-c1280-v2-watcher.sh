#!/usr/bin/env bash
set -eu

root=/data/clawbox-performance-20261022T154600Z/launch/full-c1280-optimized-v2-public-20261022T212200Z
chmod +x "$root/run.sh"
nohup "$root/run.sh" > "$root/host-launch.log" 2>&1 < /dev/null &
printf '%s\n' "$!" > "$root/launcher.pid"
