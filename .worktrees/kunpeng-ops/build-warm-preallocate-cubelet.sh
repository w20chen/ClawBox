#!/usr/bin/env bash
set -euo pipefail
source_dir=/home/weitianc/CubeSandbox-standalone-20260907
docker run --rm --name clawbox-warm-preallocate-build \
  -v "$source_dir:/workspace" -w /workspace/Cubelet \
  -v /home/weitianc/.cache/cube-sandbox-builder/go:/go \
  -v /home/weitianc/.cache/go-build:/root/.cache/go-build \
  --entrypoint bash cube-sandbox-builder:ubuntu2004 \
  -c 'set -e; gofmt -w services/cubebox/pause_cow.go; go test ./services/cubebox -run "TestDirectPause|TestPause" -count=1; go build -p 12 -o /workspace/_output/bin/cubelet-warm-preallocate ./cmd/cubelet'
