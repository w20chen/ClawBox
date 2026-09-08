#!/usr/bin/env bash
set -eu
namespace=cube-system
pod=cubelet-official-inspect
if ! kubectl -n "$namespace" get pod "$pod" >/dev/null 2>&1; then
  kubectl -n "$namespace" run "$pod" \
    --image=cube-sandbox-int.tencentcloudcr.com/cube-sandbox/cubelet:v0.7.0 \
    --restart=Never --command -- sleep 600
fi
kubectl -n "$namespace" wait "pod/$pod" --for=condition=Ready --timeout=120s
echo official
kubectl -n "$namespace" exec "$pod" -- sh -lc \
  "find /opt/cube-image -type f | grep -E 'cube-vs|Cubelet/bin/cubelet' | xargs sha256sum"
echo custom
kubectl -n "$namespace" exec cube-node-t6pd8 -c cubelet -- sh -lc \
  "find /opt/cube-image -type f | grep -E 'cube-vs|Cubelet/bin/cubelet' | xargs sha256sum"
