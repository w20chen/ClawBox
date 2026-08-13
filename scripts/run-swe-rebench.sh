#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

python3 -c 'import kubernetes' >/dev/null 2>&1 || {
  echo "Python Kubernetes dependency is missing." >&2
  echo "Install ClawBox first: python3 -m pip install -e ." >&2
  exit 1
}

exec python3 -m clawbox.benchmark.kubernetes "$@"
