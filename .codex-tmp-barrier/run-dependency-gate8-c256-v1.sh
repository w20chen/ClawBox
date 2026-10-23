#!/usr/bin/env bash
set -uo pipefail

study=/data/clawbox-performance-20261022T154600Z
name=dependency-gate8-poisson-c256-v1-20261023T074500CST
run_id=dependency-gate8-poisson-c256-v1-20261023t074500cst
attempt_id=attempt-dependency-gate8-poisson-c256-v1-20261023t074500cst
spec="$study/diagnostic/$name/experiment.yaml"
qualification_root="$study/qualification/${name}-c64"
launch_root="$study/launch/$name"
results_root="$study/runs"
source_root=/home/weitianc/clawbox-performance-source-dependency-gate-v1-20261023T073800CST
host_config="$study/recovery/host-shared100-20261022T212900Z.yaml"
host_profile="$study/recovery/host-shared100-20261022T212900Z.json"
analyzer="$study/full/full-c1280-public-20261022T201300Z/analyze-full-latency.py"

mkdir -p "$qualification_root" "$launch_root"
if [[ -e "$results_root/$run_id" ]]; then
  printf '%s\n' 'result root already exists; refusing to reuse run ID' > "$launch_root/not-started"
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
  printf '%s\n' 23 > "$launch_root/exit-status"
  exit 23
fi

bash scripts/clawbox --sudo experiment host apply "$host_config" \
  --profile "$host_profile" \
  > "$launch_root/host-apply.json" 2> "$launch_root/host-apply.stderr"
host_apply_status=$?
printf '%s\n' "$host_apply_status" > "$launch_root/host-apply-exit-status"
if [[ "$host_apply_status" != 0 ]]; then
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
  printf '%s\n' 22 > "$launch_root/exit-status"
  exit 22
fi

started=$(date +%s)
printf '%s\n' "$started" > "$launch_root/started-unix"
sha256sum "$spec" "$qualification_root/qualification.json" \
  "$host_config" "$host_profile" \
  "$source_root/clawbox/cube/lifecycle.py" \
  "$source_root/clawbox/experiments/admission.py" \
  "$source_root/clawbox/experiments/policy.py" \
  "$source_root/clawbox/experiments/spec.py" \
  "$source_root/clawbox/experiments/worker.py" \
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
