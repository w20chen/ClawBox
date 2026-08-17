#!/bin/bash
# Check cells/pods/logs for a prefix.
# Usage: bash check-prefix.sh <prefix> [grep-pattern]
PREFIX="${1:?usage: check-prefix.sh <prefix> [grep]}"
PAT="${2:-}"
echo "=== cells ==="
kubectl get sandboxtask -A 2>/dev/null | grep "${PREFIX}"
echo "=== pods ==="
kubectl get pods -A 2>/dev/null | grep "${PREFIX}"
echo "=== runtime pod logs (tail 40) ==="
RP=$(kubectl get pods -A -o name 2>/dev/null | grep "${PREFIX}.*runtime" | head -1 | cut -d/ -f2)
NS=$(kubectl get pods -A -o 'jsonpath={range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep "${PREFIX}.*runtime" | head -1 | awk '{print $1}')
echo "runtime pod: ${NS}/${RP}"
if [ -n "${RP}" ] && [ -n "${NS}" ]; then
  if [ -n "${PAT}" ]; then
    kubectl logs "${RP}" -n "${NS}" --tail=200 2>&1 | grep -E "${PAT}" | tail -30
  else
    kubectl logs "${RP}" -n "${NS}" --tail=40 2>&1 | tail -40
  fi
else
  echo "no runtime pod found"
fi
echo "=== done ==="
