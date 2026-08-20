#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLAWTUNE_ROOT="${CLAWTUNE_ROOT:-${ROOT}/../ClawTune}"
CLAWTUNE_ROOT="$(cd "${CLAWTUNE_ROOT}" 2>/dev/null && pwd)" || {
  echo "ClawTune checkout not found; set CLAWTUNE_ROOT=/absolute/path/to/ClawTune" >&2
  exit 1
}
REGISTRY="${REGISTRY:?set REGISTRY, for example registry.example.com/clawbox}"
TAG="${TAG:-dev}"
BRIDGE_OUTPUT="${BRIDGE_OUTPUT:-${ROOT}/.artifacts/tool-bridge-arm64}"
CLAWTUNE_REVISION="$(git -C "${CLAWTUNE_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
CLAWBOX_REVISION="$(git -C "${ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"

[[ "$(uname -m)" =~ ^(aarch64|arm64)$ ]] || {
  echo "Kubernetes images must be built on the native arm64 builder" >&2
  exit 1
}
docker_arch="$(docker info --format '{{.Architecture}}')"
[[ "${docker_arch}" =~ ^(aarch64|arm64)$ ]] || {
  echo "Docker engine is not native arm64 (${docker_arch})" >&2
  exit 1
}
if [[ -d /proc/sys/fs/binfmt_misc ]]; then
  foreign_handlers="$(find /proc/sys/fs/binfmt_misc -maxdepth 1 -type f \
    \( -name 'qemu-*' -o -name 'rosetta*' \) -printf '%f\n' 2>/dev/null || true)"
  [[ -z "${foreign_handlers}" ]] || {
    echo "foreign-architecture binfmt handlers must be disabled:" >&2
    printf '%s\n' "${foreign_handlers}" >&2
    exit 1
  }
fi

python3 "${ROOT}/scripts/validate_clawtune_integration.py" \
  --clawtune-root "${CLAWTUNE_ROOT}"

if [[ "${REGISTRY}" == registry.example.com* && "${PUSH:-0}" == 1 ]]; then
  echo "REGISTRY is still the documentation placeholder (${REGISTRY})" >&2
  exit 64
fi

mkdir -p "${BRIDGE_OUTPUT}"
tool_image="${REGISTRY}/tool-bridge-arm64:${TAG}"
runtime_image="${REGISTRY}/runtime-arm64:${TAG}"
control_image="${REGISTRY}/control-plane-arm64:${TAG}"

# Module/registry mirrors for restricted networks; defaults keep the official
# upstreams, so plain `docker build` behaviour is unchanged.
GOPROXY="${GOPROXY:-https://proxy.golang.org,direct}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmjs.org}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}"
APT_MIRROR="${APT_MIRROR:-}"

docker build --platform linux/arm64 --pull \
  --build-arg GOPROXY="${GOPROXY}" \
  -f "${ROOT}/docker/Dockerfile.tool-bridge" \
  -t "${tool_image}" "${ROOT}"
docker build --platform linux/arm64 --pull --build-context clawtune="${CLAWTUNE_ROOT}" \
  --build-arg CLAWTUNE_REVISION="${CLAWTUNE_REVISION}" \
  --build-arg CLAWBOX_REVISION="${CLAWBOX_REVISION}" \
  --build-arg NPM_REGISTRY="${NPM_REGISTRY}" \
  --build-arg PIP_INDEX_URL="${PIP_INDEX_URL}" \
  --build-arg APT_MIRROR="${APT_MIRROR}" \
  -f "${ROOT}/docker/Dockerfile.runtime" \
  -t "${runtime_image}" "${ROOT}"
docker build --platform linux/arm64 --pull --build-context clawtune="${CLAWTUNE_ROOT}" \
  --build-arg CLAWTUNE_REVISION="${CLAWTUNE_REVISION}" \
  --build-arg PIP_INDEX_URL="${PIP_INDEX_URL}" \
  -f "${ROOT}/docker/Dockerfile.control-plane" \
  -t "${control_image}" "${ROOT}"

for image in "${tool_image}" "${runtime_image}" "${control_image}"; do
  architecture="$(docker image inspect --format '{{.Architecture}}' "${image}")"
  [[ "${architecture}" =~ ^(aarch64|arm64)$ ]] \
    || { echo "built image is not native arm64: ${image} (${architecture})" >&2; exit 1; }
done

temporary="$(mktemp -d)"
bridge_container="$(docker create "${tool_image}")"
cleanup() {
  [[ -z "${bridge_container:-}" ]] || docker rm -f "${bridge_container}" >/dev/null 2>&1 || true
  rm -rf -- "${temporary}"
}
trap cleanup EXIT
docker cp "${bridge_container}:/usr/local/bin/tool-bridge" "${temporary}/tool-bridge"
docker rm "${bridge_container}" >/dev/null
bridge_container=""
install -m 0555 "${temporary}/tool-bridge" "${BRIDGE_OUTPUT}/tool-bridge"

if [[ "${PUSH:-0}" == 1 ]]; then
  docker push "${tool_image}"
  docker push "${runtime_image}"
  docker push "${control_image}"
fi

file "${BRIDGE_OUTPUT}/tool-bridge" | grep -Eqi 'ELF 64-bit.*(ARM aarch64|aarch64)'
echo "ARM64 images and static Tool Bridge are ready"
