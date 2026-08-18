#!/usr/bin/env bash
# Show per-agent activity: turn count + last real tool action for each concurrent cell.
OUT=/tmp/m1-conc-agents.txt
: > "$OUT"
{
  for RID in $(cat /tmp/m1-conc-runids.txt 2>/dev/null); do
    CR="run-$(echo "$RID" | tr 'A-Z' 'a-z')-a1"
    RPOD=$(kubectl get pods -n clawbox-benchmarks 2>/dev/null | grep "$CR-runtime" | awk '{print $1}' | head -1)
    echo "=== $CR (pod=$RPOD) ==="
    if [ -n "$RPOD" ]; then
      kubectl exec -n clawbox-benchmarks "$RPOD" -- sh -c \
        "grep -c turn /state/$CR/logs/agent.log 2>/dev/null || echo 0; echo '-- last actions:'; grep -E 'tool (read|edit|exec)|pytest|apply_patch' /state/$CR/logs/agent.log 2>/dev/null | tail -3" 2>&1
    fi
  done
  echo "== agents done =="
} > "$OUT" 2>&1
echo written
