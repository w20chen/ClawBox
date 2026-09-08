#!/usr/bin/env bash
set -euo pipefail
source_dir=/home/weitianc/CubeSandbox-standalone-20260907
cache_dir=/home/weitianc/.cache/clawbox-cargo
mkdir -p "$cache_dir"
if docker inspect clawbox-private-copy-shim-build >/dev/null 2>&1; then
  docker cp clawbox-private-copy-shim-build:/usr/local/cargo/registry "$cache_dir/"
  docker cp clawbox-private-copy-shim-build:/usr/local/cargo/git "$cache_dir/"
  docker stop clawbox-private-copy-shim-build
  while docker inspect clawbox-private-copy-shim-build >/dev/null 2>&1; do sleep 1; done
fi
docker run --rm --name clawbox-private-copy-shim-build \
  -v "$source_dir:/workspace" -w /workspace/CubeShim \
  -v "$cache_dir/registry:/usr/local/cargo/registry" \
  -v "$cache_dir/git:/usr/local/cargo/git" \
  -e CARGO_HTTP_MULTIPLEXING=false -e CARGO_HTTP_TIMEOUT=60 \
  -e CUBE_VERSION=v0.7.0-clawbox-private-copy \
  -e CUBE_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -e CUBE_COMMIT="$(git -C "$source_dir" rev-parse HEAD)" \
  --entrypoint bash cube-sandbox-builder:ubuntu2004 \
  -c 'cargo build --release --locked -j 12 -p containerd-shim-cube-rs'
