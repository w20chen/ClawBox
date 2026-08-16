#!/usr/bin/env bash
# FC-0: read-only audit of a Kata/Firecracker arm64 artifact tree.
set -uo pipefail

KATA_ROOT="${KATA_ROOT:-/opt/kata}"
FIRECRACKER_VERSION="${FIRECRACKER_VERSION:-1.12.1}"
KATA_VERSION="${KATA_VERSION:-3.31.0}"
EMIT=""
PASS=0
FAIL=0

usage() {
  echo "usage: audit-kata-firecracker-arm64.sh [--root DIR] [--kata-version VERSION] [--firecracker-version VERSION] [--emit FILE]" >&2
  exit 64
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) KATA_ROOT="${2:-}"; shift 2 ;;
    --firecracker-version) FIRECRACKER_VERSION="${2:-}"; shift 2 ;;
    --kata-version) KATA_VERSION="${2:-}"; shift 2 ;;
    --emit) EMIT="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

[[ "${FIRECRACKER_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || usage
[[ "${KATA_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || usage
[[ -d "${KATA_ROOT}" ]] || { echo "FAIL Kata root is missing: ${KATA_ROOT}" >&2; exit 1; }

say() { printf '%-72s %s\n' "$1" "$2"; }
pass() { say "$1" PASS; PASS=$((PASS + 1)); }
fail() { say "$1" FAIL; FAIL=$((FAIL + 1)); }
first_file() {
  local candidate
  for candidate in "$@"; do
    [[ -f "${candidate}" ]] && { printf '%s' "${candidate}"; return 0; }
  done
  return 1
}
configured_value() {
  local key="$1"
  [[ -n "${config:-}" ]] || return 1
  sed -n -E "s#^[[:space:]]*${key}[[:space:]]*=[[:space:]]*\"([^\"]*)\".*#\1#p" "${config}" | head -1
}
numeric_value() {
  local key="$1"
  [[ -n "${config:-}" ]] || return 1
  sed -n -E "s#^[[:space:]]*${key}[[:space:]]*=[[:space:]]*([0-9]+(\.[0-9]+)?).*#\1#p" "${config}" | head -1
}
artifact_path() {
  local configured="$1"
  if [[ "${configured}" == /opt/kata/* && "${KATA_ROOT}" != /opt/kata ]]; then
    printf '%s/%s' "${KATA_ROOT}" "${configured#/opt/kata/}"
  else
    printf '%s' "${configured}"
  fi
}

command -v file >/dev/null 2>&1 || { echo "file(1) is required" >&2; exit 69; }

firecracker="$(first_file "${KATA_ROOT}/bin/firecracker" "${KATA_ROOT}/runtime/bin/firecracker" || true)"
if [[ -x "${firecracker}" ]]; then
  elf="$(file -Lb "${firecracker}" 2>/dev/null || true)"
  if grep -Eqi 'ELF 64-bit.*(ARM aarch64|aarch64)' <<<"${elf}"; then
    pass "Firecracker is an aarch64 ELF (${firecracker})"
  else
    fail "Firecracker is not an aarch64 ELF (${elf:-unknown})"
  fi
  version_output="$("${firecracker}" --version 2>&1 || true)"
  if grep -Eq "(^|[^0-9])v?${FIRECRACKER_VERSION//./\\.}([^0-9]|$)" <<<"${version_output}"; then
    pass "Firecracker version is pinned to ${FIRECRACKER_VERSION}"
  else
    fail "Firecracker version differs from ${FIRECRACKER_VERSION} (${version_output:-no output})"
  fi
else
  fail "executable Firecracker binary exists under ${KATA_ROOT}"
fi

jailer="$(first_file "${KATA_ROOT}/bin/jailer" "${KATA_ROOT}/runtime/bin/jailer" || true)"
if [[ -x "${jailer}" ]]; then
  jailer_elf="$(file -Lb "${jailer}" 2>/dev/null || true)"
  jailer_version_output="$("${jailer}" --version 2>&1 || true)"
  if grep -Eqi 'ELF 64-bit.*(ARM aarch64|aarch64)' <<<"${jailer_elf}" \
    && grep -Eq "(^|[^0-9])v?${FIRECRACKER_VERSION//./\\.}([^0-9]|$)" <<<"${jailer_version_output}"; then
    pass "Firecracker jailer is pinned aarch64 (${jailer})"
  else
    fail "Firecracker jailer architecture/version is invalid (${jailer_elf:-unknown}; ${jailer_version_output:-no output})"
  fi
else
  fail "executable Firecracker jailer exists under ${KATA_ROOT}"
fi

config="$(first_file \
  "${KATA_ROOT}/share/defaults/kata-containers/configuration-fc-arm64.toml" \
  "${KATA_ROOT}/share/defaults/kata-containers/runtime-rs/configuration-fc.toml" \
  "${KATA_ROOT}/share/defaults/kata-containers/configuration-rs-fc.toml" \
  "${KATA_ROOT}/share/defaults/kata-containers/configuration-fc.toml" || true)"
if [[ -n "${config}" ]] && grep -Eqi 'firecracker|hypervisor[._-]?name[[:space:]]*=[[:space:]]*"?fc' "${config}"; then
  pass "Firecracker Kata configuration exists (${config})"
  if grep -Eqi '^[[:space:]]*shared_fs[[:space:]]*=[[:space:]]*"virtio-?fs"' "${config}"; then
    fail "Firecracker config depends on virtio-fs"
  else
    pass "Firecracker config does not depend on virtio-fs"
  fi
  grep -Eq '^[[:space:]]*static_sandbox_resource_mgmt[[:space:]]*=[[:space:]]*true([[:space:]]|$)' "${config}" \
    && pass "Firecracker uses static sandbox sizing instead of CPU/memory hotplug" \
    || fail "Firecracker static sandbox sizing is not enabled"
  grep -Eq '^[[:space:]]*disable_guest_empty_dir[[:space:]]*=[[:space:]]*false([[:space:]]|$)' "${config}" \
    && pass "emptyDir stays guest-local instead of using a host shared filesystem" \
    || fail "guest-local emptyDir handling is not enabled"
  fc_default_vcpus="$(numeric_value default_vcpus || true)"
  fc_max_vcpus="$(numeric_value default_maxvcpus || true)"
  if [[ -n "${fc_default_vcpus}" ]] \
    && awk -v v="${fc_default_vcpus}" 'BEGIN { exit !(v >= 1 && v <= 32) }'; then
    pass "Firecracker default_vcpus is pinned within [1,32] (${fc_default_vcpus})"
  else
    fail "Firecracker default_vcpus must be pinned within [1,32] (${fc_default_vcpus:-unset})"
  fi
  if [[ -n "${fc_max_vcpus}" ]] && (( fc_max_vcpus >= 1 && fc_max_vcpus <= 32 )); then
    pass "Firecracker default_maxvcpus is pinned within [1,32] (${fc_max_vcpus})"
  else
    fail "Firecracker default_maxvcpus must be pinned within [1,32] (${fc_max_vcpus:-unset})"
  fi
  if [[ -n "${fc_default_vcpus}" && -n "${fc_max_vcpus}" ]] \
    && awk -v d="${fc_default_vcpus}" -v m="${fc_max_vcpus}" 'BEGIN { exit !(d <= m) }'; then
    pass "Firecracker default_vcpus does not exceed default_maxvcpus"
  else
    fail "Firecracker default_vcpus exceeds default_maxvcpus"
  fi
  # Kata runtime-rs derives its agent-connect retry budget as
  # reconnect_timeout_ms / dial_timeout_ms. 3.31.0 ships an fc config with
  # dial_timeout_ms=45000 > reconnect_timeout_ms=3000, making the retry count 0
  # and panicking the shim ("called Option::unwrap() on a None value" in
  # hybrid_vsock connect). Gate reconnect >= dial (>= 1 retry) with the same
  # 10 s floor upstream added after 3.31.0.
  agent_dial_ms="$(numeric_value dial_timeout_ms || true)"
  agent_reconnect_ms="$(numeric_value reconnect_timeout_ms || true)"
  if [[ -n "${agent_dial_ms}" && "${agent_dial_ms}" -ge 1 ]] \
    && [[ -n "${agent_reconnect_ms}" && "${agent_reconnect_ms}" -ge "${agent_dial_ms}" ]] \
    && (( agent_reconnect_ms >= 10000 )); then
    pass "Kata agent reconnect_timeout_ms >= dial_timeout_ms (${agent_reconnect_ms} >= ${agent_dial_ms}); runtime-rs retry budget is sound"
  else
    fail "Kata agent reconnect_timeout_ms must be >= dial_timeout_ms with a >= 10s budget (reconnect=${agent_reconnect_ms:-unset} dial=${agent_dial_ms:-unset}); runtime-rs retry_times=0 panics in hybrid_vsock connect"
  fi
else
  fail "Firecracker Kata configuration is missing or does not select Firecracker"
fi

configured_kernel="$(configured_value kernel || true)"
kernel="$(artifact_path "${configured_kernel}")"
if [[ -n "${configured_kernel}" && -f "${kernel}" ]]; then
  kernel_type="$(file -Lb "${kernel}" 2>/dev/null || true)"
  if grep -Eqi '(ARM aarch64|aarch64|Linux kernel ARM64)' <<<"${kernel_type}"; then
    pass "arm64 guest kernel exists (${kernel})"
  else
    fail "guest kernel architecture is not proven arm64 (${kernel_type:-unknown})"
  fi
else
  fail "Firecracker config points to an existing arm64 guest kernel (${configured_kernel:-unset})"
fi

configured_image="$(configured_value image || true)"
configured_initrd="$(configured_value initrd || true)"
image="$(artifact_path "${configured_image}")"
if [[ -n "${configured_image}" && -s "${image}" ]]; then
  pass "Firecracker config points to a block rootfs image (${image})"
else
  fail "Firecracker config points to a block rootfs image (${configured_image:-unset})"
fi
[[ -z "${configured_initrd}" ]] && pass "Firecracker config is not initrd-backed" \
  || fail "initrd-only/mixed guest artifacts are unsupported (${configured_initrd})"

shim="$(first_file \
  "${KATA_ROOT}/bin/containerd-shim-kata-v2" \
  "${KATA_ROOT}/runtime-rs/bin/containerd-shim-kata-v2" || true)"
if [[ -x "${shim}" ]]; then
  shim_type="$(file -Lb "${shim}" 2>/dev/null || true)"
  if grep -Eqi 'ELF 64-bit.*(ARM aarch64|aarch64)' <<<"${shim_type}"; then
    pass "Kata containerd shim is an aarch64 ELF (${shim})"
  else
    fail "Kata containerd shim is not an aarch64 ELF (${shim_type:-unknown})"
  fi
  shim_version_output="$("${shim}" --version 2>&1 || true)"
  if grep -Eq "(^|[^0-9])${KATA_VERSION//./\\.}([^0-9]|$)" <<<"${shim_version_output}"; then
    pass "Kata shim version is pinned to ${KATA_VERSION}"
  else
    fail "Kata shim version differs from ${KATA_VERSION} (${shim_version_output:-no output})"
  fi
else
  fail "Kata containerd shim exists"
fi

if [[ -n "${config}" && -n "${firecracker}" ]]; then
  configured_fc="$(grep -E '^[[:space:]]*(path|path_firecracker)[[:space:]]*=' "${config}" | head -1 || true)"
  configured_fc_path="$(sed -n -E 's#^[[:space:]]*(path|path_firecracker)[[:space:]]*=[[:space:]]*"([^"]+)".*#\2#p' <<<"${configured_fc}")"
  resolved_fc="$(artifact_path "${configured_fc_path}")"
  if [[ -n "${configured_fc_path}" && -x "${resolved_fc}" ]] \
    && [[ "$(readlink -f "${resolved_fc}")" == "$(readlink -f "${firecracker}")" ]]; then
    pass "Kata config has an explicit hypervisor path to the audited Firecracker binary (${configured_fc_path})"
  else
    fail "Kata config does not have an explicit hypervisor path to the audited Firecracker binary"
  fi
  configured_jailer_path="$(configured_value jailer_path || true)"
  resolved_jailer="$(artifact_path "${configured_jailer_path}")"
  if [[ -n "${configured_jailer_path}" && -x "${resolved_jailer}" ]] \
    && [[ "$(readlink -f "${resolved_jailer}")" == "$(readlink -f "${jailer}")" ]]; then
    pass "Kata config points to the audited Firecracker jailer (${configured_jailer_path})"
  else
    fail "Kata config does not point to the audited Firecracker jailer"
  fi
fi

echo
echo "FC-0 summary: ${PASS} pass, ${FAIL} fail"
(( FAIL == 0 )) || exit 1

if [[ -n "${EMIT}" ]]; then
  umask 077
  mkdir -p "$(dirname "${EMIT}")"
  {
    printf 'CLAWBOX_FC_AUDITED=1\n'
    printf 'CLAWBOX_FC_VERSION=%q\n' "${FIRECRACKER_VERSION}"
    printf 'CLAWBOX_KATA_VERSION=%q\n' "${KATA_VERSION}"
    printf 'CLAWBOX_KATA_FLAVOR=%q\n' "runtime-rs"
    printf 'CLAWBOX_FC_BINARY=%q\n' "${firecracker}"
    printf 'CLAWBOX_FC_JAILER=%q\n' "${jailer}"
    printf 'CLAWBOX_FC_CONFIG=%q\n' "${config}"
    printf 'CLAWBOX_FC_KERNEL=%q\n' "${kernel}"
    printf 'CLAWBOX_FC_IMAGE=%q\n' "${image}"
    printf 'CLAWBOX_FC_SHIM=%q\n' "${shim}"
  } >"${EMIT}"
  echo "Wrote machine-readable audit: ${EMIT}"
fi
