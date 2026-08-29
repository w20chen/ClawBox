#!/usr/bin/env bash
# Short, self-contained entry point for the validated direct Tool-vsock smoke.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
usage() {
  cat >&2 <<'EOF'
usage: run-direct-firecracker-smoke.sh --mode resident|snapshot --output DIR [--sessions N] [--memory-mib MIB] [replay options]

Runs the validated direct Runtime+Tool Firecracker vsock smoke using the
current checkout as a disposable Tool workspace. Output must be outside it.
Any remaining options are passed to the replay experiment command.
EOF
  exit 64
}

mode='' output='' sessions=1 memory_mib=512
extra=()
while (($#)); do
  case "$1" in
    --mode) mode=${2:-}; shift 2 ;;
    --output) output=${2:-}; shift 2 ;;
    --sessions) sessions=${2:-}; shift 2 ;;
    --memory-mib) memory_mib=${2:-}; shift 2 ;;
    --help|-h) usage ;;
    *) extra+=("$1"); shift ;;
  esac
done
[[ $mode == resident || $mode == snapshot ]] || usage
[[ -n $output ]] || usage
[[ $sessions =~ ^[1-9][0-9]*$ && $memory_mib =~ ^[1-9][0-9]*$ ]] || usage

trace="${output}.smoke.jsonl"
[[ ! -e $trace ]] || { echo "refusing to overwrite trace: $trace" >&2; exit 2; }
python3 "$ROOT/scripts/make-direct-replay-smoke.py" --output "$trace"
base_commit="$(git -C "$ROOT" rev-parse HEAD)"
bash "$ROOT/scripts/run-direct-firecracker-experiment.sh" \
  --mode "$mode" --output "$output" \
  --base-image /opt/kata/share/kata-containers/kata-containers.img \
  --workspace "$ROOT" --base-commit "$base_commit" \
  --trace "$trace" --calibration "$trace" \
  --sessions "$sessions" --memory-mib "$memory_mib" "${extra[@]}"
