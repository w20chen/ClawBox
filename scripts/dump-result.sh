#!/bin/bash
# Dump a task result summary from the ingester DB.
# Usage: bash dump-result.sh <task_id>
set -u
TASK="${1:?usage: dump-result.sh <task_id>}"
export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
IPOD=$(kubectl -n clawbox-system get pods -o jsonpath='{.items[?(@.metadata.labels.app\.kubernetes\.io/component=="trace-ingester")].metadata.name}')
echo "ingester pod: $IPOD"
echo "=== schema ==="
kubectl -n clawbox-system exec "$IPOD" -- python3 -c "
import sqlite3, glob
dbs = glob.glob('/data/*.db')
conn = sqlite3.connect(dbs[0])
cur = conn.cursor()
cur.execute('PRAGMA table_info(task_results)')
for row in cur.fetchall():
    print('col', row)
" 2>&1
echo "=== result for ${TASK} ==="
kubectl -n clawbox-system exec "$IPOD" -- python3 -c "
import sqlite3, glob, json
dbs = glob.glob('/data/*.db')
conn = sqlite3.connect(dbs[0])
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute('SELECT * FROM task_results WHERE task_id=?', ('${TASK}',))
row = cur.fetchone()
if row is None:
    print('no row')
    raise SystemExit(0)
d = dict(row)
print('row keys:', list(d.keys()))
payload = d.get('payload') or d.get('result') or ''
print('payload type:', type(payload).__name__, 'len:', len(payload))
if isinstance(payload, bytes):
    payload = payload.decode('utf-8', 'replace')
try:
    obj = json.loads(payload)
    print('status:', obj.get('status'))
    print('session_id:', obj.get('session_id'))
    print('metadata:', json.dumps(obj.get('metadata', {}), ensure_ascii=False)[:600])
    fa = obj.get('final_answer') or ''
    patch = obj.get('patch') or ''
    print('final_answer len:', len(fa))
    print('patch len:', len(patch))
    if patch:
        print('patch head:', patch[:400])
except Exception as e:
    print('parse error:', e)
    print('payload head:', payload[:500])
" 2>&1
echo "=== done ==="

