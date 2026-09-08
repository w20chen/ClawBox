#!/usr/bin/env bash
set -euo pipefail
source_dir=/home/weitianc/CubeSandbox-standalone-20260907
docker run --rm --name clawbox-private-copy-shim-build \
  -v "$source_dir:/workspace" -w /workspace/CubeShim \
  --entrypoint bash cube-sandbox-builder:ubuntu2004 \
  -c 'export CUBE_VERSION=v0.7.0-clawbox-private-copy CUBE_BUILD_TIME=2026-09-07T13:25:00Z; export CUBE_COMMIT=$(git -C /workspace rev-parse HEAD); cargo build --release --locked -j 12 -p containerd-shim-cube-rs'
