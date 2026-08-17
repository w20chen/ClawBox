#!/bin/bash
# Dump final_answer for a task.
# Usage: bash dump-answer.sh <task_id>
set -u
TASK="${1:?usage: dump-answer.sh <task_id>}"
export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
IPOD=$(kubectl -n clawbox-system get pods -o jsonpath='{.items[?(@.metadata.labels.app\.kubernetes\.io/component=="trace-ingester")].metadata.name}')
kubectl -n clawbox-system exec "$IPOD" -- python3 -c "
import sqlite3, glob, json
dbs = glob.glob('/data/*.db')
conn = sqlite3.connect(dbs[0])
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute('SELECT payload FROM task_results WHERE task_id=?', ('${TASK}',))
row = cur.fetchone()
if row is None:
    print('no row')
    raise SystemExit(0)
obj = json.loads(row['payload'])
fa = obj.get('final_answer') or ''
print('final_answer len:', len(fa))
print('----')
print(fa[:6000])
" 2>&1
echo "=== done ==="
