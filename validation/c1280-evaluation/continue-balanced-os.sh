#!/usr/bin/env bash
set -euo pipefail
study=/home/weitianc/clawbox-c1280-20261017
source_root=/home/weitianc/clawbox-high-density-followup-20261017/setup-source
full=real-trace-p0-c640-balanced-aoff-s075-01
next_run=real-trace-p0-c640-balanced-os-s075-01
spec="$study/configs/real-trace-p0-c640-balanced-os-s075.yaml"
cd "$source_root"
exec 9>"$study/balanced-os-continuation.lock"
flock -n 9 || exit 1
while true; do
  verdict="$(./.venv/bin/python - "$study/results/$full/run-state.json" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
if not p.exists():
 print('waiting');raise SystemExit
s=json.loads(p.read_text())
if s['state'] not in {'failed','succeeded','cancelled','destroyed'}:
 print('waiting')
else:
 print('clean' if s['arms'] and all(a.get('cleanup_verified') is True for a in s['arms'].values()) else 'needs_cleanup')
PY
)"
  case "$verdict" in
    clean) break ;;
    waiting) sleep 20 ;;
    *) echo "Previous run needs verified cleanup; no next run launched."; exit 1 ;;
  esac
done
if [[ -e "$study/results/$next_run/run-state.json" ]]; then
  echo "Run already exists; refusing duplicate launch: $next_run"
  exit 0
fi
set -a
source "$study/configs/machine-c1280.env"
set +a
export CLAWBOX_SANDBOX_CREATE_CONCURRENCY=8
./scripts/clawbox --machine-env "$study/configs/machine-c1280.env" --sudo \
  --output-root "$study/results" experiment qualify --concurrency 2 \
  --receipt "$spec.qualification.json" "$spec" >"$study/balanced-os-qualification.log" 2>&1
./scripts/clawbox --machine-env "$study/configs/machine-c1280.env" --sudo \
  --output-root "$study/results" experiment launch --run-id "$next_run" \
  --qualification "$spec.qualification.json" --detach "$spec"
