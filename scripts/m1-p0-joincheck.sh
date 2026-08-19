#!/usr/bin/env bash
# P0 post-run verification: fetch the run's span + bridge traces from the
# ingester, verify execution_id join (100%, no time window), and prove real
# DeepSeek API calls (LLM-kind spans + agent.log evidence).
# Usage: bash m1-p0-joincheck.sh [CR]
set -euo pipefail
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

IPOD=$(kubectl -n clawbox-system get pods \
  -l app.kubernetes.io/component=trace-ingester \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
[[ -n "$IPOD" ]] || { echo "no Running trace-ingester pod" >&2; exit 1; }
echo "ingester pod: $IPOD"
OUT=/tmp/p0-join
mkdir -p "$OUT"
find "$OUT" -mindepth 1 -maxdepth 1 -type f -delete
kubectl -n clawbox-system exec -i "$IPOD" -- python3 - "$CR" <<'PY' > "$OUT/traces.tar.gz"
import base64, io, json, os, sqlite3, glob, sys, tarfile
task = sys.argv[1]
dbs = glob.glob('/data/*.db')
if not dbs:
    raise SystemExit('no trace ingester database found under /data')
conn = sqlite3.connect(dbs[0])
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("""SELECT relative_path, offset, payload_base64, final FROM trace_chunks
               WHERE task_id=? ORDER BY relative_path, offset""", (task,))
files = {}
for row in cur.fetchall():
    rel = row['relative_path']
    files.setdefault(rel, []).append((row['offset'], row['payload_base64'], row['final']))
if not files:
    raise SystemExit(f'no trace chunks found for {task}')
with tarfile.open(fileobj=sys.stdout.buffer, mode='w:gz') as archive:
    for rel, chunks in files.items():
        data = b''.join(base64.b64decode(c[1]) for c in sorted(chunks, key=lambda c: c[0]))
        name = rel.replace('/', '_')
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
PY
tar -xzf "$OUT/traces.tar.gz" -C "$OUT"
rm -f "$OUT/traces.tar.gz"
echo "trace files:"
find "$OUT" -maxdepth 1 -type f -printf '  %f\n' | sort
echo "=== bridge records: execution_source + count ==="
python3 - "$OUT" <<'PY'
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
sources = Counter(b.get('execution_source') for b in bridges)
print('execution_source:', sources)
if not bridges:
    raise SystemExit('no bridge records found')
if sources.get('runtime-envelope', 0) == 0:
    raise SystemExit(f'no runtime-envelope bridge records found: {sources}')
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
if not span_ids or not tool_spans:
    raise SystemExit('no tool spans with execution IDs found')
if not llm_spans:
    raise SystemExit('no LLM spans found')
with open(os.path.join(out, 'span_ids.txt'), 'w') as fh:
    for s in sorted(span_ids):
        fh.write(s + '\n')
PY
echo "=== exact join check (span id in bridge ids) ==="
python3 - "$OUT" <<'PY'
import json, os, sys
out = sys.argv[1]
bridge_sources = {}
with open(os.path.join(out, 'bridges.jsonl')) as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        b = json.loads(line)
        eid = b.get('execution_id')
        if eid:
            bridge_sources[eid] = b.get('execution_source')
bridge_ids = set(bridge_sources)
span_ids = set(open(os.path.join(out, 'span_ids.txt')).read().split())
matched = span_ids & bridge_ids
print(f'spans={len(span_ids)} bridge_ids={len(bridge_ids)} matched={len(matched)}')
print('join_rate=', (len(matched) / len(span_ids)) if span_ids else 1.0)
print('UNMATCHED:', sorted(span_ids - bridge_ids)[:10])
if span_ids != matched:
    raise SystemExit('execution_id join rate is below 100%')
wrong_sources = {eid: bridge_sources[eid] for eid in span_ids
                 if bridge_sources.get(eid) != 'runtime-envelope'}
if wrong_sources:
    raise SystemExit(f'joined spans without runtime-envelope provenance: {wrong_sources}')
PY
echo "=== result acceptance ==="
DUMP_RESULT=/tmp/dump-result.sh
[[ -f "$DUMP_RESULT" ]] || DUMP_RESULT="$HOME/ClawBox/scripts/dump-result.sh"
RESULT="$(bash "$DUMP_RESULT" "$CR" 2>&1)"
printf '%s\n' "$RESULT" | grep -E 'status:|patch_status|patch len|final_answer len|agent_exit_code' | head -8
printf '%s\n' "$RESULT" | grep -Eqi 'status:[[:space:]]*(succeeded|cleaned)' || {
  echo "result is not successful" >&2
  exit 1
}
printf '%s\n' "$RESULT" | grep -Eq 'patch len:[[:space:]]*[1-9][0-9]*' || {
  echo "result patch is empty" >&2
  exit 1
}
echo "=== done ==="
