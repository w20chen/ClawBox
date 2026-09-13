#!/usr/bin/env bash
set -Eeuo pipefail

# Run the complete 15five comparison matrix once per snapshot mechanism.
# The worker writes per-arm events, native ClawTune traces, model-gateway
# records, and summary files below the selected output root.

PROJECT_ROOT=${CLAWBOX_PROJECT_ROOT:-/home/weitianc/ClawBox-incremental-20260913}
OUTPUT_ROOT=${CLAWBOX_STUDY_ROOT:-/home/weitianc/clawbox-tiered-study-20260913}
PYTHON=${CLAWBOX_PYTHON:-/home/weitianc/miniconda3/envs/ML/bin/python}
MACHINE_ENV=${CLAWBOX_MACHINE_ENV:-$HOME/.config/clawbox/machine.env}
TIMEOUT_SECONDS=${CLAWBOX_STUDY_TIMEOUT_SECONDS:-7000}

test -f "$MACHINE_ENV"
test -x "$PYTHON"
test -f "$PROJECT_ROOT/15five-full-copy.yaml"
test -f "$PROJECT_ROOT/15five-incremental-cow.yaml"

set -a
# shellcheck disable=SC1090
source "$MACHINE_ENV"
set +a
export PYTHONUNBUFFERED=1
cd "$PROJECT_ROOT"
export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export CLAWBOX_OUTPUT_ROOT="$OUTPUT_ROOT"
# Keep the workload concurrency at 16 while allowing the host to create pairs
# in parallel; this is independent of the agent concurrency in the spec.
export CLAWBOX_SANDBOX_CREATE_CONCURRENCY=${CLAWBOX_SANDBOX_CREATE_CONCURRENCY:-8}

mkdir -p "$OUTPUT_ROOT"
cat >"$OUTPUT_ROOT/manifest.json" <<EOF
{"schema_version":1,"experiment":"15five-scim2-filter-parser-13","concurrency":16,"runtime_memory_mib":2048,"tool_memory_mib":4096,"pool_memory_budget_mib":65536,"warm_memory_capacity_mib":65536,"local_numa_node":0,"warm_numa_node":1,"snapshot_mechanisms":["full-copy","incremental-cow"],"timeout_per_mechanism_seconds":$TIMEOUT_SECONDS}
EOF

run_one() {
  local mechanism=$1
  local spec="$PROJECT_ROOT/15five-${mechanism}.yaml"
  local run_id="15five-${mechanism}-c16"
  local log="$OUTPUT_ROOT/${run_id}.console.log"
  echo "[$(date -u +%FT%TZ)] starting ${mechanism}" | tee -a "$OUTPUT_ROOT/launcher.log"
  if ! timeout --preserve-status "$TIMEOUT_SECONDS" "$PYTHON" -m clawbox.experiments.worker \
      --spec "$spec" --run-id "$run_id" --attempt-id "attempt-${run_id}" \
      --task-uid "study-${run_id}" > >(tee "$log") 2>&1; then
    STUDY_FAILED=1
    echo "[$(date -u +%FT%TZ)] ${mechanism} failed; continuing with the other mechanism" | tee -a "$OUTPUT_ROOT/launcher.log"
  fi
  "$PYTHON" "$PROJECT_ROOT/scripts/report-standalone-study.py" \
    "$OUTPUT_ROOT/$run_id" > "$OUTPUT_ROOT/${run_id}.summary.md" || true
  echo "[$(date -u +%FT%TZ)] finished ${mechanism}" | tee -a "$OUTPUT_ROOT/launcher.log"
}

STUDY_FAILED=0
run_one full-copy
run_one incremental-cow
echo "[$(date -u +%FT%TZ)] study complete" | tee -a "$OUTPUT_ROOT/launcher.log"
exit "$STUDY_FAILED"
