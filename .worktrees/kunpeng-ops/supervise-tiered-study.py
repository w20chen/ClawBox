#!/usr/bin/env python3
"""Continue the authorized kunpeng study independently of the coding agent."""
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

study = Path('/home/weitianc/clawbox-tiered-study-20260907')
gate = study / 'raw-standalone/gate-tiered-policy-c4-v1/summary.json'
run = study / 'raw-standalone/tiered-c40-20260908-v1'
repo = Path('/home/weitianc/ClawBox-experiment-b65b007')

def log(message):
    print(datetime.now(timezone.utc).isoformat(), message, flush=True)

log('Waiting for the running c4 gate; c40 will start only after both arms pass.')
deadline = time.monotonic() + 7200
while not gate.exists():
    if time.monotonic() >= deadline:
        raise SystemExit('STOPPED: c4 summary was not produced within two hours; inspect the c4 log.')
    time.sleep(30)
summary = json.loads(gate.read_text())
arms = summary['arms']
expected = {'tool-p90-tiered-lru-oracle-reactive', 'tool-p90-tiered-time-oracle-reactive'}
if len(arms) != 2 or {a['arm']['policy']['name'] for a in arms} != expected:
    raise SystemExit('STOPPED: unexpected c4 arm set.')
for arm in arms:
    correctness = arm['correctness']
    if not (arm['status'] == 'succeeded'
            and arm['arm']['concurrency'] == 4
            and correctness.get('completed_sessions') == 4
            and correctness.get('validation_passed') is True
            and correctness.get('native_tool_exact_id_join_rate') == 1.0
            and correctness.get('native_tool_telemetry_loss_total') == 0):
        raise SystemExit('STOPPED: c4 validation/telemetry gate failed: ' + arm['arm']['policy']['name'])
log('Both c4 arms passed, including validation and exact telemetry joins. Starting 13 c40 arms.')
if run.exists():
    raise SystemExit('STOPPED: c40 output already exists; refusing an accidental duplicate run.')
with (study / 'gates/tiered-c40-20260908-v1.log').open('x') as output:
    result = subprocess.run(['bash', '/home/weitianc/run-tiered-c40.sh'],
                            stdout=output, stderr=subprocess.STDOUT)
log(f'c40 worker exited with code {result.returncode}.')
with (study / 'c40-results.md').open('w') as output:
    subprocess.run([str(repo / '.venv/bin/python'), str(repo / 'scripts/report-standalone-study.py'),
                    str(run)], stdout=output, stderr=subprocess.STDOUT, check=True)
if not (run / 'summary.json').exists():
    raise SystemExit('STOPPED: c40 worker did not produce a final summary. Inspect its log.')
results = json.loads((run / 'summary.json').read_text())['arms']
passed = sum(a['status'] == 'succeeded' for a in results)
log(f'Finished: {passed}/{len(results)} c40 arms succeeded. Report: {study / "c40-results.md"}')
if len(results) != 13 or passed != 13:
    raise SystemExit('Some c40 arms need follow-up; failed results and logs are preserved.')
