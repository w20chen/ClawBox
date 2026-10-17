#!/usr/bin/env bash
set -euo pipefail

study=/home/weitianc/clawbox-c1280-20261017
source_root=/home/weitianc/clawbox-high-density-followup-20261017/setup-source
os_run=c1280-screen-c320-os-06
predicted_run=c1280-screen-c320-predicted-06
os_state="$study/results/$os_run/run-state.json"
predicted_spec="$study/configs/c320-predicted-only.yaml"

while true; do
  readiness="$($source_root/.venv/bin/python - "$os_state" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    print('wait')
    raise SystemExit
state = json.loads(path.read_text())
if state.get('state') not in {'succeeded', 'failed'}:
    print('wait')
    raise SystemExit
arms = list((state.get('arms') or {}).values())
print('ready' if arms and all(arm.get('cleanup_verified') is True for arm in arms)
      else 'cleanup-failed')
PY
)"
  case "$readiness" in
    ready) break ;;
    cleanup-failed)
      echo "OS run reached a terminal state without verified cleanup: $os_state" >&2
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

cd "$source_root"
bash scripts/clawbox --machine-env "$study/configs/machine-c1280.env" \
  --output-root "$study/results" experiment validate "$predicted_spec" --inputs
bash scripts/clawbox --machine-env "$study/configs/machine-c1280.env" --sudo \
  --output-root "$study/results" experiment launch "$predicted_spec" \
  --run-id "$predicted_run" --qualification-concurrency 2 --detach
