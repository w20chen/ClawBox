#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

# The supported target uses the local single-node Kubernetes API.  Login-shell
# proxy settings (especially stale socks5h localhost tunnels) must not be
# inherited by the Python Kubernetes client.  This only affects this launcher
# process; Runtime Pods receive their own explicit, fail-closed egress policy.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy

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
