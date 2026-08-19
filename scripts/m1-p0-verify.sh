#!/usr/bin/env bash
# P0 verification: rebuild-control-plane image, recreate M1 smoke stack, submit
# a REAL task (DeepSeek via the clawbox-llm secret), watch to terminal, then
# verify: (1) execution_id flows (execution_source=runtime-envelope + span id
# == bridge id), (2) patch_status=present, (3) real DeepSeek API calls.
# Usage: bash m1-p0-verify.sh [deadline_seconds]
set -u
DEADLINE="${1:-600}"
OUT=/tmp/m1-p0-verify.log
: > "$OUT"
{
  export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
  unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
  TOKEN="clawbox-m1-smoke-token-0001"
  IMG="127.0.0.1:5000/clawbox/control-plane-arm64:dev"
  DATA="$HOME/clawbox-m1-data"
  TASK_IMAGE="127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:bdf4637498a4b765f0e91333ff292c226bd011a31c220d76609daff43e2c39fd"
  TEMPLATES="{\"swe-rebench-arm64\":{\"1\":{\"toolImage\":\"$TASK_IMAGE\",\"secretName\":\"clawbox-llm\",\"runtimeImage\":\"127.0.0.1:5000/clawbox/runtime-arm64:dev\",\"llmEgressCIDR\":\"0.0.0.0/0\",\"profile\":\"small\",\"maxDeadlineSeconds\":3600,\"minDeadlineSeconds\":60}}}"

  echo "== control-plane components =="
  kubectl get deploy -n clawbox-system 2>/dev/null | grep -E 'cell|ingest|NAME' || echo "none"

  echo "== alembic upgrade =="
  docker run --rm -e "DATABASE_URL=sqlite:////data/clawbox-managed.db" \
    -v "$DATA:/data" "$IMG" python3 -m alembic upgrade head 2>&1 | tail -2

  echo "== kubeconfig =="
  KUBEDIR="${HOME}/m1-kubeconfig"
  rm -rf "${KUBEDIR}"
  mkdir -p "${KUBEDIR}"
  cp ~/.kube/config "${KUBEDIR}/config"
  chmod 644 "${KUBEDIR}/config"  # container runs non-root; needs world-read on the bind mount

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
    -v "${KUBEDIR}/config":/mnt/kube/config:ro \
    "$IMG" clawbox-managed-dispatcher
  sleep 4
  docker ps --format '{{.Names}} {{.Status}}' | grep clawbox-m1 || true

  echo "== submit real task (deadline=${DEADLINE}s) =="
  IDKEY="m1-p0-verify-$(date +%s)"
  PAYLOAD="$(HOME="$HOME" python3 - <<PY
import json, hashlib, os
path = os.path.join(os.environ['HOME'], 'ClawBox', 'scripts', 'problem-scim2-13.txt')
problem = open(path, encoding='utf-8').read()
body = {
    "projectId": "m1-p0-verify",
    "templateRef": "swe-rebench-arm64",
    "templateRevision": 1,
    "inputRef": "15five__scim2-filter-parser-13",
    "inputSha256": hashlib.sha256(problem.encode()).hexdigest(),
    "deadlineSeconds": int(os.environ.get('DEADLINE', '600')),
    "idempotencyKey": "$IDKEY",
    "problemStatement": problem,
}
print(json.dumps(body))
PY
)"
  RESP=$(curl -s -X POST http://127.0.0.1:8085/v1/runs \
    -H "X-Clawbox-Token: $TOKEN" -H "X-Tenant-Id: tenant-a" \
    -H "Content-Type: application/json" -d "$PAYLOAD")
  echo "submit: $RESP"
  RUN_ID=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("runId",""))')
  echo "RUN_ID=$RUN_ID"
  CR="run-$(echo "$RUN_ID" | tr 'A-Z' 'a-z')-a1"
  echo "CR=$CR"
  echo "RUN_ID=$RUN_ID" > /tmp/m1-p0-run.txt
  echo "CR=$CR" >> /tmp/m1-p0-run.txt

  echo "== watch (up to $((DEADLINE + 600))s) =="
  for i in $(seq 1 $(((DEADLINE + 600) / 10))); do
    PH=$(kubectl get sandboxtask -n clawbox-benchmarks "$CR" -o jsonpath='{.status.phase}' 2>/dev/null)
    RUNPH=$(curl -s http://127.0.0.1:8085/v1/runs/$RUN_ID -H "X-Clawbox-Token: $TOKEN" -H "X-Tenant-Id: tenant-a" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("phase",""))' 2>/dev/null)
    echo "  t=$((i*10))s CR=$PH run=$RUNPH"
    case "$PH" in Cleaned|Failed|TimedOut) break;; esac
    sleep 10
  done

  echo "== final CR =="
  kubectl get sandboxtask -n clawbox-benchmarks "$CR" -o jsonpath='{.status}' 2>/dev/null; echo
  echo "== result =="
  bash ~/ClawBox/scripts/dump-result.sh "$CR" 2>&1 | grep -E 'status:|patch_status|patch len|final_answer len|agent_exit_code' | head -8
} > "$OUT" 2>&1
echo "p0 done; see $OUT"
