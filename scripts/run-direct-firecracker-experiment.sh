#!/usr/bin/env bash
# Build a fresh two-VM image pair, then run one reproducible replay arm.
set -euo pipefail

usage() {
  echo "usage: $0 --mode resident|snapshot --output DIR --base-image IMG --workspace DIR --base-commit SHA --trace TRACE --calibration TRACE [--sessions N] [--memory-mib MIB] [replay options]" >&2
  exit 2
}

mode='' output='' base_image='' workspace='' base_commit='' trace='' calibration=''
sessions=1 memory_mib=512 extra_space_mib=512
extra=()
while (($#)); do
  case "$1" in
    --mode) mode=${2:-}; shift 2;;
    --output) output=${2:-}; shift 2;;
    --base-image) base_image=${2:-}; shift 2;;
    --workspace) workspace=${2:-}; shift 2;;
    --base-commit) base_commit=${2:-}; shift 2;;
    --trace) trace=${2:-}; shift 2;;
    --calibration) calibration=${2:-}; shift 2;;
    --sessions) sessions=${2:-}; shift 2;;
    --memory-mib) memory_mib=${2:-}; shift 2;;
    --extra-space-mib) extra_space_mib=${2:-}; shift 2;;
    --help|-h) usage;;
    *) extra+=("$1"); shift;;
  esac
done
[[ $mode == resident || $mode == snapshot ]] || usage
[[ -n $output && -n $base_image && -n $workspace && -n $base_commit && -n $trace && -n $calibration ]] || usage
[[ ! -e $output ]] || { echo "refusing to reuse existing output: $output" >&2; exit 2; }

mkdir -p "$output"
rootfs="$output/tool-runtime.ext4"
python3 scripts/build-runtime-agent-rootfs.py \
  --base-image "$base_image" --agent-source clawbox/replay/guest_agent.c \
  --workspace-source "$workspace" --extra-space-mib "$extra_space_mib" --output-rootfs "$rootfs"
python3 scripts/prepare-high-density-experiment.py \
  --output "$output/input" --sessions "$sessions" --workspace-source "$workspace" \
  --base-commit "$base_commit" --rootfs-source "$rootfs" --tool-rootfs-source "$rootfs" \
  --trace "$trace" --calibration "$calibration" --memory-mib "$memory_mib" \
  --guest-agent --guest-touch-mib 128 --tool-guest-touch-mib 256
python3 -m clawbox.replay.cli experiment "$output/input/manifest.json" \
  --mode "$mode" --resident-slots "$sessions" --tool-resident-slots "$sessions" \
  --numa-node 0 --output-dir "$output/results" "${extra[@]}"
