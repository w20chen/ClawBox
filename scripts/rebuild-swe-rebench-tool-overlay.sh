#!/usr/bin/env bash
# Rebuild the current Tool Bridge and overlay it onto the known-good task image.
set -euo pipefail

ROOT="${CLAWBOX_ROOT:-$HOME/ClawBox}"
BASE_IMAGE="${BASE_IMAGE:?set BASE_IMAGE to the immutable SWE-Rebench digest}"
REGISTRY="${REGISTRY:-127.0.0.1:5000/clawbox}"
TAG="${TAG:-p0-envelope}"
BRIDGE_IMAGE="${REGISTRY}/tool-bridge-arm64:${TAG}"
TASK_IMAGE="${REGISTRY}/swe-rebench-arm64:${TAG}"

cd "$ROOT"
docker build --platform linux/arm64 \
  --build-arg GOPROXY="${GOPROXY:-https://goproxy.cn,direct}" \
  -f docker/Dockerfile.tool-bridge -t "$BRIDGE_IMAGE" .
docker run --rm "$BRIDGE_IMAGE" --self-test | grep '"arch":"arm64"'

mkdir -p .artifacts
bridge_container="$(docker create "$BRIDGE_IMAGE")"
overlay_container=""
cleanup() {
  [[ -z "${bridge_container:-}" ]] || docker rm -f "$bridge_container" >/dev/null 2>&1 || true
  [[ -z "${overlay_container:-}" ]] || docker rm -f "$overlay_container" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker cp "$bridge_container:/usr/local/bin/tool-bridge" .artifacts/tool-bridge-overlay
docker rm "$bridge_container" >/dev/null
bridge_container=""
chmod 0555 .artifacts/tool-bridge-overlay

if ! docker build --platform linux/arm64 \
    --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
    -f docker/Dockerfile.swe-rebench-tool-overlay -t "$TASK_IMAGE" .; then
  echo "BuildKit overlay failed; using a stopped-container one-file commit" >&2
  overlay_container="$(docker create "$BASE_IMAGE")"
  docker cp .artifacts/tool-bridge-overlay \
    "$overlay_container:/usr/local/bin/tool-bridge"
  docker commit "$overlay_container" "$TASK_IMAGE" >/dev/null
  docker rm "$overlay_container" >/dev/null
  overlay_container=""
fi
docker run --rm --entrypoint /usr/local/bin/tool-bridge "$TASK_IMAGE" --self-test \
  | grep '"arch":"arm64"'
docker push "$TASK_IMAGE"
digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "$TASK_IMAGE")"
printf 'TOOL_IMAGE=%s\n' "$digest"
