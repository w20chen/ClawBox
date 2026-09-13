#!/usr/bin/env bash
# Manual, repeatable host setup and read-only diagnostics for tiered snapshots.
set -euo pipefail
if [[ $# != 2 || $2 != check && $2 != apply ]]; then
  echo "usage: $0 HOST_ENV {check|apply}" >&2
  exit 64
fi
config=$(realpath "$1")
[[ -f $config ]] || { echo "missing host config: $config" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$config"
set +a
for name in CLAWBOX_WARM_ROOT CLAWBOX_COLD_ROOT CLAWBOX_LOCAL_MIB CLAWBOX_WARM_MIB CLAWBOX_LOCAL_NODE CLAWBOX_WARM_NODE; do
  [[ -n ${!name:-} ]] || { echo "missing $name in $config" >&2; exit 1; }
done
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [[ $2 == check ]]; then
  exec python3 "$script_dir/diagnose-snapshot-host.py"
fi
exec sudo env \
  CLAWBOX_LOCAL_MIB="$CLAWBOX_LOCAL_MIB" CLAWBOX_WARM_MIB="$CLAWBOX_WARM_MIB" \
  CLAWBOX_LOCAL_NODE="$CLAWBOX_LOCAL_NODE" CLAWBOX_WARM_NODE="$CLAWBOX_WARM_NODE" \
  bash "$script_dir/setup-tiered-memory.sh" "$CLAWBOX_WARM_ROOT" "$CLAWBOX_COLD_ROOT" "$(id -un)"
