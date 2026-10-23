#!/usr/bin/env bash
set -uo pipefail

study=/data/clawbox-performance-20261022T154600Z
baseline_launch="$study/launch/full-c1280-optimized-v5-public-20261022T214100Z"
baseline_run="$study/runs/full-c1280-optimized-v5-public-20261022t214100z"
qualification_root="$study/qualification/full-c1280-optimized-v6-create50-public-c64-20261022T220200Z"
launch_root="$study/launch/full-c1280-optimized-v6-create50-public-20261022T220200Z"
results_root="$study/runs"
run_id=full-c1280-optimized-v6-create50-public-20261022t220200z
attempt_id=attempt-full-c1280-optimized-v6-create50-20261022t220200z
spec="$study/full/full-c1280-optimized-v6-create50-public-20261022T220200Z/experiment.yaml"
source_root=/home/weitianc/clawbox-performance-source-20261022T212900Z
host_config="$study/recovery/host-shared100-20261022T212900Z.yaml"
host_profile="$study/recovery/host-shared100-20261022T212900Z.json"
analyzer="$study/full/full-c1280-public-20261022T201300Z/analyze-full-latency.py"

while [[ ! -f "$baseline_launch/exit-status" ]]; do
  sleep 30
done
cp "$baseline_launch/exit-status" "$launch_root/baseline-exit-status"
cp "$baseline_run/run-state.json" "$launch_root/baseline-final-run-state.json"

if ! /home/weitianc/miniconda3/bin/python -c \
  'import json,sys; d=json.load(open(sys.argv[1])); arms=d.get("arms",{}).values(); sys.exit(0 if d.get("state") != "running" and arms and all(a.get("cleanup_verified") for a in arms) else 1)' \
  "$baseline_run/run-state.json"; then
  printf '%s\n' 'baseline cleanup was not verified; optimized run not started' \
    > "$launch_root/not-started"
  printf '%s\n' 20 > "$launch_root/exit-status"
  exit 20
fi

if [[ -e "$results_root/$run_id" ]]; then
  printf '%s\n' 'result root already exists; refusing to reuse run ID' \
    > "$launch_root/not-started"
  printf '%s\n' 21 > "$launch_root/exit-status"
  exit 21
fi

set -a
. /home/weitianc/.config/clawbox/machine.env
set +a
cd "$source_root"

bash scripts/clawbox experiment host check "$host_config" \
  > "$launch_root/host-check.json" 2> "$launch_root/host-check.stderr"
host_check_status=$?
printf '%s\n' "$host_check_status" > "$launch_root/host-check-exit-status"
if [[ "$host_check_status" != 0 ]]; then
  printf '%s\n' 'shared-pool host check failed; optimized run not started' \
    > "$launch_root/not-started"
  printf '%s\n' 23 > "$launch_root/exit-status"
  exit 23
fi

bash scripts/clawbox --sudo experiment host apply "$host_config" \
  --profile "$host_profile" \
  > "$launch_root/host-apply.json" 2> "$launch_root/host-apply.stderr"
host_apply_status=$?
printf '%s\n' "$host_apply_status" > "$launch_root/host-apply-exit-status"
if [[ "$host_apply_status" != 0 ]]; then
  printf '%s\n' 'shared-pool host apply failed; optimized run not started' \
    > "$launch_root/not-started"
  printf '%s\n' 24 > "$launch_root/exit-status"
  exit 24
fi

qualification_started=$(date +%s)
printf '%s\n' "$qualification_started" > "$qualification_root/started-unix"
sudo -n -E /home/weitianc/miniconda3/bin/python -m clawbox.cli \
  --output-root "$qualification_root/results" experiment qualify "$spec" \
  --concurrency 64 --receipt "$qualification_root/qualification.json" \
  > "$qualification_root/stdout.log" 2> "$qualification_root/stderr.log"
qualification_status=$?
qualification_finished=$(date +%s)
printf '%s\n' "$qualification_status" > "$qualification_root/exit-status"
printf '%s\n' "$qualification_finished" > "$qualification_root/finished-unix"
printf '%s\n' "$((qualification_finished-qualification_started))" \
  > "$qualification_root/elapsed-seconds"
if [[ "$qualification_status" != 0 ]]; then
  printf '%s\n' 'optimized qualification failed; formal run not started' \
    > "$launch_root/not-started"
  printf '%s\n' 22 > "$launch_root/exit-status"
  exit 22
fi

started=$(date +%s)
printf '%s\n' "$started" > "$launch_root/started-unix"
sha256sum "$spec" "$qualification_root/qualification.json" \
  "$host_config" "$host_profile" \
  "$source_root/clawbox/experiments/admission.py" \
  "$source_root/clawbox/experiments/policy.py" \
  "$source_root/clawbox/experiments/worker.py" \
  "$source_root/clawbox/experiments/spec.py" \
  "$source_root/clawbox/experiments/host.py" \
  "$source_root/clawbox/lab.py" \
  > "$launch_root/frozen-input-sha256.txt"

sudo -n -E /home/weitianc/miniconda3/bin/python -m clawbox.cli \
  --output-root "$results_root" experiment run "$spec" \
  --run-id "$run_id" --attempt-id "$attempt_id" --owner-id "$run_id" \
  --qualification "$qualification_root/qualification.json" \
  > "$launch_root/stdout.log" 2> "$launch_root/stderr.log"
run_status=$?
finished=$(date +%s)
printf '%s\n' "$run_status" > "$launch_root/formal-exit-status"
printf '%s\n' "$finished" > "$launch_root/finished-unix"
printf '%s\n' "$((finished-started))" > "$launch_root/elapsed-seconds"

event_path=$(find "$results_root/$run_id/attempts" -path '*/events/*.jsonl' -type f -print -quit)
if [[ -n "$event_path" ]]; then
  /home/weitianc/miniconda3/bin/python "$analyzer" "$event_path" \
    > "$launch_root/latency-analysis.json" \
    2> "$launch_root/latency-analysis.stderr"
  printf '%s\n' "$?" > "$launch_root/latency-analysis-exit-status"
fi
printf '%s\n' "$run_status" > "$launch_root/exit-status"
exit "$run_status"
