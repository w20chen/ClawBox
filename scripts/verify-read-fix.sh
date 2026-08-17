#!/bin/bash
# Verify the read-tool fix evidence in the ingester result for a task.
# Usage: bash verify-read-fix.sh <task_id>
set -u
TASK="${1:?usage: verify-read-fix.sh <task_id>}"
export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
IPOD=$(kubectl -n clawbox-system get pods -o jsonpath='{.items[?(@.metadata.labels.app\.kubernetes\.io/component=="trace-ingester")].metadata.name}')
echo "ingester pod: $IPOD"
kubectl -n clawbox-system exec "$IPOD" -- python3 -c "
import sqlite3, glob, json
dbs = glob.glob('/data/*.db')
conn = sqlite3.connect(dbs[0]); conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute('SELECT task_id, payload_sha256, length(payload), datetime(created_at) FROM task_results WHERE task_id=?', ('${TASK}',))
row = cur.fetchone()
print('result row:', tuple(row))
if row is None:
    raise SystemExit(1)
cur.execute('SELECT payload FROM task_results WHERE task_id=?', ('${TASK}',))
obj = json.loads(cur.fetchone()['payload'])
print('status:', obj.get('status'))
meta = obj.get('metadata', {})
print('agent_exit_code:', meta.get('agent_exit_code'), 'patch_status:', meta.get('patch_status'))
log = obj.get('logs', {}).get('agent', '')
print('agent.log len:', len(log))
print('escapes count:', log.count('Sandbox path escapes'))
print('read failed count:', sum(1 for l in log.splitlines() if 'read failed' in l))
print('exec calls:', log.count('exec command='))
print('llm transport responses:', log.count('[model-fetch] response'))
" 2>&1
echo "=== done ==="
