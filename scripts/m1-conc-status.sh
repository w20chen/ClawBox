#!/usr/bin/env bash
# Print phase of each concurrent CR from /tmp/m1-conc-runids.txt.
OUT=/tmp/m1-conc-status.txt
: > "$OUT"
{
  echo "== runids: $(cat /tmp/m1-conc-runids.txt 2>/dev/null) =="
  for RID in $(cat /tmp/m1-conc-runids.txt 2>/dev/null); do
    CR="run-$(echo "$RID" | tr 'A-Z' 'a-z')-a1"
    printf '%-42s ' "$CR"
    kubectl get sandboxtask -n clawbox-benchmarks "$CR" -o jsonpath='{.status.phase}' 2>/dev/null
    echo
  done
  echo "== all run- CRs =="
  kubectl get sandboxtask -n clawbox-benchmarks 2>/dev/null | grep run-
  echo "== pods =="
  kubectl get pods -n clawbox-benchmarks 2>/dev/null | grep -E 'run-01m0af6' | head -10
  echo "== firecracker count =="
  ps -eo comm | grep -c firecracker
  echo "== status done =="
} > "$OUT" 2>&1
echo written
