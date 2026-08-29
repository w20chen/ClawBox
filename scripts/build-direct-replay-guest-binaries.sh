#!/usr/bin/env bash
# Build the ARM64 guest-side replay loop and the existing Tool Bridge without
# requiring a Go toolchain on the Firecracker host.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT="${1:-${ROOT}/.artifacts/direct-replay-guest}"
case "${OUTPUT}" in
  /*) ;;
  *) OUTPUT="${ROOT}/${OUTPUT}" ;;
esac
mkdir -p "${OUTPUT}"

command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 69; }
docker run --rm --user "$(id -u):$(id -g)" \
  -v "${ROOT}:/src" -w /src golang:1.25-bookworm \
  sh -ec '
    export CGO_ENABLED=0 GOOS=linux GOARCH=arm64
    (cd clawbox/replay/guest_runtime && go build -trimpath -ldflags="-s -w" -o "'"${OUTPUT}"'/clawbox-replay-runtime" .)
    (cd toolbridge && go build -trimpath -ldflags="-s -w" -o "'"${OUTPUT}"'/tool-bridge" .)
  '
chmod 0755 "${OUTPUT}/clawbox-replay-runtime" "${OUTPUT}/tool-bridge"
printf 'guest runtime: %s\ntool bridge: %s\n' \
  "${OUTPUT}/clawbox-replay-runtime" "${OUTPUT}/tool-bridge"
