#!/usr/bin/env bash
set -euo pipefail

run_id=c1280-screen-c320-05
study_root=/home/weitianc/clawbox-c1280-20261017
source_root=/home/weitianc/clawbox-high-density-followup-20261017/setup-source
deadline=$((SECONDS + 3600))
quiet_checks=0

compute_idle_percent() {
  python3 - <<'PY'
import time

def snapshot():
    totals = [0, 0]
    with open('/proc/stat', encoding='ascii') as stream:
        for line in stream:
            name, *raw = line.split()
            if not name.startswith('cpu') or name == 'cpu':
                continue
            try:
                cpu = int(name[3:])
            except ValueError:
                continue
            if cpu > 159:
                continue
            values = [int(value) for value in raw]
            totals[0] += sum(values)
            totals[1] += values[3] + (values[4] if len(values) > 4 else 0)
    return totals

before = snapshot()
time.sleep(2)
after = snapshot()
elapsed = after[0] - before[0]
idle = after[1] - before[1]
print(100.0 * idle / elapsed if elapsed else 0.0)
PY
}

while (( SECONDS < deadline )); do
  idle=$(compute_idle_percent)
  if pgrep -f '[c]onfirm_page_center_candidates.py' >/dev/null; then
    quiet_checks=0
    printf '%s waiting: external benchmark present; compute idle %.1f%%\n' "$(date -Is)" "$idle"
  elif awk -v idle="$idle" 'BEGIN { exit !(idle >= 90.0) }'; then
    quiet_checks=$((quiet_checks + 1))
    printf '%s idle gate %d/3: compute idle %.1f%%\n' "$(date -Is)" "$quiet_checks" "$idle"
  else
    quiet_checks=0
    printf '%s waiting: compute idle %.1f%%\n' "$(date -Is)" "$idle"
  fi
  if (( quiet_checks >= 3 )); then
    break
  fi
  sleep 18
done

if (( quiet_checks < 3 )); then
  echo 'Host did not become idle within one hour; c320 was not launched.' >&2
  exit 75
fi
if pgrep -af '[s]cripts/clawbox.*experiment' >/dev/null; then
  echo 'Another ClawBox experiment process is already running; c320 was not launched.' >&2
  exit 76
fi
if [[ -e "$study_root/results/$run_id" ]]; then
  echo "Run ID already exists: $study_root/results/$run_id" >&2
  exit 77
fi

cd "$source_root"
exec bash scripts/clawbox \
  --machine-env "$study_root/configs/machine-c1280.env" \
  --sudo \
  --output-root "$study_root/results" \
  experiment launch "$study_root/configs/c320.yaml" \
  --run-id "$run_id" \
  --qualification "$study_root/configs/c320.yaml.qualification.json" \
  --qualification-concurrency 2
