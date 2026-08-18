#!/usr/bin/env bash
# Extract the real patch content for one concurrent cell from the ingester.
OUT=/tmp/m1-conc-patch.txt
: > "$OUT"
{
  export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
  unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
  IPOD=$(kubectl -n clawbox-system get pods 2>/dev/null | grep 'clawbox-ingester' | grep Running | awk '{print $1}' | head -1)
  [ -z "$IPOD" ] && IPOD="clawbox-ingester-849dbcd594-rzgfs"
  echo "ingester pod: $IPOD"
  kubectl -n clawbox-system exec "$IPOD" -- python3 -c "
import sqlite3, glob, json
conn = sqlite3.connect(glob.glob('/data/*.db')[0]); conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute(\"SELECT payload FROM task_results WHERE task_id='run-01m0af6w13asy5edjhz8eqwczz-a1'\")
row = cur.fetchone()
if row:
    data = json.loads(row['payload'])
    p = data.get('patch') or data.get('result',{}).get('patch') or data
    print('KEYS:', list(data.keys())[:20])
    if isinstance(p, dict): p = p.get('patch','')
    print('PATCH_START:')
    print(str(p)[:1500])
else:
    print('no row')
" 2>&1
  echo "== done =="
} > "$OUT" 2>&1
echo written
