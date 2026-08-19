#!/usr/bin/env bash
# Recreate the M1 smoke stack (API + dispatcher) from the freshly built
# control-plane image, with a valid kubeconfig for the dispatcher.
# Run on the target. Logs to /tmp/m1-recreate.log.
set -u
LOG=/tmp/m1-recreate.log
: > "$LOG"
{
  export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
  unset http_proxy https_proxy all_proxy

  echo "== kubeconfig =="
  KUBEDIR="${HOME}/m1-kubeconfig"
  rm -rf "${KUBEDIR}"
  mkdir -p "${KUBEDIR}"
  cp ~/.kube/config "${KUBEDIR}/config"
  chmod 600 "${KUBEDIR}/config"
  ls -la "${KUBEDIR}"

  TOKEN="clawbox-m1-smoke-token-0001"
  TEMPLATES='{"swe-rebench-arm64":{"1":{"toolImage":"127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:bdf4637498a4b765f0e91333ff292c226bd011a31c220d76609daff43e2c39fd","secretName":"clawbox-llm","runtimeImage":"127.0.0.1:5000/clawbox/runtime-arm64:dev","llmEgressCIDR":"0.0.0.0/0","profile":"small","maxDeadlineSeconds":3600,"minDeadlineSeconds":60}}}'

  echo "== recreate API =="
  docker rm -f clawbox-m1-api 2>/dev/null || true
  docker run -d --name clawbox-m1-api --restart unless-stopped \
    -p 127.0.0.1:8085:8085 \
    -e CLAWBOX_SERVICE_TOKEN="$TOKEN" \
    -e CLAWBOX_TEMPLATES="$TEMPLATES" \
    -e DATABASE_URL=sqlite:////data/clawbox-managed.db \
    -v /home/weitianc/clawbox-m1-data:/data \
    127.0.0.1:5000/clawbox/control-plane-arm64:dev \
    clawbox-managed-api

  echo "== recreate dispatcher =="
  docker rm -f clawbox-m1-dispatcher 2>/dev/null || true
  docker run -d --name clawbox-m1-dispatcher --restart unless-stopped \
    -e DATABASE_URL=sqlite:////data/clawbox-managed.db \
    -e CLAWBOX_CR_VERSION=v1alpha1 \
    -e CLAWBOX_CELL_NAMESPACE=clawbox-benchmarks \
    -e CLAWBOX_TEMPLATES="$TEMPLATES" \
    -e KUBECONFIG=/mnt/kube/config \
    -v /home/weitianc/clawbox-m1-data:/data \
    -v "${KUBEDIR}/config":/mnt/kube/config:ro \
    127.0.0.1:5000/clawbox/control-plane-arm64:dev \
    clawbox-managed-dispatcher

  echo "== status =="
  sleep 5
  docker ps --format '{{.Names}} {{.Status}}' | grep clawbox-m1
  echo "API_LOG:"; docker logs clawbox-m1-api 2>&1 | tail -5
  echo "DISPATCHER_LOG:"; docker logs clawbox-m1-dispatcher 2>&1 | tail -5
  echo "== health =="
  curl -fsS "http://127.0.0.1:8085/healthz" 2>&1 || echo "api not healthy yet"
} >> "$LOG" 2>&1
echo "done; see $LOG"
