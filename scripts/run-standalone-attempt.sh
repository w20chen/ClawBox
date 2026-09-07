#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 4 || $# -gt 5 ]]; then
  echo "usage: $0 SPEC RUN_ID ATTEMPT_ID TASK_UID [CONSOLE_LOG]" >&2
  exit 64
fi

spec=$1
run_id=$2
attempt_id=$3
task_uid=$4
console_log=${5:-}
machine_env=${CLAWBOX_MACHINE_ENV:-$HOME/.config/clawbox/machine.env}

test -f "$machine_env"
set -a
# shellcheck disable=SC1090
source "$machine_env"
set +a
export PYTHONUNBUFFERED=1

command=(
  .venv/bin/python -m clawbox.experiments.worker
  --spec "$spec"
  --run-id "$run_id"
  --attempt-id "$attempt_id"
  --task-uid "$task_uid"
)

if [[ -n "$console_log" ]]; then
  mkdir -p "$(dirname "$console_log")"
  "${command[@]}" 2>&1 | tee "$console_log"
else
  "${command[@]}"
fi
