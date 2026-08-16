#!/usr/bin/env bash
# FC-1: reproducibly assemble and optionally install the audited arm64 stack.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="plan"
KATA_VERSION="${KATA_VERSION:-3.31.0}"
FIRECRACKER_VERSION="${FIRECRACKER_VERSION:-1.12.1}"
# GitHub's immutable 3.31.0 release metadata digest for
# kata-static-3.31.0-arm64.tar.zst.
KATA_3_31_0_ARM64_SHA256="42a7e67a2c2bf3e97a615c99a293b2bc01ea9c84111fc2bf4abeedb7adc9c2ac"
OUTPUT="${CLAWBOX_FC_OUTPUT:-${ROOT}/.artifacts/kata-fc-arm64}"
STATE_DIR="/var/lib/clawbox-bootstrap"
TMP_DIR=""
# Firecracker caps a microVM at 32 vCPUs. Kata fills unset vCPU knobs with the
# host CPU count (128 on Kunpeng), which fails Firecracker config validation
# with "Firecracker hypervisor can not support 128 vCPUs", so the generated
# TOML pins both values explicitly.
FC_DEFAULT_VCPUS="${CLAWBOX_FC_DEFAULT_VCPUS:-2}"
FC_MAX_VCPUS="${CLAWBOX_FC_MAX_VCPUS:-32}"

usage() {
  cat >&2 <<'EOF'
usage: build-kata-firecracker-arm64.sh [plan|build|install] [options]
  --output DIR       Artifact output (default: .artifacts/kata-fc-arm64)
`build` must run natively on arm64. `install` is the explicit authorization to
replace /opt/kata; the previous tree is retained under the bootstrap backups.
EOF
  exit 64
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    plan|build|install) MODE="$1"; shift ;;
    --output) OUTPUT="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

[[ "${KATA_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || usage
[[ "${FIRECRACKER_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || usage
[[ "${FC_DEFAULT_VCPUS}" =~ ^[1-9][0-9]*$ && "${FC_DEFAULT_VCPUS}" -le 32 ]] || {
  echo "CLAWBOX_FC_DEFAULT_VCPUS must be an integer in [1,32]" >&2; exit 64; }
[[ "${FC_MAX_VCPUS}" =~ ^[1-9][0-9]*$ && "${FC_MAX_VCPUS}" -le 32 ]] || {
  echo "CLAWBOX_FC_MAX_VCPUS must be an integer in [1,32]" >&2; exit 64; }
[[ "${FC_DEFAULT_VCPUS}" -le "${FC_MAX_VCPUS}" ]] || {
  echo "CLAWBOX_FC_DEFAULT_VCPUS must not exceed CLAWBOX_FC_MAX_VCPUS" >&2; exit 64; }

echo "Kata=${KATA_VERSION} Firecracker=${FIRECRACKER_VERSION} arch=arm64"
echo "output=${OUTPUT} mode=${MODE}"
if [[ "${MODE}" == plan ]]; then
  cat <<'EOF'
Build downloads the pinned Kata arm64 release and Firecracker aarch64 release,
verifies publisher SHA256 digests, normalizes a Firecracker configuration, and
runs FC-0. No emulation, binfmt, or foreign-architecture builder is used.
EOF
  exit 0
fi

[[ "$(uname -m)" =~ ^(aarch64|arm64)$ ]] || {
  echo "native arm64 host is required; cross-architecture building is forbidden" >&2
  exit 1
}
for command in curl file grep sed sha256sum tar zstd; do
  command -v "${command}" >/dev/null 2>&1 || { echo "missing command: ${command}" >&2; exit 69; }
done

download() {
  curl -fL --retry 4 --retry-delay 2 --connect-timeout 15 --max-time 1800 "$1" -o "$2"
}
verify_from_sums() {
  local sums="$1" asset="$2"
  grep -E "[ *]$(basename "${asset}")$" "${sums}" >"${asset}.sha256" \
    || { echo "publisher checksum does not list $(basename "${asset}")" >&2; exit 1; }
  (cd "$(dirname "${asset}")" && sha256sum -c "$(basename "${asset}").sha256")
}
verify_sidecar_checksum() {
  local checksum="$1" asset="$2" expected actual
  expected="$(grep -Eo '[a-fA-F0-9]{64}' "${checksum}" | head -1 | tr 'A-F' 'a-f')"
  actual="$(sha256sum "${asset}" | awk '{print $1}')"
  [[ -n "${expected}" && "${actual}" == "${expected}" ]] \
    || { echo "checksum mismatch for $(basename "${asset}")" >&2; exit 1; }
}
verify_expected_checksum() {
  local expected="$1" asset="$2" actual
  [[ "${expected}" =~ ^[a-fA-F0-9]{64}$ ]] \
    || { echo "a 64-character SHA256 is required for $(basename "${asset}")" >&2; exit 1; }
  actual="$(sha256sum "${asset}" | awk '{print $1}')"
  [[ "${actual}" == "${expected,,}" ]] \
    || { echo "checksum mismatch for $(basename "${asset}")" >&2; exit 1; }
}

TMP_DIR="$(mktemp -d)"
trap '[[ -n "${TMP_DIR}" ]] && rm -rf -- "${TMP_DIR}"' EXIT
stage="${TMP_DIR}/stage"
mkdir -p "${stage}"

# Kata 3.31 publishes one arm64 archive. It contains the runtime-rs shim,
# guest kernel/image, and the runtime-rs Firecracker configuration.
kata_asset="kata-static-${KATA_VERSION}-arm64.tar.zst"
kata_base="https://github.com/kata-containers/kata-containers/releases/download/${KATA_VERSION}"
download "${kata_base}/${kata_asset}" "${TMP_DIR}/${kata_asset}"
if [[ "${KATA_VERSION}" == 3.31.0 ]]; then
  kata_sha256="${KATA_ARCHIVE_SHA256:-${KATA_3_31_0_ARM64_SHA256}}"
else
  kata_sha256="${KATA_ARCHIVE_SHA256:-}"
fi
[[ -n "${kata_sha256}" ]] || {
  echo "a reviewed KATA_ARCHIVE_SHA256 is required when overriding Kata ${KATA_VERSION}" >&2
  exit 1
}
verify_expected_checksum "${kata_sha256}" "${TMP_DIR}/${kata_asset}"
tar --zstd -C "${stage}" -xf "${TMP_DIR}/${kata_asset}"

fc_asset="firecracker-v${FIRECRACKER_VERSION}-aarch64.tgz"
fc_base="https://github.com/firecracker-microvm/firecracker/releases/download/v${FIRECRACKER_VERSION}"
download "${fc_base}/${fc_asset}" "${TMP_DIR}/${fc_asset}"
if download "${fc_base}/${fc_asset}.sha256.txt" "${TMP_DIR}/${fc_asset}.publisher-sha256"; then
  verify_sidecar_checksum "${TMP_DIR}/${fc_asset}.publisher-sha256" "${TMP_DIR}/${fc_asset}"
else
  download "${fc_base}/SHA256SUMS" "${TMP_DIR}/fc-SHA256SUMS"
  verify_from_sums "${TMP_DIR}/fc-SHA256SUMS" "${TMP_DIR}/${fc_asset}"
fi
tar -C "${TMP_DIR}" -xzf "${TMP_DIR}/${fc_asset}"

kata_root="${stage}/opt/kata"
[[ -d "${kata_root}" ]] || { echo "Kata archive did not contain /opt/kata" >&2; exit 1; }
fc_release_dir="${TMP_DIR}/release-v${FIRECRACKER_VERSION}-aarch64"
install -Dm0755 "${fc_release_dir}/firecracker-v${FIRECRACKER_VERSION}-aarch64" "${kata_root}/bin/firecracker"
install -Dm0755 "${fc_release_dir}/jailer-v${FIRECRACKER_VERSION}-aarch64" "${kata_root}/bin/jailer"

config_dir="${kata_root}/share/defaults/kata-containers"
source_config=""
for candidate in \
  "${config_dir}/configuration-rs-fc.toml" \
  "${config_dir}/runtime-rs/configuration-rs-fc.toml"; do
  [[ -f "${candidate}" ]] && { source_config="${candidate}"; break; }
done
[[ -n "${source_config}" ]] || {
  echo "pinned Kata release lacks a Firecracker config; refusing to mix artifacts from another tree" >&2
  exit 1
}

mkdir -p "${config_dir}"
target_config="${config_dir}/configuration-fc-arm64.toml"
awk -v default_vcpus="${FC_DEFAULT_VCPUS}" -v max_vcpus="${FC_MAX_VCPUS}" '
  function flush_fc() {
    if (in_fc && !fc_emitted) {
      if (!found_vcpus) { print "default_vcpus = " default_vcpus; found_vcpus = 1 }
      if (!found_maxvcpus) { print "default_maxvcpus = " max_vcpus; found_maxvcpus = 1 }
      fc_emitted = 1
    }
    in_fc = 0
  }
  /^\[hypervisor\.firecracker\][[:space:]]*$/ {
    flush_fc()
    in_fc = 1; fc_emitted = 0; in_runtime = 0
    print; next
  }
  /^\[runtime\][[:space:]]*$/ {
    flush_fc()
    in_fc = 0; in_runtime = 1
    print; next
  }
  /^\[/ {
    flush_fc()
    in_fc = 0; in_runtime = 0
  }
  in_fc && /^[[:space:]]*(path|path_firecracker)[[:space:]]*=/ {
    sub(/=.*/, "= \"/opt/kata/bin/firecracker\""); found=1
  }
  in_fc && /^[[:space:]]*jailer_path[[:space:]]*=/ {
    sub(/=.*/, "= \"/opt/kata/bin/jailer\""); found_jailer=1
  }
  in_fc && /^[[:space:]]*default_vcpus[[:space:]]*=/ {
    sub(/=.*/, "= " default_vcpus); found_vcpus=1
  }
  in_fc && /^[[:space:]]*default_maxvcpus[[:space:]]*=/ {
    sub(/=.*/, "= " max_vcpus); found_maxvcpus=1
  }
  in_runtime && /^[[:space:]]*static_sandbox_resource_mgmt[[:space:]]*=/ {
    sub(/=.*/, "= true"); found_static=1
  }
  in_runtime && /^[[:space:]]*disable_guest_empty_dir[[:space:]]*=/ {
    sub(/=.*/, "= false"); found_guest_emptydir=1
  }
  { print }
  END {
    flush_fc()
    if (!found || !found_jailer || !found_vcpus || !found_maxvcpus || !found_static || !found_guest_emptydir) exit 42
  }
' "${source_config}" >"${target_config}" \
  || { echo "could not normalize the Firecracker hypervisor section in ${source_config}" >&2; exit 1; }
grep -Eq '^[[:space:]]*(path|path_firecracker)[[:space:]]*=[[:space:]]*"/opt/kata/bin/firecracker"' "${target_config}" \
  || { echo "could not normalize Firecracker path in ${source_config}" >&2; exit 1; }
grep -Eq "^[[:space:]]*default_vcpus[[:space:]]*=[[:space:]]*${FC_DEFAULT_VCPUS}([[:space:]]|$)" "${target_config}" \
  || { echo "could not pin Firecracker default_vcpus in ${source_config}" >&2; exit 1; }
grep -Eq "^[[:space:]]*default_maxvcpus[[:space:]]*=[[:space:]]*${FC_MAX_VCPUS}([[:space:]]|$)" "${target_config}" \
  || { echo "could not pin Firecracker default_maxvcpus in ${source_config}" >&2; exit 1; }

bash "${ROOT}/scripts/audit-kata-firecracker-arm64.sh" \
  --root "${kata_root}" --kata-version "${KATA_VERSION}" --firecracker-version "${FIRECRACKER_VERSION}" \
  --emit "${stage}/audit.env"

if [[ "${MODE}" == build ]]; then
  mkdir -p "$(dirname "${OUTPUT}")"
  [[ ! -e "${OUTPUT}" ]] || {
    echo "refusing to overwrite existing artifact output: ${OUTPUT}" >&2
    exit 1
  }
  mv "${stage}" "${OUTPUT}"
  echo "FC-1 artifact ready: ${OUTPUT}"
  exit 0
fi

[[ "${MODE}" == install ]] || usage
[[ "$(id -u)" == 0 ]] || { echo "install must run as root" >&2; exit 1; }
install -d -m 0700 "${STATE_DIR}/backups"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
if [[ -e /opt/kata ]]; then
  cp -a /opt/kata "${STATE_DIR}/backups/opt-kata-${stamp}"
fi
rm -rf -- /opt/kata
cp -a "${kata_root}" /opt/kata
shim=""
for candidate in \
  /opt/kata/runtime-rs/bin/containerd-shim-kata-v2 \
  /opt/kata/bin/containerd-shim-kata-v2; do
  [[ -x "${candidate}" ]] && { shim="${candidate}"; break; }
done
[[ -n "${shim}" ]] || { echo "installed artifact lost its Kata shim" >&2; exit 1; }
ln -sfn "${shim}" /usr/local/bin/containerd-shim-kata-v2
bash "${ROOT}/scripts/audit-kata-firecracker-arm64.sh" \
  --root /opt/kata --kata-version "${KATA_VERSION}" --firecracker-version "${FIRECRACKER_VERSION}" \
  --emit "${STATE_DIR}/firecracker-audit.env"
echo "FC-1 installed. Configure devmapper before installing the handler."
