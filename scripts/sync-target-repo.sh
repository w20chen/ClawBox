#!/bin/bash
# Sync ~/ClawBox on the target to origin/main (discarding local changes).
export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
set -e
cd ~/ClawBox
git -c http.proxy= -c https.proxy= fetch origin main
git reset --hard origin/main
git log --oneline -3
echo "=== eol check ==="
git ls-files --eol scripts/runtime-entrypoint.sh docker/Dockerfile.runtime scripts/artifact-uploader.py
echo "=== entrypoint has pre-create ==="
grep -c "pre-creating sandbox runtime root" scripts/runtime-entrypoint.sh || true
echo "=== done ==="
