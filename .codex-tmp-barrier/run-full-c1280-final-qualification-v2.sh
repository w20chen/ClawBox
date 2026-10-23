#!/usr/bin/env bash
set -uo pipefail

root=/data/clawbox-performance-20261022T154600Z/qualification/full-c1280-public-c64-final-v2-20261022T202500Z
spec=/data/clawbox-performance-20261022T154600Z/full/full-c1280-public-20261022T201300Z/experiment.yaml
source_root=/home/weitianc/clawbox-performance-source-20261022T202400Z
started=$(date +%s)
printf '%s\n' "$started" > "$root/started-unix"

set -a
. /home/weitianc/.config/clawbox/machine.env
set +a
cd "$source_root"

sudo -n -E /home/weitianc/miniconda3/bin/python -m clawbox.cli \
  --output-root "$root/results" experiment qualify "$spec" \
  --concurrency 64 --receipt "$root/qualification.json" \
  > "$root/stdout.log" 2> "$root/stderr.log"
rc=$?
finished=$(date +%s)
printf '%s\n' "$rc" > "$root/exit-status"
printf '%s\n' "$finished" > "$root/finished-unix"
printf '%s\n' "$((finished-started))" > "$root/elapsed-seconds"
exit "$rc"
