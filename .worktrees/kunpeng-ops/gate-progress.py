import json
from pathlib import Path

root = Path('/home/weitianc/clawbox-tiered-study-20260907/raw-standalone/gate-tiered-policy-c4-v1')
for path in sorted((root / 'events').glob('*.jsonl')):
    counts = {}
    errors = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        event = row.get('event', '')
        counts[event] = counts.get(event, 0) + 1
        if event.endswith('failed'):
            errors.append(row)
    print(path.stem, {k: v for k, v in counts.items() if k in ('sandbox_paused', 'sandbox_restored', 'session_completed', 'tool_admitted')}, errors)
for path in sorted((root / 'model-gateway').glob('*.json')):
    if 'rejected-request' in path.name:
        print('REJECTION', path.name)
        continue
    rows = json.loads(path.read_text())
    print(path.stem, len(rows), sum(bool(r.get('delivered')) for r in rows))
if (root / 'summary.md').exists():
    print((root / 'summary.md').read_text())
