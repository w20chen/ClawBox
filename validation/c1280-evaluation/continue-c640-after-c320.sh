#!/usr/bin/env bash
set -euo pipefail

study=/home/weitianc/clawbox-c1280-20261017
source_root=/home/weitianc/clawbox-high-density-followup-20261017/setup-source
c640_run=c1280-primary-c640-07
c640_spec="$study/configs/c640.yaml"
states=(
  "$study/results/c1280-screen-c320-os-06/run-state.json"
  "$study/results/c1280-screen-c320-predicted-06/run-state.json"
)

cd "$source_root"

while true; do
  readiness="$($source_root/.venv/bin/python - "${states[@]}" <<'PY'
import json
import sys
from pathlib import Path

from clawbox.experiments.results import ResultEnvelope
from clawbox.experiments.supervisor import is_trial_outcome

states = []
for value in sys.argv[1:]:
    path = Path(value)
    if not path.is_file():
        print('wait')
        raise SystemExit
    states.append(json.loads(path.read_text()))
if any(state.get('state') not in {'succeeded', 'failed'} for state in states):
    print('wait')
    raise SystemExit
for state in states:
    arms = list((state.get('arms') or {}).values())
    if not arms or not all(arm.get('cleanup_verified') is True for arm in arms):
        print('invalid')
        raise SystemExit
    for arm in arms:
        result_path = arm.get('result')
        if not result_path:
            print('invalid')
            raise SystemExit
        path = Path(result_path)
        if not path.is_file():
            print('invalid')
            raise SystemExit
        result = ResultEnvelope.model_validate_json(path.read_text())
        if not is_trial_outcome(result):
            print('invalid')
            raise SystemExit
print('ready')
PY
)"
  case "$readiness" in
    ready) break ;;
    invalid)
      echo "c320 ended without two valid, cleaned trial outcomes" >&2
      exit 1
      ;;
    wait) sleep 60 ;;
    *) echo "unexpected readiness value: $readiness" >&2; exit 1 ;;
  esac
done

while pgrep -f /root/agent-test-bench >/dev/null 2>&1; do
  echo "waiting for /root/agent-test-bench to leave CPUs 0-159"
  sleep 60
done

bash scripts/clawbox --machine-env "$study/configs/machine-c1280.env" \
  --output-root "$study/results" experiment validate "$c640_spec" --inputs
bash scripts/clawbox --machine-env "$study/configs/machine-c1280.env" --sudo \
  --output-root "$study/results" experiment launch "$c640_spec" \
  --run-id "$c640_run" --qualification-concurrency 2 --detach
