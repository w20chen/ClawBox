#!/usr/bin/env bash
# Rebuild and push only the Runtime image from the current ClawBox/ClawTune trees.
set -u

LOG=/tmp/clawbox-runtime-build.log
CLAWBOX_ROOT="${CLAWBOX_ROOT:-$HOME/ClawBox}"
CLAWTUNE_ROOT="${CLAWTUNE_ROOT:-$HOME/ClawTune}"
IMAGE="${IMAGE:-127.0.0.1:5000/clawbox/runtime-arm64:dev}"
APT_MIRROR="${APT_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn}"

: >"$LOG"
{
  set -e
  cd "$CLAWBOX_ROOT"
  revision="$(git -C "$CLAWTUNE_ROOT" rev-parse HEAD)"
  echo "ClawBox=$(git rev-parse --short HEAD) ClawTune=${revision}"
  env -u http_proxy -u https_proxy -u all_proxy \
    docker build --platform linux/arm64 \
      --build-context "clawtune=${CLAWTUNE_ROOT}" \
      --build-arg "CLAWTUNE_REVISION=${revision}" \
      --build-arg "APT_MIRROR=${APT_MIRROR}" \
      --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
      --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
      -f docker/Dockerfile.runtime -t "$IMAGE" .
  docker push "$IMAGE"
  docker run --rm --entrypoint /bin/grep "$IMAGE" \
    -R -n sandboxExecEnvelope /opt/clawtune/packages/clawtune-plugin/dist
  docker image inspect --format 'ID={{.Id}}' "$IMAGE"
  echo RUNTIME_IMAGE_REBUILD_COMPLETE
} >>"$LOG" 2>&1
