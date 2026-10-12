#!/usr/bin/env bash
# Export the working guest environment, not host credentials or live VM state.
set -euo pipefail
if [[ "${1:-}" == --help ]]; then
  echo 'Usage: bash scripts/export-machine-assets.sh NEW_DIRECTORY [--profile HOST.json] [--with-clawtune]'
  echo 'Export images and their kernel. --profile uses applied image references; --with-clawtune includes matching source for a new host.'
  echo 'Reads CLAWBOX_MACHINE_ENV or ~/.config/clawbox/machine.env. Does not export live VMs or machine configuration.'
  exit 0
fi
(( $# >= 1 )) || { echo 'usage: bash scripts/export-machine-assets.sh NEW_DIRECTORY [--profile HOST.json] [--with-clawtune]' >&2; exit 2; }
destination=$(realpath -m "$1")
shift
profile=
with_clawtune=0
while (( $# )); do
  case "$1" in
    --profile)
      (( $# >= 2 )) || { echo '--profile requires a path' >&2; exit 2; }
      profile=$2; shift 2 ;;
    --with-clawtune) with_clawtune=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
source_file=${CLAWBOX_MACHINE_ENV:-${HOME}/.config/clawbox/machine.env}
set -a
if [[ -f "$source_file" ]]; then
  source "$source_file"
elif [[ -n "${CLAWBOX_MACHINE_ENV:-}" ]]; then
  echo "Machine configuration not found: $source_file" >&2; exit 2
fi
set +a
if [[ -n "$profile" ]]; then
  CLAWBOX_RUNTIME_IMAGE=$(jq -er '.runtime.source_image_reference' "$profile")
  CLAWBOX_TOOL_IMAGE=$(jq -er '.sandbox.source_image_reference' "$profile")
fi
: "${CLAWBOX_RUNTIME_IMAGE:?set the registered Runtime image in machine.env}"
: "${CLAWBOX_TOOL_IMAGE:?set the registered Tool image in machine.env}"
[[ ! -e "$destination" ]] || { echo 'Choose a new export directory.' >&2; exit 1; }
mkdir -p "$destination/kernel"
if (( with_clawtune )); then
  : "${CLAWTUNE_ROOT:?set the source matching the guest images in machine.env}"
  project=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
  python=${CLAWBOX_PYTHON:-$project/.venv/bin/python}
  "$python" "$project/scripts/validate_clawtune_integration.py" --clawtune-root "$CLAWTUNE_ROOT"
  PYTHONPATH="$project${PYTHONPATH:+:$PYTHONPATH}" "$python" - "$CLAWTUNE_ROOT" "$destination" "$CLAWBOX_RUNTIME_IMAGE" "$CLAWBOX_TOOL_IMAGE" <<'PY'
import io, json, subprocess, sys, tarfile
from pathlib import Path
from clawbox.clawtune_integration import source_revision
root, destination = map(Path, sys.argv[1:3])
root = root.resolve()
revision = source_revision(root)
if revision == 'unknown':
    raise SystemExit('ClawTune source needs Git provenance or CLAWTUNE_REVISION')
for reference in sys.argv[3:]:
    image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', reference]))[0]
    environment = dict(value.split('=', 1) for value in image['Config'].get('Env', []) if '=' in value)
    if environment.get('CLAWTUNE_REVISION') != revision:
        raise SystemExit(f'ClawTune source does not match image {reference}; use its original source export')
def include(info):
    relative = Path(info.name).relative_to('ClawTune')
    if any(part in {'.git', '.venv', '__pycache__', '.pytest_cache', 'node_modules'} for part in relative.parts):
        return None
    if relative.name == '.env' or (relative.name.startswith('.env.') and not relative.name.endswith('.example')):
        return None
    if info.issym() and not (root / relative).resolve().is_relative_to(root):
        raise ValueError(f'ClawTune source has an external symlink: {relative}')
    return info
with tarfile.open(destination / 'clawtune-source.tar.gz', 'w:gz') as bundle:
    bundle.add(root, arcname='ClawTune', filter=include)
    if not (root / 'CLAWTUNE_REVISION').exists():
        data = (revision + '\n').encode('ascii')
        info = tarfile.TarInfo('ClawTune/CLAWTUNE_REVISION')
        info.size = len(data)
        bundle.addfile(info, io.BytesIO(data))
(destination / 'clawtune-source.json').write_text(json.dumps({'revision': revision, 'images': sys.argv[3:]}, indent=2) + '\n')
PY
fi
docker tag "$CLAWBOX_RUNTIME_IMAGE" clawbox-transfer/runtime:research
docker tag "$CLAWBOX_TOOL_IMAGE" clawbox-transfer/tool:research
docker save -o "$destination/guest-images.tar" clawbox-transfer/runtime:research clawbox-transfer/tool:research
kernel_root=${CLAWBOX_KERNEL_ROOT:-/usr/local/services/cubetoolbox/cube-kernel-scf}
cp "$kernel_root/vmlinux-bm" "$kernel_root/version" "$kernel_root/version.json" "$destination/kernel/"
(cd "$destination" && sha256sum guest-images.tar kernel/vmlinux-bm kernel/version kernel/version.json > SHA256SUMS)
if (( with_clawtune )); then
  (cd "$destination" && sha256sum clawtune-source.tar.gz clawtune-source.json >> SHA256SUMS)
fi
echo "Exported guest images and the matching kernel to $destination"
echo 'Copy the approved replay trace separately. This export does not include passwords, host configuration, or live VMs.'
