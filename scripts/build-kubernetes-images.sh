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

CLAWTUNE_BUNDLE="${CLAWTUNE_ROOT}/swe_rebench/.runtime/bundle"

test -x "${CLAWTUNE_BUNDLE}/entrypoint.sh" || {
  echo "ClawTune bundle missing; run: cd ${CLAWTUNE_ROOT} && python3 -m swe_rebench.runner prepare" >&2
  exit 1
}
test -d "${CLAWTUNE_BUNDLE}/plugin" && test -d "${CLAWTUNE_BUNDLE}/scheduler" || {
  echo "ClawTune bundle is incomplete: ${CLAWTUNE_BUNDLE}" >&2
  exit 1
}

if [[ "${REGISTRY}" == "registry.example.com"* && "${PUSH:-0}" == "1" ]]; then
  echo "REGISTRY is still the documentation placeholder (${REGISTRY}); set a real registry or use PUSH=0" >&2
  exit 64
fi

docker build --build-context clawtune="${CLAWTUNE_ROOT}" \
  -f "${ROOT}/docker/Dockerfile.clawtune-bundle" \
  -t "${REGISTRY}/clawtune-swe-bundle:${TAG}" "${ROOT}"
docker build --build-context clawtune="${CLAWTUNE_ROOT}" \
  -f "${ROOT}/docker/Dockerfile.runtime" \
  -t "${REGISTRY}/runtime:${TAG}" "${ROOT}"
docker build -f "${ROOT}/docker/Dockerfile.tool-agent" \
  -t "${REGISTRY}/tool-agent:${TAG}" "${ROOT}"

if [[ "${PUSH:-0}" == "1" ]]; then
  docker push "${REGISTRY}/clawtune-swe-bundle:${TAG}"
  docker push "${REGISTRY}/runtime:${TAG}"
  docker push "${REGISTRY}/tool-agent:${TAG}"
fi
