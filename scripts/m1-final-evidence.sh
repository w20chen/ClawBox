#!/usr/bin/env bash
# Final M1 real-task evidence: result, run state, patch commit, phases.
OUT=/tmp/m1-final-evidence.txt
: > "$OUT"
{
  export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
  unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
  TOKEN="clawbox-m1-smoke-token-0001"
  RUN_ID="01M0ADY8DNNWNJWVW7G42390RD"
  CR="run-01m0ady8dnnwnjwvw7g42390rd-a1"

  echo "== CR final status =="
  kubectl get sandboxtask -n clawbox-benchmarks $CR -o jsonpath='{.status}' 2>/dev/null; echo
  echo "== official ingester result (task=$CR) =="
  bash ~/ClawBox/scripts/dump-result.sh $CR 2>&1 | tail -12
  echo "== API run state =="
  curl -s http://127.0.0.1:8085/v1/runs/$RUN_ID -H "X-Clawbox-Token: $TOKEN" -H "X-Tenant-Id: tenant-a"; echo
  echo "== API run events =="
  curl -s http://127.0.0.1:8085/v1/runs/$RUN_ID/events -H "X-Clawbox-Token: $TOKEN" -H "X-Tenant-Id: tenant-a"; echo
  echo "== agent's real committed patch (first 40 lines) =="
  head -40 /tmp/m1-realpatch.txt
  echo "== leaked processes check =="
  ps -eo comm | grep -c firecracker
  echo "== final evidence done =="
} > "$OUT" 2>&1
echo written
