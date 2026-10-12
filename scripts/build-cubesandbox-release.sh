#!/usr/bin/env bash
# Build a standalone release from verified assets without replacing live services.
set -Eeuo pipefail
project=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source_dir=${CUBE_SOURCE_DIR:-$HOME/src/CubeSandbox}
assets=
jobs=1
mirror=upstream
go_proxy=${GOPROXY:-}
while (( $# )); do
  case "$1" in
    --help)
      echo 'Usage: bash scripts/build-cubesandbox-release.sh --assets BUNDLE_DIR [--source SOURCE_DIR] [--jobs 1] [--mirror upstream|cn] [--direct-network]'
      echo 'Prepares patched v0.7.0 sources, checks asset hashes, and builds a release. Does not install services.'
      echo 'Use CUBE_SOURCE_URL for a reachable source mirror and BUILDX_CONFIG for an alternate writable build cache.'
      echo 'Use --go-proxy URL to select the Go module proxy (cn defaults to https://goproxy.cn,direct).'
      exit 0 ;;
    --direct-network)
      unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
      shift ;;
    --source|--assets|--jobs|--mirror|--go-proxy)
      (( $# >= 2 )) || { echo "$1 requires a value" >&2; exit 2; }
      case "$1" in --source) source_dir=$2 ;; --assets) assets=$2 ;; --jobs) jobs=$2 ;; --mirror) mirror=$2 ;; --go-proxy) go_proxy=$2 ;; esac
      shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$assets" && -f "$assets/SHA256SUMS" && -s "$assets/kernel/vmlinux-bm" ]] || {
  echo 'Supply --assets with the exported SHA256SUMS, guest-images.tar and kernel directory.' >&2; exit 2;
}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || { echo '--jobs must be a positive integer' >&2; exit 2; }
[[ "$mirror" == upstream || "$mirror" == cn ]] || { echo '--mirror must be upstream or cn' >&2; exit 2; }
if [[ -z "$go_proxy" ]]; then
  if [[ "$mirror" == cn ]]; then go_proxy=https://goproxy.cn,direct; else go_proxy=https://proxy.golang.org,direct; fi
fi
export GOPROXY="$go_proxy"
export BUILDER_BUILD_ARGS="${BUILDER_BUILD_ARGS:-} --build-arg GOPROXY"
assets=$(realpath "$assets")
(cd "$assets" && sha256sum -c SHA256SUMS)
docker info >/dev/null
docker buildx version >/dev/null || {
  echo 'Docker Buildx is required; on Ubuntu 24.04 install docker-buildx (see docs/installation.md).' >&2; exit 2;
}
# Keep builder activity writable even after earlier root-run Docker commands.
export BUILDX_CONFIG=${BUILDX_CONFIG:-$HOME/.cache/clawbox/buildx}
mkdir -p "$BUILDX_CONFIG"
[[ -w "$BUILDX_CONFIG" ]] || { echo 'Set BUILDX_CONFIG to a writable build cache directory.' >&2; exit 1; }
export CUBE_SOURCE_DIR="$source_dir"
bash "$project/deploy/cubesandbox/prepare-semantic-source.sh"
cd "$source_dir"
[[ -f deploy/one-click/build.env ]] || cp deploy/one-click/build.env.example deploy/one-click/build.env
# Re-evaluate build arguments even when dependency stamps match. In pinned
# v0.7.0 a nonempty value requests a build; 0 keeps Docker's layer cache enabled.
if [[ "$mirror" == cn ]]; then
  make builder-image MIRROR=cn BUILDER_FORCE_REBUILD=0
else
  make builder-image MIRROR= BUILDER_FORCE_REBUILD=0
fi
# Upstream sources build.env after reading the process environment. Supply a
# small overlay so explicit CLI values win without editing the user's file.
build_env=$(mktemp)
trap 'rm -f "$build_env"' EXIT
printf 'source %q\nONE_CLICK_BUILD_JOBS=%q\nONE_CLICK_CUBE_KERNEL_VMLINUX=%q\n' \
  "$PWD/deploy/one-click/build.env" "$jobs" "$assets/kernel/vmlinux-bm" > "$build_env"
printf 'Per-component build progress: %s/deploy/one-click/.work/build-logs/\n' "$PWD"
ONE_CLICK_BUILD_ENV_FILE="$build_env" bash deploy/one-click/build-release-bundle-builder.sh
