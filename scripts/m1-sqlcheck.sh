#!/usr/bin/env bash
# Inspect the agent's actual change in the tool VM (sql.py content).
OUT=/tmp/m1-sqlcheck.txt
: > "$OUT"
{
  echo "== sql.py: grep AttrPath/namedtuple =="
  kubectl exec -n clawbox-benchmarks run-01m0ady8dnnwnjwvw7g42390rd-a1-tool -- \
    sh -c 'cd /testbed && grep -n "AttrPath\|namedtuple\|attr_name\|attr_path" src/scim2_filter_parser/transpilers/sql.py 2>&1 | head -30' 2>&1
  echo "== git log last commit =="
  kubectl exec -n clawbox-benchmarks run-01m0ady8dnnwnjwvw7g42390rd-a1-tool -- \
    sh -c 'cd /testbed && git log --oneline -3 2>&1' 2>&1
  echo "== agent final answer (last 30 lines of agent.log) =="
  kubectl exec -n clawbox-benchmarks run-01m0ady8dnnwnjwvw7g42390rd-a1-runtime-9n9bt -- \
    sh -c 'grep -n "final\|patch\|diff\|```" /state/run-01m0ady8dnnwnjwvw7g42390rd-a1/logs/agent.log 2>/dev/null | tail -15' 2>&1
  echo "== done =="
} > "$OUT" 2>&1
echo written
