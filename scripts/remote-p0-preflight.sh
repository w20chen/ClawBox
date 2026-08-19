#!/usr/bin/env bash
# Read-only P0 target-host audit. This script never removes snapshots, restarts
# services, or changes Kubernetes resources.
set -u

export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY

section() { printf '\n== %s ==\n' "$1"; }
try() {
  printf '+ '
  printf '%q ' "$@"
  printf '\n'
  "$@" 2>&1 || printf '[exit=%s]\n' "$?"
}

section identity
try date -Is
try hostname
try uptime

section disk-cleanup
try df -h /
try pgrep -af 'disk-clean|docker image prune|docker builder prune'
if [[ -f /tmp/disk-clean.log ]]; then
  try tail -40 /tmp/disk-clean.log
fi

section kubernetes-health
try kubectl get nodes -o wide
try kubectl get nodes -o custom-columns='NAME:.metadata.name,READY:.status.conditions[?(@.type=="Ready")].status,TAINTS:.spec.taints'
try kubectl get pods -A --field-selector=status.phase!=Succeeded,status.phase!=Failed -o wide
try kubectl get sandboxtasks -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,PHASE:.status.phase,AGE:.metadata.creationTimestamp'

section affected-task
for task in \
  run-01m0cfwwh7g5daz21x9n0zrs6h-a1 \
  run-01m0ceg91xax10362gakjchy2a-a1 \
  run-01m0ccssb7fqpvng1f7cpb3bhd-a1; do
  try kubectl -n clawbox-benchmarks get sandboxtask "$task" \
    -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,OUTCOME:.status.outcome,AGE:.metadata.creationTimestamp'
done
try kubectl -n clawbox-benchmarks get pods \
  --field-selector=status.phase!=Succeeded,status.phase!=Failed -o wide

section services
try systemctl is-active containerd
try systemctl is-active kubelet
try systemctl --no-pager --full status containerd

section unprivileged-runtime-state
try ctr -n k8s.io snapshots --snapshotter devmapper info clawbox-fc--pool-snap-811
try ctr -n k8s.io containers info 05956a76352d8f49643de7ef00db058a90b96e812983f07b0cff3e267a4ea743
try crictl inspectp 05956a76352d8f49643de7ef00db058a90b96e812983f07b0cff3e267a4ea743
try dmsetup info -c clawbox-fc--pool-snap-811

section passwordless-sudo-probes
try sudo -n ctr -n k8s.io snapshots --snapshotter devmapper info clawbox-fc--pool-snap-811
try sudo -n dmsetup info -c clawbox-fc--pool-snap-811
try sudo -n dmsetup deps clawbox-fc--pool-snap-811
try sudo -n lvs -a --select 'lv_name=clawbox-fc-pool-snap-811' \
  -o vg_name,lv_name,lv_attr,data_percent,metadata_percent,devices

section targeted-journal
journalctl --no-pager -u containerd -u kubelet --since '-2 hours' \
  -g 'snap-811|Deactivating|already exists|devmapper' 2>&1 | tail -100

section marker-search
for root in /var/lib/containerd /var/lib/clawbox; do
  if [[ -r "$root" ]]; then
    try grep -R -n -m 20 'snap-811' "$root"
  fi
done

section done
echo 'READ-ONLY PREFLIGHT COMPLETE'
