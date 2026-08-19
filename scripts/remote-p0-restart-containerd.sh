#!/usr/bin/env bash
# Controlled single-node containerd restart for P0 devmapper state recovery.
set -euo pipefail

export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY

NODE="$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')"
WAS_UNSCHEDULABLE="$(kubectl get node "$NODE" -o jsonpath='{.spec.unschedulable}')"
CORDONED_BY_US=false

restore_scheduling() {
  if [[ "$CORDONED_BY_US" == true && "$WAS_UNSCHEDULABLE" != true ]]; then
    kubectl uncordon "$NODE" >/dev/null 2>&1 || true
  fi
}
trap restore_scheduling EXIT

active="$(kubectl get sandboxtasks -A --no-headers \
  -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,PHASE:.status.phase' \
  | awk '$3 != "Cleaned" {print}')"
if [[ -n "$active" ]]; then
  printf 'refusing restart: non-Cleaned SandboxTasks exist\n%s\n' "$active" >&2
  exit 1
fi

if [[ "$WAS_UNSCHEDULABLE" != true ]]; then
  kubectl cordon "$NODE" >/dev/null
  CORDONED_BY_US=true
fi

echo "restarting containerd on cordoned node $NODE"
timeout 90 sudo -n systemctl restart containerd

for _ in $(seq 1 60); do
  [[ "$(systemctl is-active containerd 2>/dev/null || true)" == active ]] && break
  sleep 2
done
[[ "$(systemctl is-active containerd 2>/dev/null || true)" == active ]]

echo "waiting for Kubernetes API and Ready node"
ready=false
for _ in $(seq 1 90); do
  state="$(kubectl get node "$NODE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
  if [[ "$state" == True ]]; then
    ready=true
    break
  fi
  sleep 2
done
[[ "$ready" == true ]] || { echo 'node did not return Ready' >&2; exit 1; }

echo "checking devmapper snapshotter plugin"
sudo -n /usr/bin/ctr plugins ls | awk '$1 ~ /snapshotter/ && $2 == "devmapper" {print; ok=($NF == "ok")} END {exit !ok}'

if sudo -n dmsetup info clawbox-fc--pool-snap-811 >/dev/null 2>&1; then
  echo 'snap-811 mapping unexpectedly remains after restart' >&2
  sudo -n dmsetup info -c clawbox-fc--pool-snap-811 >&2
  exit 1
fi

restore_scheduling
CORDONED_BY_US=false
kubectl get node "$NODE" -o custom-columns='NAME:.metadata.name,READY:.status.conditions[?(@.type=="Ready")].status,UNSCHEDULABLE:.spec.unschedulable'
kubectl get pods -A --field-selector=status.phase!=Succeeded,status.phase!=Failed -o wide
echo 'CONTAINERD RESTART RECOVERY COMPLETE'
