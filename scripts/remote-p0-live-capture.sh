#!/usr/bin/env bash
# Preserve pod logs/status for a P0 run before the controller deletes children.
# Usage: bash remote-p0-live-capture.sh <SandboxTask name>
set -euo pipefail

CR="${1:?usage: remote-p0-live-capture.sh <SandboxTask name>}"
NS=clawbox-benchmarks
OUT="/tmp/p0-live-${CR}"
mkdir -p "$OUT"

for i in $(seq 1 900); do
  phase="$(kubectl -n "$NS" get sandboxtask "$CR" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  printf '%s phase=%s\n' "$(date -Is)" "$phase" >>"$OUT/phases.log"

  pods="$(kubectl -n "$NS" get pods \
    -l "clawbox.openai.com/task=${CR}" -o name 2>/dev/null || true)"
  for resource in $pods; do
    pod="${resource#pod/}"
    if kubectl -n "$NS" get pod "$pod" -o yaml >"$OUT/${pod}.yaml.tmp" 2>/dev/null; then
      mv "$OUT/${pod}.yaml.tmp" "$OUT/${pod}.yaml"
    fi
    if kubectl -n "$NS" describe pod "$pod" >"$OUT/${pod}.describe.tmp" 2>/dev/null; then
      mv "$OUT/${pod}.describe.tmp" "$OUT/${pod}.describe"
    fi
    if kubectl -n "$NS" logs "$pod" >"$OUT/${pod}.log.tmp" 2>/dev/null; then
      mv "$OUT/${pod}.log.tmp" "$OUT/${pod}.log"
    fi
    if [[ "$pod" == *-runtime-* ]] && \
       kubectl -n "$NS" exec "$pod" -- tar -C /state -czf - . \
         >"$OUT/${pod}.state.tar.gz.tmp" 2>/dev/null; then
      mv "$OUT/${pod}.state.tar.gz.tmp" "$OUT/${pod}.state.tar.gz"
    fi
  done

  case "$phase" in Cleaned|Failed|TimedOut) break;; esac
  sleep 2
done

sleep 3
kubectl -n "$NS" get events --sort-by=.lastTimestamp \
  | grep -F "$CR" >"$OUT/events.log" 2>&1 || true
echo "capture complete: $OUT"
