#!/bin/bash
# Grep the runtime agent.log for tool behavior.
# Usage: bash check-agent-log.sh <prefix-or-cellname>
PREFIX="${1:?usage: check-agent-log.sh <prefix>}"
NS=clawbox-benchmarks
RP=$(kubectl get pods -n "$NS" -o name 2>/dev/null | grep "${PREFIX}.*runtime" | head -1 | cut -d/ -f2)
[ -z "$RP" ] && { echo "no runtime pod"; exit 1; }
# derive task id: pod name = <taskid>-runtime-<rand>
TASK=${RP%-runtime-*}
echo "runtime pod: $RP  task: $TASK"
STATE="/state/${TASK}"
echo "=== Sandbox path escapes count ==="
kubectl exec -n "$NS" "$RP" -- sh -c "grep -c 'Sandbox path escapes' ${STATE}/logs/agent.log 2>/dev/null || true" 2>&1 | head -3
echo "=== read/write/edit activity (tail 25) ==="
kubectl exec -n "$NS" "$RP" -- sh -c "grep -E 'read |write |edit|apply_patch|exec |patch|failed' ${STATE}/logs/agent.log 2>/dev/null | tail -25" 2>&1 | head -30
echo "=== agent.log tail (15) ==="
kubectl exec -n "$NS" "$RP" -- sh -c "tail -15 ${STATE}/logs/agent.log 2>/dev/null" 2>&1 | head -20
echo "=== done ==="
