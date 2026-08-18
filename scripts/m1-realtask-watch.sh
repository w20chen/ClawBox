#!/usr/bin/env bash
# Watch the real M1 task to terminal, then collect full evidence.
OUT=/tmp/m1-realtask-watch.txt
: > "$OUT"
{
  RUN_ID="01M0ADY8DNNWNJWVW7G42390RD"
  CR="run-01m0ady8dnnwnjwvw7g42390rd-a1"
  NS="clawbox-benchmarks"
  TOKEN="clawbox-m1-smoke-token-0001"

  echo "== waiting for $CR (poll 60s, max 35min) =="
  for i in $(seq 1 35); do
    PH=$(kubectl get sandboxtask -n $NS $CR -o jsonpath='{.status.phase}' 2>/dev/null)
    echo "  t=${i}m phase=$PH"
    case "$PH" in Cleaned|Failed|TimedOut) break;; esac
    sleep 60
  done

  echo "== CR final status =="
  kubectl get sandboxtask -n $NS $CR -o jsonpath='{.status}' 2>/dev/null; echo
  echo "== CR spec digest check =="
  kubectl get sandboxtask -n $NS $CR -o jsonpath='{.spec.problemStatement}' 2>/dev/null | head -c 120; echo

  echo "== runtime pod logs (tail 25) =="
  RPOD=$(kubectl get pods -n $NS -o jsonpath="{.items[?(@.metadata.labels.cell==\"$CR\")].metadata.name}" 2>/dev/null | tr ' ' '\n' | grep runtime | head -1)
  [ -n "$RPOD" ] || RPOD=$(kubectl get pods -n $NS 2>/dev/null | grep "$CR-runtime" | awk '{print $1}' | head -1)
  echo "runtime pod: $RPOD"
  [ -n "$RPOD" ] && kubectl logs -n $NS "$RPOD" --tail=25 2>&1 | tail -25

  echo "== ingester result =="
  export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
  unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
  bash ~/ClawBox/scripts/dump-result.sh 15five__scim2-filter-parser-13 2>&1 | tail -20

  echo "== API run state =="
  curl -s http://127.0.0.1:8085/v1/runs/$RUN_ID -H "X-Clawbox-Token: $TOKEN" -H "X-Tenant-Id: tenant-a"; echo
  echo "== API run events =="
  curl -s http://127.0.0.1:8085/v1/runs/$RUN_ID/events -H "X-Clawbox-Token: $TOKEN" -H "X-Tenant-Id: tenant-a"; echo
  echo "== watch done =="
} > "$OUT" 2>&1
echo written
