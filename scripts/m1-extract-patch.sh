#!/usr/bin/env bash
# Extract the agent's real patch directly from the tool VM (backup path while
# the runtime's SSH patch collection is in its transient EOF loop).
OUT=/tmp/m1-patch.txt
: > "$OUT"
{
  echo "== git status/diff in tool VM =="
  kubectl exec -n clawbox-benchmarks run-01m0ady8dnnwnjwvw7g42390rd-a1-tool -- \
    sh -c 'cd /testbed && git status --short 2>&1; echo ---; git diff 2>&1 | head -120' 2>&1
  echo "== patch done =="
} > "$OUT" 2>&1
echo written
