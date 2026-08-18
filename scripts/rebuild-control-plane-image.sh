#!/usr/bin/env bash
# Rebuild + push the control-plane image with M1 components (run on target).
# Logs to /tmp/clawbox-build.log; run via nohup so a wedged SSH pty cannot
# stall the local terminal.
set -u
LOG=/tmp/clawbox-build.log
: > "$LOG"
cd ~/ClawBox || exit 1
{
  echo "== git rev =="
  git rev-parse --short HEAD
  echo "== build =="
  env -u http_proxy -u https_proxy -u all_proxy \
    docker build --platform linux/arm64 \
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
