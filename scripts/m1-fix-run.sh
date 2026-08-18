#!/usr/bin/env bash
# Fix dispatcher kubeconfig + create a Run through the live Managed API.
OUT=/tmp/m1-fix.txt
: > "$OUT"
{
  echo "== prep kubeconfig =="
  cp "$HOME/.kube/config" /tmp/m1-kubeconfig 2>&1
  chmod 644 /tmp/m1-kubeconfig 2>&1
  ls -l /tmp/m1-kubeconfig

  echo "== recreate dispatcher with readable kubeconfig =="
  docker rm -f clawbox-m1-dispatcher 2>/dev/null
  IMG="127.0.0.1:5000/clawbox/control-plane-arm64:dev"
  DATA="$HOME/clawbox-m1-data"
  TASK_IMAGE="127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:bdf4637498a4b765f0e91333ff292c226bd011a31c220d76609daff43e2c39fd"
  TEMPLATES="{\"swe-rebench-arm64\":{\"1\":{\"toolImage\":\"$TASK_IMAGE\",\"secretName\":\"clawbox-llm\",\"runtimeImage\":\"127.0.0.1:5000/clawbox/runtime-arm64:dev\",\"llmEgressCIDR\":\"0.0.0.0/0\",\"profile\":\"small\",\"maxDeadlineSeconds\":3600,\"minDeadlineSeconds\":60}}}"
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
  echo "== dispatcher logs after fix =="
  docker logs clawbox-m1-dispatcher 2>&1 | tail -6

  echo "== create run via API =="
  TOKEN="$(cat "$DATA/token.env")"
  curl -s -X POST http://127.0.0.1:8085/v1/runs \
    -H "X-Clawbox-Token: $TOKEN" -H "X-Tenant-Id: tenant-a" \
    -H "Content-Type: application/json" \
    -d '{"projectId":"m1-smoke","templateRef":"swe-rebench-arm64","templateRevision":1,"inputRef":"m1-smoke-001","inputSha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","deadlineSeconds":1800,"idempotencyKey":"m1-smoke-key-001"}'
  echo
  echo "== CRs after submit =="
  sleep 6
  kubectl get sandboxtask -n clawbox-benchmarks 2>/dev/null | grep 'run-' || echo "(no run- CR yet)"
  echo "== fix done =="
} > "$OUT" 2>&1
echo written
