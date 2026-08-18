#!/usr/bin/env bash
# Final evidence for the 3 concurrent runs: CR phases, ingester results, leak check.
OUT=/tmp/m1-conc-final.txt
: > "$OUT"
{
  export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
  unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
  echo "== concurrent run CRs =="
  for RID in $(cat /tmp/m1-conc-runids.txt 2>/dev/null); do
    CR="run-$(echo "$RID" | tr 'A-Z' 'a-z')-a1"
    printf '%-42s ' "$CR"
    kubectl get sandboxtask -n clawbox-benchmarks "$CR" -o jsonpath='phase={.status.phase} outcome={.status.outcome}{"\n"}' 2>/dev/null
  done
  echo "== ingester results =="
  for RID in $(cat /tmp/m1-conc-runids.txt 2>/dev/null); do
    CR="run-$(echo "$RID" | tr 'A-Z' 'a-z')-a1"
    echo "--- $CR ---"
    bash ~/ClawBox/scripts/dump-result.sh "$CR" 2>&1 | grep -E 'status:|agent_exit_code|patch_status|patch len|final_answer len' | head -5
  done
  echo "== leaks =="
  ps -eo comm | grep -c firecracker
  echo "== final done =="
} > "$OUT" 2>&1
echo written
