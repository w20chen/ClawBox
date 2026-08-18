#!/usr/bin/env bash
# Show finalize state of each concurrent runtime.
OUT=/tmp/m1-conc-finalize.txt
: > "$OUT"
{
  for RID in $(cat /tmp/m1-conc-runids.txt 2>/dev/null); do
    CR="run-$(echo "$RID" | tr 'A-Z' 'a-z')-a1"
    RPOD=$(kubectl get pods -n clawbox-benchmarks 2>/dev/null | grep "$CR-runtime" | awk '{print $1}' | head -1)
    echo "=== $CR (pod=$RPOD) ==="
    [ -n "$RPOD" ] && kubectl logs -n clawbox-benchmarks "$RPOD" --tail=5 2>&1 | tail -5
  done
  echo "== finalize done =="
} > "$OUT" 2>&1
echo written
