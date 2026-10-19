#!/usr/bin/env bash
# Install/update the client environment without replacing host services or data.
set -Eeuo pipefail
project=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
machine=${CLAWBOX_MACHINE_ENV:-$HOME/.config/clawbox/machine.env}
if [[ -f "$machine" ]]; then
  set -a
  source "$machine"
  set +a
elif [[ -n "${CLAWBOX_MACHINE_ENV:-}" ]]; then
  echo "Machine configuration not found: $machine" >&2; exit 2
fi
python=${CLAWBOX_BOOTSTRAP_PYTHON:-python3}
venv="$project/.venv"
cube_source=${CUBE_SOURCE_DIR:-}
clawtune=${CLAWTUNE_ROOT:-}
while (( $# )); do
  case "$1" in
    --direct-network)
      unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
      shift ;;
    --python|--venv|--cube-source|--clawtune)
      (( $# >= 2 )) || { echo "$1 requires a value" >&2; exit 2; }
      case "$1" in
        --python) python=$2 ;; --venv) venv=$2 ;;
        --cube-source) cube_source=$2 ;; --clawtune) clawtune=$2 ;;
      esac
      shift 2 ;;
    --help)
      echo 'Usage: bash scripts/bootstrap-python.sh [--python python3.12] [--venv PATH] [--cube-source PATH] [--clawtune PATH]'
      echo 'Existing environments are reused. A patched SDK and matching ClawTune can be selected explicitly.'
      echo 'ClawTune source is required: set CLAWTUNE_ROOT in machine.env or use --clawtune with the matching exported source.'
      echo 'Use --direct-network to explicitly bypass inherited proxies during installation.'
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -z "$cube_source" || -f "$cube_source/sdk/python/pyproject.toml" ]] || {
  echo 'Invalid --cube-source: expected sdk/python/pyproject.toml' >&2; exit 1;
}
[[ -n "$clawtune" ]] || { echo 'Supply --clawtune with the source bundled with your guest images (or set CLAWTUNE_ROOT in machine.env).' >&2; exit 2; }
[[ -z "$clawtune" || -f "$clawtune/services/sidecar/pyproject.toml" ]] || {
  echo 'Invalid --clawtune: expected services/sidecar/pyproject.toml' >&2; exit 1;
}
if [[ ! -x "$venv/bin/python" ]]; then
  "$python" -c 'import sys; assert sys.version_info >= (3,12), "Python >= 3.12 is required"'
  "$python" -m venv "$venv"
fi
"$venv/bin/python" -c 'import sys; assert sys.version_info >= (3,12), "Existing venv requires Python >= 3.12"'
requirements=$(mktemp)
trap 'rm -f "$requirements"' EXIT
# Resolve selected local sources directly. Installing the Git dependency first
# would require network access and could silently select a different revision.
"$venv/bin/python" - "$project" "$clawtune" "$cube_source" > "$requirements" <<'PY'
from pathlib import Path
import sys, tomllib
project, clawtune, cube = sys.argv[1:]
metadata = tomllib.loads((Path(project) / 'pyproject.toml').read_text())['project']
for requirement in metadata['dependencies'] + metadata['optional-dependencies']['dev']:
    if clawtune and requirement.startswith('clawtune-sidecar '):
        requirement = 'clawtune-sidecar @ ' + (Path(clawtune).resolve() / 'services/sidecar').as_uri()
    if cube and requirement.startswith('cubesandbox=='):
        requirement = '-e ' + str(Path(cube).resolve() / 'sdk/python')
    print(requirement)
PY
"$venv/bin/python" -m pip install -r "$requirements"
"$venv/bin/python" -m pip install --no-deps -e "$project"
"$venv/bin/python" -m pip check
"$venv/bin/python" "$project/scripts/validate_clawtune_integration.py" --clawtune-root "$clawtune"
"$venv/bin/python" -m clawbox.cli experiment --help
printf 'Python environment ready: %s\n' "$venv"
echo 'Next: configure machine.env and host.yaml using docs/guide.md.'
