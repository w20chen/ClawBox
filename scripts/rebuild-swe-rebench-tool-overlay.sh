#!/usr/bin/env bash
# Rebuild the current Tool Bridge and overlay it onto the known-good task image.
set -euo pipefail

ROOT="${CLAWBOX_ROOT:-$HOME/ClawBox}"
CLAWTUNE_ROOT="${CLAWTUNE_ROOT:-${ROOT}/../ClawTune}"
BASE_IMAGE="${BASE_IMAGE:?set BASE_IMAGE to the immutable SWE-Rebench digest}"
REGISTRY="${REGISTRY:-127.0.0.1:5000/clawbox}"
TAG="${TAG:-p0-envelope}"
BRIDGE_IMAGE="${REGISTRY}/tool-bridge-arm64:${TAG}"
TASK_IMAGE="${REGISTRY}/swe-rebench-arm64:${TAG}"

cd "$ROOT"
[[ -f "${CLAWTUNE_ROOT}/tools/guest_collector_server.py" ]] || {
  echo "missing sibling ClawTune guest collector: ${CLAWTUNE_ROOT}" >&2
  exit 66
}
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

KATA_SHARE="${KATA_SHARE:-/opt/kata/share/kata-containers}"
debug_kernel="${KATA_SHARE}/vmlinux-debug.container"
debug_release="$(basename "$(readlink -f "${debug_kernel}")")"
debug_release="${debug_release#vmlinux-}"
kernel_version="${debug_release%%-*}"
debug_config="${KATA_SHARE}/config-${debug_release}"
[[ -r "${debug_config}" ]] || { echo "missing ${debug_config}" >&2; exit 66; }
cp "${debug_config}" .artifacts/kata-debug.config
case "${kernel_version}" in
  6.18.28) kernel_source_sha256="f360789483586cf8a20b4ab2bffe76ead6b62c0db1eeb0d917294456c4d77b74" ;;
  *) kernel_source_sha256="${KERNEL_SOURCE_SHA256:-}" ;;
esac
[[ "${kernel_source_sha256}" =~ ^[a-f0-9]{64}$ ]] || {
  echo "reviewed KERNEL_SOURCE_SHA256 required for guest ${kernel_version}" >&2
  exit 65
}
clawtune_revision="${CLAWTUNE_REVISION:-$(git -C "${CLAWTUNE_ROOT}" rev-parse HEAD)}"
[[ "${clawtune_revision}" =~ ^[a-f0-9]{40}$ ]] || {
  echo "CLAWTUNE_REVISION must be an exact 40-character commit id" >&2
  exit 65
}

docker build --platform linux/arm64 --network host \
  --build-context "clawtune=${CLAWTUNE_ROOT}" \
  --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
  --build-arg "CLAWTUNE_REVISION=${clawtune_revision}" \
  --build-arg "KERNEL_VERSION=${kernel_version}" \
  --build-arg "KERNEL_SOURCE_SHA256=${kernel_source_sha256}" \
  --build-arg "APT_MIRROR=${APT_MIRROR:-}" \
  -f docker/Dockerfile.swe-rebench-tool-telemetry -t "$TASK_IMAGE" .
docker run --rm --entrypoint /usr/local/bin/tool-bridge "$TASK_IMAGE" --self-test \
  | grep '"arch":"arm64"'
docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.source.clawtune.revision"}}' \
  "$TASK_IMAGE" | grep -Fx "${clawtune_revision}"
docker push "$TASK_IMAGE"
digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "$TASK_IMAGE")"
printf 'TOOL_IMAGE=%s\n' "$digest"
