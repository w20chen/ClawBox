#!/usr/bin/env bash
# Rebuild + push the control-plane image with M1 components (run on target).
# Logs to /tmp/clawbox-build.log; run via nohup so a wedged SSH pty cannot
# stall the local terminal.
set -u
LOG=/tmp/clawbox-build.log
: > "$LOG"
cd ~/ClawBox || exit 1
CLAWTUNE_ROOT="${CLAWTUNE_ROOT:-$(cd ../ClawTune 2>/dev/null && pwd)}"
[[ -n "${CLAWTUNE_ROOT}" && -f "${CLAWTUNE_ROOT}/services/sidecar/pyproject.toml" ]] || exit 1
revision="$(git -C "${CLAWTUNE_ROOT}" rev-parse HEAD)"
{
  echo "== git rev =="
  git rev-parse --short HEAD
  echo "== build =="
  env -u http_proxy -u https_proxy -u all_proxy \
    docker build --platform linux/arm64 \
      --build-context "clawtune=${CLAWTUNE_ROOT}" \
      --build-arg "CLAWTUNE_REVISION=${revision}" \
      --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
      -f docker/Dockerfile.control-plane \
      -t 127.0.0.1:5000/clawbox/control-plane-arm64:dev . 2>&1
  echo "BUILD_EXIT=$?"
  echo "== push =="
  env -u http_proxy -u https_proxy -u all_proxy \
    docker push 127.0.0.1:5000/clawbox/control-plane-arm64:dev 2>&1
  echo "PUSH_EXIT=$?"
  echo "== digest =="
  docker image inspect --format 'ID={{.Id}}' 127.0.0.1:5000/clawbox/control-plane-arm64:dev
  echo "== done =="
} >> "$LOG" 2>&1
