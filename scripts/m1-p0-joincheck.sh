#!/usr/bin/env bash
# P0 post-run verification: fetch the run's span + bridge traces from the
# ingester, verify execution_id join (100%, no time window), and prove real
# DeepSeek API calls (LLM-kind spans + agent.log evidence).
# Usage: bash m1-p0-joincheck.sh [CR]
set -u
CR="${1:-}"
export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY

if [[ -z "$CR" ]]; then
  for f in /tmp/m1-kb-run.txt /tmp/m1-p0-run.txt; do
    if [[ -f "$f" ]]; then
      CR=$(grep '^CR=' "$f" | cut -d= -f2)
      [[ -n "$CR" ]] && break
    fi
  done
fi
CR="${CR:-$(kubectl get sandboxtask -n clawbox-benchmarks -o name 2>/dev/null | grep run- | head -1 | cut -d/ -f2)}"
echo "CR=$CR"

IPOD=$(kubectl -n clawbox-system get pods -o jsonpath='{.items[?(@.metadata.labels.app\.kubernetes\.io/component=="trace-ingester")].metadata.name}' 2>/dev/null)
echo "ingester pod: $IPOD"
OUT=/tmp/p0-join
mkdir -p "$OUT"
kubectl -n clawbox-system exec "$IPOD" -- python3 - "$CR" "$OUT" <<'PY'
import base64, json, os, sqlite3, glob, sys
task = sys.argv[1]
out = sys.argv[2]
dbs = glob.glob('/data/*.db')
conn = sqlite3.connect(dbs[0])
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("""SELECT relative_path, payload_base64, final FROM trace_chunks
               WHERE task_id=? ORDER BY relative_path, offset""", (task,))
files = {}
for row in cur.fetchall():
    rel = row['relative_path']
    files.setdefault(rel, []).append((row['offset'], row['payload_base64'], row['final']))
print('trace files:', sorted(files.keys()))
for rel, chunks in files.items():
    data = b''.join(base64.b64decode(c[1]) for c in sorted(chunks, key=lambda c: c[0]))
    name = rel.replace('/', '_')
    with open(os.path.join(out, name), 'wb') as fh:
        fh.write(data)
    print('wrote', name, len(data), 'bytes, final=', any(c[2] for c in chunks))
PY
echo "=== bridge records: execution_source + count ==="
grep -h '"execution_id"' "$OUT"/*tool-bridge*.jsonl 2>/dev/null | python3 - "$OUT" <<'PY'
import json, glob, os, sys
out = sys.argv[1]
bridges = []
for path in glob.glob(os.path.join(out, '*tool-bridge*.jsonl')):
    for line in open(path, encoding='utf-8', errors='replace'):
        line = line.strip()
        if not line:
            continue
        try:
            bridges.append(json.loads(line))
        except Exception:
            pass
print('bridge records:', len(bridges))
from collections import Counter
print('execution_source:', Counter(b.get('execution_source') for b in bridges))
with open(os.path.join(out, 'bridges.jsonl'), 'w') as fh:
    for b in bridges:
        fh.write(json.dumps(b) + '\n')
PY
echo "=== span records: execution_id set + LLM spans ==="
python3 - "$OUT" <<'PY'
import json, glob, os, sys
out = sys.argv[1]
span_ids = set()
llm_spans = 0
tool_spans = 0
for path in glob.glob(os.path.join(out, '*.jsonl')):
    if 'tool-bridge' in path:
        continue
    for line in open(path, encoding='utf-8', errors='replace'):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get('record_type') == 'span_end':
            e = (r.get('execution') or {}).get('execution_id')
            if e:
                span_ids.add(e)
            if r.get('kind') == 'llm':
                llm_spans += 1
            elif r.get('kind') == 'tool':
                tool_spans += 1
print('span execution_ids:', len(span_ids))
print('tool spans:', tool_spans, 'llm spans:', llm_spans)
with open(os.path.join(out, 'span_ids.txt'), 'w') as fh:
    for s in sorted(span_ids):
        fh.write(s + '\n')
PY
echo "=== exact join check (span id in bridge ids) ==="
python3 - "$OUT" <<'PY'
import json, os, sys
out = sys.argv[1]
bridge_ids = set()
with open(os.path.join(out, 'bridges.jsonl')) as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        b = json.loads(line)
        eid = b.get('execution_id')
        if eid:
            bridge_ids.add(eid)
span_ids = set(open(os.path.join(out, 'span_ids.txt')).read().split())
matched = span_ids & bridge_ids
print(f'spans={len(span_ids)} bridge_ids={len(bridge_ids)} matched={len(matched)}')
print('join_rate=', (len(matched) / len(span_ids)) if span_ids else 1.0)
print('UNMATCHED:', sorted(span_ids - bridge_ids)[:10])
PY
echo "=== DeepSeek evidence in result agent.log ==="
bash ~/ClawBox/scripts/dump-result.sh "$CR" 2>&1 | grep -iE 'deepseek|api\.deepseek' | head -5 || echo "(no deepseek string in result summary)"
echo "=== done ==="
