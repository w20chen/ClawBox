#!/usr/bin/env bash
# Summarize the ingester database: trace chunks per task + result rows.
# Usage: bash scripts/diag-ingester.sh   (run on the target host)
export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY

IPOD=$(kubectl -n clawbox-system get pods -o jsonpath='{.items[?(@.metadata.labels.app\.kubernetes\.io/component=="trace-ingester")].metadata.name}')
echo "ingester pod: $IPOD"

echo "=== find db file ==="
kubectl -n clawbox-system exec "$IPOD" -- sh -c 'ls -la /data/ 2>/dev/null; ls -la /data/*.db 2>/dev/null' 2>&1

echo "=== trace chunks per task (created_at min/max/count) ==="
kubectl -n clawbox-system exec "$IPOD" -- python3 -c "
import sqlite3, glob
dbs = glob.glob('/data/*.db')
print('dbs:', dbs)
if not dbs:
    raise SystemExit(1)
conn = sqlite3.connect(dbs[0])
cur = conn.cursor()
cur.execute(\"SELECT task_id, count(*), datetime(min(created_at)), datetime(max(created_at)) FROM trace_chunks GROUP BY task_id ORDER BY max(created_at)\")
for row in cur.fetchall():
    print('chunks', row)
cur.execute('SELECT task_id, payload_sha256, datetime(created_at) FROM task_results ORDER BY created_at')
for row in cur.fetchall():
    print('result', row)
" 2>&1

echo "=== done ==="
