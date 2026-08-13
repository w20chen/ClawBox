#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLAWTUNE_ROOT="${CLAWTUNE_ROOT:-$(cd "${ROOT}/../ClawTune" 2>/dev/null && pwd)}"
REGISTRY="${REGISTRY:?set REGISTRY, for example registry.example.com/clawbox}"
TAG="${TAG:-dev}"

test -x "${CLAWTUNE_ROOT}/swe_rebench/bundle/entrypoint.sh" || {
  echo "ClawTune bundle missing; run: cd ${CLAWTUNE_ROOT} && python3 -m swe_rebench.runner prepare" >&2
  exit 1
}

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
