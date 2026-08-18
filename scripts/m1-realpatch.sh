#!/usr/bin/env bash
# Extract the agent's COMMITTED patch (c1ad059) from the tool VM.
OUT=/tmp/m1-realpatch.txt
: > "$OUT"
{
  echo "== git show c1ad059 (the agent's commit) =="
  kubectl exec -n clawbox-benchmarks run-01m0ady8dnnwnjwvw7g42390rd-a1-tool -- \
    sh -c 'cd /testbed && git show c1ad059 2>&1' > "$OUT" 2>&1
  echo "== extracted to $OUT, bytes: $(wc -c < "$OUT") =="
  echo "== head =="
  head -40 "$OUT"
} >> "$OUT" 2>&1
echo written
