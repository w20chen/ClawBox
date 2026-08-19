#!/usr/bin/env bash
# Remove the single, audited stale devmapper mapping that blocks P0.
# Preconditions are checked twice, with the node cordoned for the second check.
set -euo pipefail
shopt -s nullglob

TARGET="clawbox-fc--pool-snap-811"
DEVICE="/dev/mapper/${TARGET}"
NODE="$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')"
WAS_UNSCHEDULABLE="$(kubectl get node "$NODE" -o jsonpath='{.spec.unschedulable}')"
CORDONED_BY_US=false

export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY

restore_scheduling() {
  if [[ "$CORDONED_BY_US" == true && "$WAS_UNSCHEDULABLE" != true ]]; then
    kubectl uncordon "$NODE" >/dev/null || true
  fi
}
trap restore_scheduling EXIT

assert_no_active_cells() {
  local tasks pods
  tasks="$(kubectl get sandboxtasks -A --no-headers \
    -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,PHASE:.status.phase' \
    | awk '$3 != "Cleaned" {print}')"
  pods="$(kubectl -n clawbox-benchmarks get pods \
    --field-selector=status.phase!=Succeeded,status.phase!=Failed \
    --no-headers -o custom-columns='NAME:.metadata.name,PHASE:.status.phase' 2>/dev/null || true)"
  if [[ -n "$tasks" || -n "$pods" ]]; then
    printf 'refusing removal: active tasks or benchmark pods exist\n%s\n%s\n' "$tasks" "$pods" >&2
    exit 1
  fi
}

assert_device_unowned() {
  local open_count major_minor holders
  open_count="$(sudo -n dmsetup info -c --noheadings -o open "$TARGET" | tr -d '[:space:]')"
  [[ "$open_count" == 0 ]] || { echo "refusing removal: open count is $open_count" >&2; exit 1; }
  if findmnt -rn -S "$DEVICE" | grep -q .; then
    echo "refusing removal: $DEVICE is mounted" >&2
    findmnt -rn -S "$DEVICE" >&2
    exit 1
  fi
  major_minor="$(sudo -n dmsetup info -c --noheadings --separator : -o major,minor "$TARGET" | tr -d '[:space:]')"
  holders=(/sys/dev/block/"${major_minor}"/holders/*)
  if (( ${#holders[@]} )); then
    printf 'refusing removal: device has holders: %s\n' "${holders[*]}" >&2
    exit 1
  fi
  sudo -n dmsetup info -c "$TARGET"
  sudo -n dmsetup status "$TARGET"
  sudo -n dmsetup deps "$TARGET"
}

echo "preflight: node=$NODE target=$TARGET"
assert_no_active_cells
assert_device_unowned

if [[ "$WAS_UNSCHEDULABLE" != true ]]; then
  kubectl cordon "$NODE" >/dev/null
  CORDONED_BY_US=true
fi
echo "node cordoned; repeating safety checks"
assert_no_active_cells
assert_device_unowned

echo "removing exact stale mapping $TARGET"
timeout 30 sudo -n dmsetup remove "$TARGET"
if sudo -n dmsetup info "$TARGET" >/dev/null 2>&1; then
  echo "removal did not take effect" >&2
  exit 1
fi

echo "REMOVED $TARGET"
restore_scheduling
CORDONED_BY_US=false
kubectl get node "$NODE" -o custom-columns='NAME:.metadata.name,READY:.status.conditions[?(@.type=="Ready")].status,UNSCHEDULABLE:.spec.unschedulable'
