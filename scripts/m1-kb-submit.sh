#!/usr/bin/env bash
# Submit a fresh REAL task through the M1 API and watch its CR to terminal.
# Run on the target. Logs to /tmp/m1-kb-submit.log
set -u
OUT=/tmp/m1-kb-submit.log
LOCK=/tmp/m1-kb-submit.lock
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "another m1-kb-submit watcher is already running" >&2
  exit 1
fi
: > "$OUT"
{
  export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
  unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
  TOKEN="clawbox-m1-smoke-token-0001"
  DEADLINE="${1:-600}"
  export DEADLINE

  echo "== submit real task (deadline=${DEADLINE}s) =="
  IDKEY="m1-kb-p0-$(date +%s)"
  PAYLOAD="$(HOME="$HOME" python3 - <<PY
import json, hashlib, os
path = os.path.join(os.environ['HOME'], 'ClawBox', 'scripts', 'problem-scim2-13.txt')
problem = open(path, encoding='utf-8').read()
body = {
    "projectId": "m1-p0-kb",
    "templateRef": "swe-rebench-arm64",
    "templateRevision": 1,
    "inputRef": "15five__scim2-filter-parser-13",
    "inputSha256": hashlib.sha256(problem.encode()).hexdigest(),
    "deadlineSeconds": int(os.environ.get('DEADLINE', '600')),
    "idempotencyKey": "$IDKEY",
    "problemStatement": problem,
}
print(json.dumps(body))
PY
)"
  RESP=$(curl -s -X POST http://127.0.0.1:8085/v1/runs \
    -H "X-Clawbox-Token: $TOKEN" -H "X-Tenant-Id: tenant-a" \
    -H "Content-Type: application/json" -d "$PAYLOAD")
  echo "submit: $RESP"
  RUN_ID=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("runId",""))')
  CR="run-$(echo "$RUN_ID" | tr 'A-Z' 'a-z')-a1"
  echo "RUN_ID=$RUN_ID"
  echo "CR=$CR"
  echo "RUN_ID=$RUN_ID" > /tmp/m1-kb-run.txt
  echo "CR=$CR" >> /tmp/m1-kb-run.txt

  echo "== watch (up to $((DEADLINE + 900))s) =="
  MISSING=0
  for i in $(seq 1 $(((DEADLINE + 900) / 15))); do
    PH=$(kubectl get sandboxtask -n clawbox-benchmarks "$CR" -o jsonpath='{.status.phase}' 2>/dev/null)
    echo "  t=$((i*15))s CR=$PH"
    if [[ -z "$PH" ]]; then
      MISSING=$((MISSING + 1))
      if (( MISSING >= 4 )); then
        echo "CR missing for four consecutive polls; stopping watcher"
        break
      fi
    else
      MISSING=0
    fi
    case "$PH" in Cleaned|Failed|TimedOut) break;; esac
    sleep 15
  done
  echo "== final CR =="
  kubectl get sandboxtask -n clawbox-benchmarks "$CR" -o jsonpath='{.status}' 2>/dev/null; echo
  echo "== result =="
  DUMP_RESULT=/tmp/dump-result.sh
  [[ -f "$DUMP_RESULT" ]] || DUMP_RESULT="$HOME/ClawBox/scripts/dump-result.sh"
  bash "$DUMP_RESULT" "$CR" 2>&1 | grep -E 'status:|patch_status|patch len|final_answer len|agent_exit_code' | head -8
} > "$OUT" 2>&1
echo "submit done; see $OUT"
