#!/usr/bin/env bash
# Export the working guest environment, not host credentials or live VM state.
set -euo pipefail
[[ $# == 1 ]] || { echo 'usage: bash scripts/export-machine-assets.sh NEW_DIRECTORY' >&2; exit 2; }
source_file=${CLAWBOX_MACHINE_ENV:-${HOME}/.config/clawbox/machine.env}
set -a
source "$source_file"
set +a
: "${CLAWBOX_RUNTIME_IMAGE:?set the registered Runtime image in machine.env}"
: "${CLAWBOX_TOOL_IMAGE:?set the registered Tool image in machine.env}"
destination=$(realpath -m "$1")
[[ ! -e "$destination" ]] || { echo 'Choose a new export directory.' >&2; exit 1; }
mkdir -p "$destination/kernel"
docker tag "$CLAWBOX_RUNTIME_IMAGE" clawbox-transfer/runtime:research
docker tag "$CLAWBOX_TOOL_IMAGE" clawbox-transfer/tool:research
docker save -o "$destination/guest-images.tar" clawbox-transfer/runtime:research clawbox-transfer/tool:research
kernel_root=${CLAWBOX_KERNEL_ROOT:-/usr/local/services/cubetoolbox/cube-kernel-scf}
cp "$kernel_root/vmlinux-bm" "$kernel_root/version" "$kernel_root/version.json" "$destination/kernel/"
echo "Exported guest images and the matching kernel to $destination"
echo 'Copy the approved replay trace separately. This export does not include passwords, host configuration, or live VMs.'
