#!/usr/bin/env bash
# M1 live smoke on the target host: run the Managed API + Dispatcher as docker
# containers (SQLite for now; Postgres blocked by Docker Hub proxy), write
# v1alpha1 SandboxTask CRs (cluster is still on the v1alpha1 CRD), and submit a
# Run through the API to prove API -> outbox -> Dispatcher -> CR -> live cell.
set -u
IMG="127.0.0.1:5000/clawbox/control-plane-arm64:dev"
DATA="$HOME/clawbox-m1-data"
mkdir -p "$DATA"
chmod 777 "$DATA"

TOKEN_FILE="$DATA/token.env"
if [ ! -s "$TOKEN_FILE" ]; then
  head -c 32 /dev/urandom | base64 | tr -d '=+/' | head -c 32 > "$TOKEN_FILE"
fi
TOKEN="$(cat "$TOKEN_FILE")"

TASK_IMAGE="127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:bdf4637498a4b765f0e91333ff292c226bd011a31c220d76609daff43e2c39fd"
TEMPLATES="{\"swe-rebench-arm64\":{\"1\":{\"toolImage\":\"$TASK_IMAGE\",\"secretName\":\"clawbox-llm\",\"runtimeImage\":\"127.0.0.1:5000/clawbox/runtime-arm64:dev\",\"llmEgressCIDR\":\"0.0.0.0/0\",\"profile\":\"small\",\"maxDeadlineSeconds\":3600,\"minDeadlineSeconds\":60}}}"

echo "== 1. alembic upgrade (sqlite) =="
docker run --rm -e "DATABASE_URL=sqlite:////data/clawbox-managed.db" \
  -v "$DATA:/data" "$IMG" python3 -m alembic upgrade head 2>&1 | tail -3

echo "== 2. start Managed API =="
docker rm -f clawbox-m1-api 2>/dev/null || true
docker run -d --name clawbox-m1-api --restart unless-stopped \
  -e "DATABASE_URL=sqlite:////data/clawbox-managed.db" \
  -e "CLAWBOX_SERVICE_TOKEN=$TOKEN" \
  -e "CLAWBOX_TEMPLATES=$TEMPLATES" \
  -v "$DATA:/data" -p 8085:8085 \
  "$IMG" clawbox-managed-api
sleep 3
curl -s http://127.0.0.1:8085/healthz; echo

echo "== 3. start Dispatcher (v1alpha1 CRs) =="
docker rm -f clawbox-m1-dispatcher 2>/dev/null || true
docker run -d --name clawbox-m1-dispatcher --restart unless-stopped \
  -e "DATABASE_URL=sqlite:////data/clawbox-managed.db" \
  -e "CLAWBOX_CR_VERSION=v1alpha1" \
  -e "CLAWBOX_CELL_NAMESPACE=clawbox-benchmarks" \
  -e "CLAWBOX_TEMPLATES=$TEMPLATES" \
  -e "KUBECONFIG=/mnt/kube/config" \
  -v "$DATA:/data" \
  -v "$HOME/.kube:/mnt/kube:ro" \
  "$IMG" clawbox-managed-dispatcher
sleep 3
echo "dispatcher logs:"
docker logs clawbox-m1-dispatcher 2>&1 | tail -5

echo "== 4. create a Run via the API =="
RUN_ID=$(curl -s -X POST http://127.0.0.1:8085/v1/runs \
  -H "X-Clawbox-Token: $TOKEN" -H "X-Tenant-Id: tenant-a" \
  -H "Content-Type: application/json" \
  -d '{"projectId":"m1-smoke","templateRef":"swe-rebench-arm64","templateRevision":1,"inputRef":"m1-smoke-001","inputSha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","deadlineSeconds":1800,"idempotencyKey":"m1-smoke-key-001"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["runId"])')
echo "runId=$RUN_ID"
echo "$RUN_ID" > "$DATA/runid.txt"

echo "== 5. watch CR appear =="
for i in $(seq 1 20); do
  CR=$(kubectl get sandboxtask -n clawbox-benchmarks 2>/dev/null | grep "run-" || true)
  if [ -n "$CR" ]; then
    echo "CR found:"
    echo "$CR"
    break
  fi
  sleep 2
done
echo "== done =="
