#!/usr/bin/env bash
# Build a research-only Tool image containing the exact sibling ClawTune tree
# plus Debian's native arm64 BCC/Clang packages.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CLAWTUNE_ROOT="${CLAWTUNE_ROOT:-${ROOT}/../ClawTune}"
BASE_IMAGE="${BASE_IMAGE:-127.0.0.1:5000/clawbox/runtime-arm64:dev}"
IMAGE="${IMAGE:-127.0.0.1:5000/clawbox/tool-telemetry-research:dev}"
APT_MIRROR="${APT_MIRROR:-mirrors.tuna.tsinghua.edu.cn}"
KATA_SHARE="${KATA_SHARE:-/opt/kata/share/kata-containers}"
DEBUG_KERNEL="${KATA_SHARE}/vmlinux-debug.container"
KERNEL_SOURCE_SHA256_6_18_28="f360789483586cf8a20b4ab2bffe76ead6b62c0db1eeb0d917294456c4d77b74"

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo -n /usr/bin/docker info >/dev/null 2>&1; then
  DOCKER=(sudo -n /usr/bin/docker)
else
  echo "Docker is not accessible directly or through passwordless sudo" >&2
  exit 77
fi

[[ "$(uname -m)" =~ ^(aarch64|arm64)$ ]] || {
  echo "research image must be built on native arm64" >&2
  exit 1
}
[[ -d "${CLAWTUNE_ROOT}/services/sidecar" ]] || {
  echo "missing sibling ClawTune checkout: ${CLAWTUNE_ROOT}" >&2
  exit 66
}
[[ -f "${CLAWTUNE_ROOT}/tools/check_guest_ebpf.py" ]] || {
  echo "ClawTune guest smoke entry point is missing" >&2
  exit 66
}
[[ -r "${DEBUG_KERNEL}" ]] || { echo "missing ${DEBUG_KERNEL}" >&2; exit 66; }
debug_release="$(basename "$(readlink -f "${DEBUG_KERNEL}")")"
debug_release="${debug_release#vmlinux-}"
debug_config="${KATA_SHARE}/config-${debug_release}"
[[ -r "${debug_config}" ]] || { echo "missing ${debug_config}" >&2; exit 66; }
kernel_version="${debug_release%%-*}"
case "${kernel_version}" in
  6.18.28) kernel_source_sha256="${KERNEL_SOURCE_SHA256_6_18_28}" ;;
  *)
    kernel_source_sha256="${KERNEL_SOURCE_SHA256:-}"
    [[ "${kernel_source_sha256}" =~ ^[a-f0-9]{64}$ ]] || {
      echo "reviewed KERNEL_SOURCE_SHA256 required for guest ${kernel_version}" >&2
      exit 65
    } ;;
esac

context="$(mktemp -d)"
cleanup() { rm -rf -- "${context}"; }
trap cleanup EXIT INT TERM
cp -a "${CLAWTUNE_ROOT}/services/sidecar" "${context}/sidecar"
cp "${CLAWTUNE_ROOT}/tools/check_guest_ebpf.py" "${context}/check_guest_ebpf.py"
cp "${ROOT}/docker/Dockerfile.tool-telemetry-research" "${context}/Dockerfile"
cp "${debug_config}" "${context}/kata-debug.config"

"${DOCKER[@]}" build --network host \
  --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
  --build-arg "APT_MIRROR=${APT_MIRROR}" \
  --build-arg "KERNEL_VERSION=${kernel_version}" \
  --build-arg "KERNEL_SOURCE_SHA256=${kernel_source_sha256}" \
  --label "org.opencontainers.image.source.clawtune.revision=$(git -C "${CLAWTUNE_ROOT}" rev-parse HEAD)" \
  -t "${IMAGE}" "${context}"
"${DOCKER[@]}" image inspect --format '{{.Architecture}}' "${IMAGE}" | grep -Eq 'arm64|aarch64'
if [[ "${PUSH:-0}" == 1 ]]; then
  "${DOCKER[@]}" push "${IMAGE}"
fi
echo "${IMAGE}"
