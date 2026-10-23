#!/usr/bin/env bash
set -uo pipefail

study=/data/clawbox-performance-20261022T154600Z
name=package-caches-poisson-c256-v2-20261023T090500CST
run_id=package-caches-poisson-c256-v2-20261023t090500cst
attempt_id=attempt-package-caches-poisson-c256-v2-20261023t090500cst
spec="$study/diagnostic/$name/experiment.yaml"
qualification_root="$study/qualification/${name}-c64"
launch_root="$study/launch/$name"
results_root="$study/runs"
source_root=/home/weitianc/clawbox-performance-source-package-caches-v2-20261023T090000CST
host_config="$study/recovery/host-shared100-20261022T212900Z.yaml"
host_profile="$study/recovery/host-shared100-20261022T212900Z.json"
analyzer="$study/full/full-c1280-public-20261022T201300Z/analyze-full-latency.py"

mkdir -p "$qualification_root" "$launch_root"
if [[ -e "$results_root/$run_id" ]]; then
  echo 'result root already exists; refusing to reuse run ID' > "$launch_root/not-started"
  echo 21 > "$launch_root/exit-status"
  exit 21
fi

set -a
. /home/weitianc/.config/clawbox/machine.env
set +a
cd "$source_root"

sudo -n bash scripts/clawbox-package-cache status \
  > "$launch_root/apt-cache-status.txt" 2> "$launch_root/apt-cache-status.stderr"
apt_cache_status=$?
echo "$apt_cache_status" > "$launch_root/apt-cache-status-exit-status"
if [[ "$apt_cache_status" != 0 ]]; then
  echo 25 > "$launch_root/exit-status"
  exit 25
fi
curl -fsS -x http://193.124.7.2:3142 -o /dev/null \
  http://deb.debian.org/debian/dists/bookworm/InRelease \
  2> "$launch_root/apt-cache-probe.stderr"
apt_cache_probe_status=$?
echo "$apt_cache_probe_status" > "$launch_root/apt-cache-probe-exit-status"
if [[ "$apt_cache_probe_status" != 0 ]]; then
  echo 26 > "$launch_root/exit-status"
  exit 26
fi

sudo -n bash scripts/clawbox-pypi-cache status \
  > "$launch_root/pypi-cache-status.txt" 2> "$launch_root/pypi-cache-status.stderr"
pypi_cache_status=$?
echo "$pypi_cache_status" > "$launch_root/pypi-cache-status-exit-status"
if [[ "$pypi_cache_status" != 0 ]]; then
  echo 27 > "$launch_root/exit-status"
  exit 27
fi
curl -fsS -o /dev/null \
  http://193.124.7.2:3141/root/pypi/+simple/pytest/ \
  2> "$launch_root/pypi-cache-probe.stderr"
pypi_cache_probe_status=$?
echo "$pypi_cache_probe_status" > "$launch_root/pypi-cache-probe-exit-status"
if [[ "$pypi_cache_probe_status" != 0 ]]; then
  echo 28 > "$launch_root/exit-status"
  exit 28
fi

bash scripts/clawbox experiment host check "$host_config" \
  > "$launch_root/host-check.json" 2> "$launch_root/host-check.stderr"
host_check_status=$?
echo "$host_check_status" > "$launch_root/host-check-exit-status"
if [[ "$host_check_status" != 0 ]]; then
  echo 23 > "$launch_root/exit-status"
  exit 23
fi

bash scripts/clawbox --sudo experiment host apply "$host_config" \
  --profile "$host_profile" \
  > "$launch_root/host-apply.json" 2> "$launch_root/host-apply.stderr"
host_apply_status=$?
echo "$host_apply_status" > "$launch_root/host-apply-exit-status"
if [[ "$host_apply_status" != 0 ]]; then
  echo 24 > "$launch_root/exit-status"
  exit 24
fi

qualification_started=$(date +%s)
echo "$qualification_started" > "$qualification_root/started-unix"
sudo -n -E /home/weitianc/miniconda3/bin/python -m clawbox.cli \
  --output-root "$qualification_root/results" experiment qualify "$spec" \
  --concurrency 64 --receipt "$qualification_root/qualification.json" \
  > "$qualification_root/stdout.log" 2> "$qualification_root/stderr.log"
qualification_status=$?
qualification_finished=$(date +%s)
echo "$qualification_status" > "$qualification_root/exit-status"
echo "$qualification_finished" > "$qualification_root/finished-unix"
echo "$((qualification_finished-qualification_started))" \
  > "$qualification_root/elapsed-seconds"
if [[ "$qualification_status" != 0 ]]; then
  echo 22 > "$launch_root/exit-status"
  exit 22
fi

started=$(date +%s)
echo "$started" > "$launch_root/started-unix"
sha256sum "$spec" "$qualification_root/qualification.json" \
  "$host_config" "$host_profile" \
  "$source_root/clawbox/cube/lifecycle.py" \
  "$source_root/clawbox/experiments/admission.py" \
  "$source_root/clawbox/experiments/openclaw_driver.py" \
  "$source_root/clawbox/experiments/policy.py" \
  "$source_root/clawbox/experiments/spec.py" \
  "$source_root/clawbox/experiments/worker.py" \
  "$source_root/deploy/apt-cache/Dockerfile" \
  "$source_root/deploy/pypi-cache/Dockerfile" \
  "$source_root/deploy/pypi-cache/start-devpi.sh" \
  "$source_root/scripts/clawbox-package-cache" \
  "$source_root/scripts/clawbox-pypi-cache" \
  > "$launch_root/frozen-input-sha256.txt"

sudo -n -E /home/weitianc/miniconda3/bin/python -m clawbox.cli \
  --output-root "$results_root" experiment run "$spec" \
  --run-id "$run_id" --attempt-id "$attempt_id" --owner-id "$run_id" \
  --qualification "$qualification_root/qualification.json" \
  > "$launch_root/stdout.log" 2> "$launch_root/stderr.log"
run_status=$?
finished=$(date +%s)
echo "$run_status" > "$launch_root/formal-exit-status"
echo "$finished" > "$launch_root/finished-unix"
echo "$((finished-started))" > "$launch_root/elapsed-seconds"

event_path=$(find "$results_root/$run_id/attempts" -path '*/events/*.jsonl' -type f -print -quit)
if [[ -n "$event_path" ]]; then
  /home/weitianc/miniconda3/bin/python "$analyzer" "$event_path" \
    > "$launch_root/latency-analysis.json" \
    2> "$launch_root/latency-analysis.stderr"
  echo "$?" > "$launch_root/latency-analysis-exit-status"
fi
echo "$run_status" > "$launch_root/exit-status"
exit "$run_status"
