#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if [[ -n "${CLAWBOX_PYTHON:-}" ]]; then
  PYTHON="${CLAWBOX_PYTHON}"
elif [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PYTHON="${ROOT}/.venv/bin/python"
else
  PYTHON=python3
fi

"${PYTHON}" -c 'import kubernetes' >/dev/null 2>&1 || {
  echo "Python Kubernetes dependency is missing from ${PYTHON}." >&2
  echo "Install ClawBox in that interpreter: ${PYTHON} -m pip install -e ." >&2
  exit 1
}

exec "${PYTHON}" -m clawbox.benchmark.kubernetes "$@"
