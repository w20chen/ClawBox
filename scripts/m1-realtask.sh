#!/usr/bin/env bash
# Submit a REAL SWE-ReBench task through the M1 Managed API and watch the cell.
# Requires the freshly rebuilt control-plane image (problemStatement support).
OUT=/tmp/m1-realtask.txt
: > "$OUT"
{
  TOKEN="clawbox-m1-smoke-token-0001"
  IMG="127.0.0.1:5000/clawbox/control-plane-arm64:dev"
  DATA="$HOME/clawbox-m1-data"
  TASK_IMAGE="127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:bdf4637498a4b765f0e91333ff292c226bd011a31c220d76609daff43e2c39fd"
  TEMPLATES="{\"swe-rebench-arm64\":{\"1\":{\"toolImage\":\"$TASK_IMAGE\",\"secretName\":\"clawbox-llm\",\"runtimeImage\":\"127.0.0.1:5000/clawbox/runtime-arm64:dev\",\"llmEgressCIDR\":\"0.0.0.0/0\",\"profile\":\"small\",\"maxDeadlineSeconds\":3600,\"minDeadlineSeconds\":60}}}"

  echo "== alembic upgrade (idempotent) =="
  docker run --rm -e "DATABASE_URL=sqlite:////data/clawbox-managed.db" \
    -v "$DATA:/data" "$IMG" python3 -m alembic upgrade head 2>&1 | tail -2

  echo "== recreate API =="
  docker rm -f clawbox-m1-api clawbox-m1-dispatcher 2>/dev/null
  docker run -d --name clawbox-m1-api --restart unless-stopped \
    -e "DATABASE_URL=sqlite:////data/clawbox-managed.db" \
    -e "CLAWBOX_SERVICE_TOKEN=$TOKEN" \
    -e "CLAWBOX_TEMPLATES=$TEMPLATES" \
    -v "$DATA:/data" -p 8085:8085 \
    "$IMG" clawbox-managed-api
  sleep 3
  curl -s http://127.0.0.1:8085/healthz; echo

  echo "== recreate dispatcher =="
  docker run -d --name clawbox-m1-dispatcher --restart unless-stopped \
    -e "DATABASE_URL=sqlite:////data/clawbox-managed.db" \
    -e "CLAWBOX_CR_VERSION=v1alpha1" \
    -e "CLAWBOX_CELL_NAMESPACE=clawbox-benchmarks" \
    -e "CLAWBOX_TEMPLATES=$TEMPLATES" \
    -e "KUBECONFIG=/mnt/kube/config" \
    -v "$DATA:/data" \
    -v /tmp/m1-kubeconfig:/mnt/kube/config:ro \
    "$IMG" clawbox-managed-dispatcher
  sleep 4

  echo "== submit real task =="
  PAYLOAD="$(HOME="$HOME" python3 - <<'PY'
import json, hashlib, os
path = os.path.join(os.environ['HOME'], 'ClawBox', 'scripts', 'problem-scim2-13.txt')
problem = open(path, encoding='utf-8').read()
body = {
    "projectId": "m1-real-task",
    "templateRef": "swe-rebench-arm64",
    "templateRevision": 1,
    "inputRef": "15five__scim2-filter-parser-13",
    "inputSha256": hashlib.sha256(problem.encode()).hexdigest(),
    "deadlineSeconds": 1800,
    "idempotencyKey": "m1-real-task-001",
    "problemStatement": problem,
}
print(json.dumps(body))
PY
)"
  echo "payload bytes: ${#PAYLOAD}"
  curl -s -X POST http://127.0.0.1:8085/v1/runs \
    -H "X-Clawbox-Token: $TOKEN" -H "X-Tenant-Id: tenant-a" \
    -H "Content-Type: application/json" -d "$PAYLOAD"
  echo
  echo "== watch cell phase (150s) =="
  for i in $(seq 1 30); do
    PH=$(kubectl get sandboxtask -n clawbox-benchmarks -o jsonpath='{.items[?(@.metadata.name=~"run-.*")].status.phase}' 2>/dev/null | tr ' ' '\n' | tail -1)
    echo "  t=${i}x5s phase=$PH"
    case "$PH" in Cleaned|Failed|TimedOut) break;; esac
    sleep 5
  done
  echo "== CR list =="
  kubectl get sandboxtask -n clawbox-benchmarks 2>/dev/null | grep run- | tail -3
  echo "== realtask done =="
} > "$OUT" 2>&1
echo written
