#!/usr/bin/env bash
# Rebuild tool-bridge + runtime + control-plane images with mirrors (run on
# target).  Restarts from scratch: kills any in-flight build, uses APT_MIRROR
# (tsinghua) because deb.debian.org stalls behind this network's proxy.
# Logs to /tmp/clawbox-rebuild-all2.log; run via nohup.
set -u
LOG=/tmp/clawbox-rebuild-all2.log
: > "$LOG"
{
  echo "== kill stale builds =="
  pkill -f 'build-kubernetes-images.sh' 2>/dev/null || true
  pkill -f 'docker build --platform linux/arm64' 2>/dev/null || true
  sleep 2
  echo "== git rev =="
  cd ~/ClawBox || exit 1
  git rev-parse --short HEAD
  echo "== clawtune sync to v2 =="
  cd ~/ClawTune || exit 1
  git remote add claw https://github.com/w20chen/claw.git 2>/dev/null || git remote set-url claw https://github.com/w20chen/claw.git
  git -c http.proxy= -c https.proxy= fetch claw v2 2>&1
  git reset --hard FETCH_HEAD 2>&1
  echo "clawtune HEAD=$(git rev-parse --short HEAD)"
  echo "== build =="
  cd ~/ClawBox || exit 1
  env -u http_proxy -u https_proxy -u all_proxy \
    REGISTRY=127.0.0.1:5000/clawbox PUSH=1 TAG=dev \
    GOPROXY=https://goproxy.cn,direct \
    NPM_REGISTRY=https://registry.npmmirror.com \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn \
    bash scripts/build-kubernetes-images.sh 2>&1
  echo "BUILD_EXIT=$?"
  echo "== digests =="
  for img in tool-bridge runtime control-plane; do
    docker image inspect --format "{{.Repository}}:dev {{.Id}}" \
      127.0.0.1:5000/clawbox/${img}-arm64:dev 2>&1
  done
  echo "== done =="
} >> "$LOG" 2>&1
