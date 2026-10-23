#!/usr/bin/env bash
set -uo pipefail

qualification_root=/data/clawbox-performance-20261022T154600Z/qualification/full-c1280-public-c64-final-v4-20261022T204100Z
launch_root=/data/clawbox-performance-20261022T154600Z/launch/full-c1280-public-v3-20261022T204100Z
results_root=/data/clawbox-performance-20261022T154600Z/runs
run_id=full-c1280-public-20261022t203000z
attempt_id=attempt-full-c1280-20261022t204100z
spec=/data/clawbox-performance-20261022T154600Z/full/full-c1280-public-20261022T201300Z/experiment.yaml
source_root=/home/weitianc/clawbox-performance-source-20261022T204000Z

while [[ ! -f "$qualification_root/exit-status" ]]; do
  sleep 5
done
qualification_status=$(<"$qualification_root/exit-status")
printf '%s\n' "$qualification_status" > "$launch_root/qualification-exit-status"
if [[ "$qualification_status" != 0 ]]; then
  printf '%s\n' "qualification failed; formal run not started" > "$launch_root/not-started"
  printf '%s\n' 10 > "$launch_root/exit-status"
  exit 10
fi

if [[ -e "$results_root/$run_id" ]]; then
  printf '%s\n' "result root already exists; refusing to reuse run ID" > "$launch_root/not-started"
  printf '%s\n' 11 > "$launch_root/exit-status"
  exit 11
fi

started=$(date +%s)
printf '%s\n' "$started" > "$launch_root/started-unix"
sha256sum "$spec" "$qualification_root/qualification.json" \
  > "$launch_root/frozen-input-sha256.txt"

set -a
. /home/weitianc/.config/clawbox/machine.env
set +a
cd "$source_root"

sudo -n -E /home/weitianc/miniconda3/bin/python -m clawbox.cli \
  --output-root "$results_root" experiment run "$spec" \
  --run-id "$run_id" --attempt-id "$attempt_id" --owner-id "$run_id" \
  --qualification "$qualification_root/qualification.json" \
  > "$launch_root/stdout.log" 2> "$launch_root/stderr.log"
rc=$?
finished=$(date +%s)
printf '%s\n' "$rc" > "$launch_root/exit-status"
printf '%s\n' "$finished" > "$launch_root/finished-unix"
printf '%s\n' "$((finished-started))" > "$launch_root/elapsed-seconds"
exit "$rc"
