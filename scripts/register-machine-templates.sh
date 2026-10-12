#!/usr/bin/env bash
# Register a matched Runtime/Tool pair and save the IDs only after both succeed.
set -Eeuo pipefail
project=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
machine=${CLAWBOX_MACHINE_ENV:-$HOME/.config/clawbox/machine.env}
output=
profile=
runtime_memory=2048 tool_memory=4096 cpu=2000 runtime_disk=20G tool_disk=40G
kernel=/usr/local/services/cubetoolbox/cube-kernel-scf/version.json
while (( $# )); do
  case "$1" in
    --help)
      echo 'Usage: bash scripts/register-machine-templates.sh --output-dir NEW_DIR [--machine-env FILE]'
      echo 'Options: --runtime-memory-mib 2048 --tool-memory-mib 4096 --cpu-millicores 2000'
      echo '         --runtime-disk 20G --tool-disk 40G --kernel-manifest FILE'
      echo '         --profile HOST.json (use the applied images and node, e.g. when resizing existing templates)'
      exit 0 ;;
    --output-dir|--machine-env|--profile|--runtime-memory-mib|--tool-memory-mib|--cpu-millicores|--runtime-disk|--tool-disk|--kernel-manifest)
      (( $# >= 2 )) || { echo "$1 requires a value" >&2; exit 2; }
      case "$1" in
        --output-dir) output=$2 ;; --machine-env) machine=$2 ;;
        --profile) profile=$2 ;;
        --runtime-memory-mib) runtime_memory=$2 ;; --tool-memory-mib) tool_memory=$2 ;;
        --cpu-millicores) cpu=$2 ;; --runtime-disk) runtime_disk=$2 ;;
        --tool-disk) tool_disk=$2 ;; --kernel-manifest) kernel=$2 ;;
      esac
      shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$output" && ! -e "$output" ]] || { echo 'Choose a new --output-dir; existing registration records are never overwritten.' >&2; exit 2; }
set -a
source "$machine"
set +a
if [[ -n "$profile" ]]; then
  CLAWBOX_RUNTIME_IMAGE=$(jq -er '.runtime.source_image_reference' "$profile")
  CLAWBOX_TOOL_IMAGE=$(jq -er '.sandbox.source_image_reference' "$profile")
  CUBE_NODE=$(jq -er '.node' "$profile")
  export CLAWBOX_RUNTIME_IMAGE CLAWBOX_TOOL_IMAGE CUBE_NODE
fi
: "${CLAWBOX_RUNTIME_IMAGE:?set the Runtime image digest in machine.env}"
: "${CLAWBOX_TOOL_IMAGE:?set the Tool image digest in machine.env}"
: "${CUBE_NODE:?set the CubeSandbox node in machine.env}"
for image in "$CLAWBOX_RUNTIME_IMAGE" "$CLAWBOX_TOOL_IMAGE"; do
  [[ "$image" =~ @sha256:[a-f0-9]{64}$ ]] || { echo 'Use immutable registry/name@sha256:... images.' >&2; exit 2; }
done
for value in "$runtime_memory" "$tool_memory" "$cpu"; do
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || { echo 'Memory and CPU values must be positive integers.' >&2; exit 2; }
done
component=$(jq -er '.variants.bm.version' "$kernel")
python=${CLAWBOX_PYTHON:-$project/.venv/bin/python}
mkdir -p "$output"
suffix=$(date -u +%Y%m%d%H%M%S)-$$
printf 'runtime-%s\ntool-%s\n' "$suffix" "$suffix" > "$output/aliases.txt"
export PYTHONPATH="$project${PYTHONPATH:+:$PYTHONPATH}"
"$python" "$project/scripts/register-cube-template.py" "$CLAWBOX_RUNTIME_IMAGE" \
  --alias "runtime-$suffix" --node "$CUBE_NODE" --cpu-millicores "$cpu" \
  --memory-mib "$runtime_memory" --writable-layer-size "$runtime_disk" \
  --exposed-port 49983 --probe-port 49983 --command /usr/local/bin/cube-runtime-entrypoint.sh \
  --expected-kernel-version "$component" > "$output/runtime.json"
"$python" "$project/scripts/register-cube-template.py" "$CLAWBOX_TOOL_IMAGE" \
  --alias "tool-$suffix" --node "$CUBE_NODE" --cpu-millicores "$cpu" \
  --memory-mib "$tool_memory" --writable-layer-size "$tool_disk" \
  --exposed-port 49983 --exposed-port 2222 --probe-port 49983 --command /usr/local/bin/cube-tool-entrypoint.sh \
  --expected-kernel-version "$component" > "$output/tool.json"
"$python" - "$machine" "$output" <<'PY'
import json, os, re, shlex, shutil, sys, tempfile
from pathlib import Path
machine, output = map(Path, sys.argv[1:])
changes = {f'CLAWBOX_{role.upper()}_TEMPLATE': json.loads((output / f'{role}.json').read_text())['template_id']
           for role in ('runtime', 'tool')}
changes.update({key: os.environ[key] for key in ('CLAWBOX_RUNTIME_IMAGE', 'CLAWBOX_TOOL_IMAGE', 'CUBE_NODE')})
text = machine.read_text()
shutil.copy2(machine, output / 'machine.env.before')
os.chmod(output / 'machine.env.before', 0o600)
for key, value in changes.items():
    text = re.sub(rf'^(?:export\s+)?{key}=.*\n?', '', text, flags=re.M)
    text = text.rstrip() + '\n' + key + '=' + shlex.quote(value) + '\n'
fd, name = tempfile.mkstemp(prefix='.machine-', dir=machine.parent)
try:
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
    os.replace(name, machine)
finally:
    Path(name).unlink(missing_ok=True)
print(f'Registered both templates; saved IDs in {machine}. Next: host init --detect, check and apply.')
PY
