#!/usr/bin/env bash
set -uo pipefail

goal=/data/clawbox-acceptance-20261022T102744Z
run=$goal/runs/swe-all503-c1280-barrier-v2-20261022t144407z
run_id=swe-all503-c1280-barrier-v2-20261022t144407z
barrier_root=$goal/barrier-v2-1792680234440024545
repair=$goal/repairs/barrier-capacity-20261022T134408Z
python=/home/weitianc/clawbox-swe-images-20261020/.venv/bin/python

echo "$(date -Is) bounded-qualification c1280 acceptance starting"
bash "$run/source/scripts/clawbox" --sudo --output-root "$run/results" \
  experiment launch "$run/experiment.yaml" --run-id "$run_id" \
  --qualification-concurrency 16 >"$run/launch.log" 2>&1 &
launch_pid=$!
echo "$launch_pid" >"$run/launch.pid"

released=0
last_report=0
while kill -0 "$launch_pid" 2>/dev/null; do
  formal_state=$run/results/$run_id/run-state.json
  arrivals=0
  if [[ -f "$barrier_root/arrivals.jsonl" ]]; then
    arrivals=$(wc -l <"$barrier_root/arrivals.jsonl")
  fi
  now=$(date +%s)
  if (( now - last_report >= 60 )); then
    echo "$(date -Is) barrier arrivals=$arrivals"
    last_report=$now
  fi
  if [[ -f "$formal_state" && "$released" == 0 && "$arrivals" -eq 1280 ]]; then
    sleep 3
    if PYTHONPATH="$run/source" "$python" \
      "$repair/verify-and-release-v2.py" >"$run/verify-release.log" 2>&1; then
      released=1
      echo "$(date -Is) verified and released target-1280 barrier"
    else
      echo "$(date -Is) barrier verification failed; aborting owned run" >&2
      bash "$run/source/scripts/clawbox" --sudo --output-root "$run/results" \
        experiment abort "$run_id" --cleanup-workers 16 \
        --cleanup-timeout-seconds 900 >>"$run/verify-release.log" 2>&1 || true
      break
    fi
  fi
  sleep 5
done

wait "$launch_pid"
launch_status=$?
echo "$launch_status" >"$run/launch.exit-status"
bash "$run/source/scripts/clawbox" --output-root "$run/results" \
  experiment status "$run_id" >"$run/final-status.json" 2>"$run/final-status.stderr" || true

barrier_pid=$(cat "$repair/full1280-server.pid")
if ps -p "$barrier_pid" -o args= | grep -F "$repair/barrier-server-v2.py" >/dev/null; then
  kill "$barrier_pid"
fi

echo "$(date -Is) c1280 acceptance finished with launch status $launch_status"
exit "$launch_status"
