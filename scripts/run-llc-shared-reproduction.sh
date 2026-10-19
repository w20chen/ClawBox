#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_DIR="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
MACHINE_ENV=${CLAWBOX_MACHINE_ENV:-${HOME}/.config/clawbox/machine.env}

if [[ -f "$MACHINE_ENV" ]]; then
  export CLAWBOX_MACHINE_ENV="$MACHINE_ENV"
  set -a
  # shellcheck disable=SC1090
  source "$MACHINE_ENV"
  set +a
fi

if [[ -n "${CLAWBOX_PYTHON:-}" ]]; then
  PYTHON="$CLAWBOX_PYTHON"
elif [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  PYTHON="${PROJECT_DIR}/.venv/bin/python"
else
  PYTHON=python3
fi

export PYTHONPATH="${PROJECT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
exec "$PYTHON" "${SCRIPT_DIR}/run_llc_shared_reproduction.py" "$@"
