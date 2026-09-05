#!/usr/bin/env bash
# Rebuild the current Tool Bridge and overlay it onto the known-good task image.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAWBOX_ROOT:-$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)}"
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
if [[ -n "${KERNEL_VERSION_OVERRIDE:-}" ]]; then
  kernel_version="${KERNEL_VERSION_OVERRIDE}"
  debug_config="${KERNEL_CONFIG:?set KERNEL_CONFIG with KERNEL_VERSION_OVERRIDE}"
else
  debug_release="$(basename "$(readlink -f "${debug_kernel}")")"
  debug_release="${debug_release#vmlinux-}"
  kernel_version="${debug_release%%-*}"
  debug_config="${KATA_SHARE}/config-${debug_release}"
fi
[[ -r "${debug_config}" ]] || { echo "missing ${debug_config}" >&2; exit 66; }
cp "${debug_config}" .artifacts/kata-debug.config
case "${kernel_version}" in
  6.6.119) kernel_source_sha256="3da09b980bb404cc28793479bb2d6c636522679215ffa65a04c893575253e5e8" ;;
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
actual_clawtune_revision="$(git -C "${CLAWTUNE_ROOT}" rev-parse HEAD)"
expected_clawtune_revision="${EXPECTED_CLAWTUNE_REVISION:-76eab6fa5c6333f4e80901c030f10cab0e4ce605}"
[[ "${clawtune_revision}" == "${actual_clawtune_revision}" ]] || {
  echo "CLAWTUNE_REVISION ${clawtune_revision} does not match checkout ${actual_clawtune_revision}" >&2
  exit 65
}
[[ "${clawtune_revision}" == "${expected_clawtune_revision}" ]] || {
  echo "ClawTune revision ${clawtune_revision} is incompatible with this release" >&2
  echo "expected ${expected_clawtune_revision}; check out that exact revision" >&2
  exit 65
}

docker build --platform linux/arm64 --network host \
  --build-context "clawtune=${CLAWTUNE_ROOT}" \
  --build-context "overlay=${ROOT}/.artifacts" \
  --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
  --build-arg "CLAWTUNE_REVISION=${clawtune_revision}" \
  --build-arg "KERNEL_VERSION=${kernel_version}" \
  --build-arg "KERNEL_SOURCE_SHA256=${kernel_source_sha256}" \
  --build-arg "APT_MIRROR=${APT_MIRROR:-}" \
  -f docker/Dockerfile.swe-rebench-tool-telemetry -t "$TASK_IMAGE" .
if [[ -n "${KERNEL_SOURCE_DIR:-}" ]]; then
  kernel_release="${KERNEL_RELEASE_OVERRIDE:?set KERNEL_RELEASE_OVERRIDE with KERNEL_SOURCE_DIR}"
  kernel_output="${KERNEL_OUTPUT_DIR_OVERRIDE:?set KERNEL_OUTPUT_DIR_OVERRIDE with KERNEL_SOURCE_DIR}"
  [[ -f "${KERNEL_SOURCE_DIR}/include/linux/sched.h" ]] || {
    echo "prepared kernel source is incomplete: ${KERNEL_SOURCE_DIR}" >&2
    exit 66
  }
  [[ -f "${kernel_output}/include/generated/autoconf.h" ]] || {
    echo "prepared kernel output is incomplete: ${kernel_output}" >&2
    exit 66
  }
  docker build --platform linux/arm64 \
    --build-context "kernel-source=${KERNEL_SOURCE_DIR}" \
    --build-context "kernel-output=${kernel_output}" \
    --build-arg "BASE_IMAGE=${TASK_IMAGE}" \
    --build-arg "KERNEL_RELEASE=${kernel_release}" \
    -f docker/Dockerfile.swe-rebench-tool-kernel-overlay -t "$TASK_IMAGE" .
fi
docker run --rm --entrypoint /usr/local/bin/tool-bridge "$TASK_IMAGE" --self-test \
  | grep '"arch":"arm64"'
docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.source.clawtune.revision"}}' \
  "$TASK_IMAGE" | grep -Fx "${clawtune_revision}"
docker push "$TASK_IMAGE"
digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "$TASK_IMAGE")"
printf 'TOOL_IMAGE=%s\n' "$digest"
