#!/usr/bin/env bash
# M1 live evidence: dump run state + events via the API, wait for the cell to
# reach a terminal phase, then show CR + run + dispatcher logs.
OUT=/tmp/m1-evidence.txt
: > "$OUT"
{
  TOKEN="clawbox-m1-smoke-token-0001"
  RUN_ID="01M0ACVX2JXCVAHWPNAZBR5F2P"
  echo "== run state =="
  curl -s http://127.0.0.1:8085/v1/runs/$RUN_ID -H "X-Clawbox-Token: $TOKEN" -H "X-Tenant-Id: tenant-a"
  echo
  echo "== run events =="
  curl -s http://127.0.0.1:8085/v1/runs/$RUN_ID/events -H "X-Clawbox-Token: $TOKEN" -H "X-Tenant-Id: tenant-a"
  echo
  echo "== attempts =="
  curl -s http://127.0.0.1:8085/v1/runs/$RUN_ID/attempts -H "X-Clawbox-Token: $TOKEN" -H "X-Tenant-Id: tenant-a"
  echo
  echo "== CR terminal wait (max 300s) =="
  for i in $(seq 1 60); do
    PH=$(kubectl get sandboxtask -n clawbox-benchmarks run-01m0acvx2jxcvahwpnazbr5f2p-a1 -o jsonpath='{.status.phase}' 2>/dev/null)
    echo "  phase=$PH"
    case "$PH" in Cleaned|Failed|TimedOut) break;; esac
    sleep 5
  done
  echo "== CR final =="
  kubectl get sandboxtask -n clawbox-benchmarks run-01m0acvx2jxcvahwpnazbr5f2p-a1 -o jsonpath='{.status}' 2>/dev/null
  echo
  echo "== dispatcher logs =="
  docker logs clawbox-m1-dispatcher 2>&1 | tail -12
  echo "== evidence done =="
} > "$OUT" 2>&1
echo written
