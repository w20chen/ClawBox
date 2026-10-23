#!/usr/bin/env bash
set -uo pipefail

goal=/data/clawbox-acceptance-20261022T102744Z
run=$goal/runs/swe-all503-c16-barrier-v2-20261022t140511z
run_id=swe-all503-c16-barrier-v2-20261022t140511z
barrier_root=$goal/barrier-v2-1792677889701476664
repair=$goal/repairs/barrier-capacity-20261022T134408Z
python=/home/weitianc/clawbox-swe-images-20261020/.venv/bin/python

echo "$(date -Is) bounded c16 smoke starting"
bash "$run/source/scripts/clawbox" --sudo --output-root "$run/results" \
  experiment launch "$run/experiment.yaml" --run-id "$run_id" \
  --qualification-concurrency 4 >"$run/launch.log" 2>&1 &
launch_pid=$!
echo "$launch_pid" >"$run/launch.pid"

released=0
while kill -0 "$launch_pid" 2>/dev/null; do
  formal_state=$run/results/$run_id/run-state.json
  if [[ -f "$formal_state" && "$released" == 0 ]]; then
    arrivals=0
    if [[ -f "$barrier_root/arrivals.jsonl" ]]; then
      arrivals=$(wc -l <"$barrier_root/arrivals.jsonl")
    fi
    if [[ "$arrivals" -eq 16 ]]; then
      sleep 2
      if PYTHONPATH="$run/source" "$python" \
        "$repair/verify-and-release-v2.py" >"$run/verify-release.log" 2>&1; then
        released=1
        echo "$(date -Is) verified and released target-16 barrier"
      else
        echo "$(date -Is) barrier verification failed; aborting owned run" >&2
        bash "$run/source/scripts/clawbox" --sudo --output-root "$run/results" \
          experiment abort "$run_id" --cleanup-workers 16 \
          --cleanup-timeout-seconds 600 >>"$run/verify-release.log" 2>&1 || true
        break
      fi
    fi
  fi
  sleep 5
done

wait "$launch_pid"
launch_status=$?
echo "$launch_status" >"$run/launch.exit-status"
bash "$run/source/scripts/clawbox" --output-root "$run/results" \
  experiment status "$run_id" >"$run/final-status.json" 2>"$run/final-status.stderr" || true

barrier_pid=$(cat "$repair/smoke2-server.pid")
if ps -p "$barrier_pid" -o args= | grep -F "$repair/barrier-server-v2.py" >/dev/null; then
  kill "$barrier_pid"
fi

echo "$(date -Is) bounded c16 smoke finished with launch status $launch_status"
exit "$launch_status"
