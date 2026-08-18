#!/usr/bin/env bash
# Submit N concurrent REAL tasks via the M1 API with a short 3-min deadline,
# then watch all cells reach terminal. No code change needed: the API's
# deadlineSeconds flows to the CR timeoutSeconds -> agent --timeout.
OUT=/tmp/m1-concurrent.txt
: > "$OUT"
{
  TOKEN="clawbox-m1-smoke-token-0001"
  API="http://127.0.0.1:8085"
  NS="clawbox-benchmarks"
  N=3

  echo "== health =="
  curl -s "$API/healthz"; echo

  echo "== submit $N concurrent runs (deadline 180s) =="
  RUN_IDS=""
  for i in $(seq 1 "$N"); do
    PAYLOAD="$(HOME="$HOME" python3 - <<PY
import json, hashlib, os
path = os.path.join(os.environ['HOME'], 'ClawBox', 'scripts', 'problem-scim2-13.txt')
problem = open(path, encoding='utf-8').read()
body = {
    "projectId": "m1-concurrent",
    "templateRef": "swe-rebench-arm64",
    "templateRevision": 1,
    "inputRef": "15five__scim2-filter-parser-13",
    "inputSha256": hashlib.sha256(problem.encode()).hexdigest(),
    "deadlineSeconds": 180,
    "idempotencyKey": "m1-conc-00$i",
    "problemStatement": problem,
}
print(json.dumps(body))
PY
)"
    RESP=$(curl -s -X POST "$API/v1/runs" \
      -H "X-Clawbox-Token: $TOKEN" -H "X-Tenant-Id: tenant-a" \
      -H "Content-Type: application/json" -d "$PAYLOAD")
    echo "  run#$i -> $RESP"
    RID=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["runId"])' 2>/dev/null)
    RUN_IDS="$RUN_IDS $RID"
  done
  echo "$RUN_IDS" > /tmp/m1-conc-runids.txt
  # CR names are the lowercased run ids -> run-<rid>-a1
  GREP=""
  for RID in $RUN_IDS; do
    GREP="$GREP run-$(echo "$RID" | tr 'A-Z' 'a-z')-a1"
  done
  echo "tracking CRs:$GREP"

  echo "== watch CRs (every 30s, 12 rounds) =="
  for r in $(seq 1 12); do
    echo "--- t=${r}x30s ---"
    kubectl get sandboxtask -n "$NS" 2>/dev/null | grep -E "$(echo "$GREP" | tr ' ' '|')" | tail -8
    TERMINAL=0
    TOTAL=0
    for CR in $GREP; do
      PH=$(kubectl get sandboxtask -n "$NS" "$CR" -o jsonpath='{.status.phase}' 2>/dev/null)
      TOTAL=$((TOTAL+1))
      case "$PH" in Cleaned|Failed|TimedOut) TERMINAL=$((TERMINAL+1));; esac
    done
    echo "terminal=$TERMINAL total=$TOTAL"
    [ "$TERMINAL" -ge "$N" ] && break
    sleep 30
  done
  echo "== final CR list =="
  for CR in $GREP; do
    kubectl get sandboxtask -n "$NS" "$CR" -o jsonpath='{.metadata.name} phase={.status.phase} outcome={.status.outcome}{"\n"}' 2>/dev/null
  done
  echo "== concurrent done =="
} > "$OUT" 2>&1
echo written
