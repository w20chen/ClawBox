#!/usr/bin/env bash
set -euo pipefail

# Prepare an official CubeSandbox v0.7.0 source checkout with the small,
# reviewable semantic TCP endpoint API used by ClawBox.  The endpoint patch is
# kept in ClawBox because commit 64102d9 is a local development commit and is
# not present in the public v0.7.0 tag.

CUBE_SOURCE_URL=${CUBE_SOURCE_URL:-https://github.com/TencentCloud/CubeSandbox.git}
CUBE_SOURCE_TAG=${CUBE_SOURCE_TAG:-v0.7.0}
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PATCH_FILE=${CUBE_ENDPOINT_PATCH:-$SCRIPT_DIR/semantic-tcp-endpoint.patch}
HAIRPIN_PATCH_FILE=${CUBE_HAIRPIN_PATCH:-$SCRIPT_DIR/hostport-hairpin.patch}
SOURCE_DIR=${CUBE_SOURCE_DIR:-$SCRIPT_DIR/../../.cubesandbox}

require() {
  command -v "$1" >/dev/null || { echo "missing command: $1" >&2; exit 1; }
}

require git
[[ -f "$PATCH_FILE" ]] || { echo "missing endpoint patch: $PATCH_FILE" >&2; exit 1; }
[[ -f "$HAIRPIN_PATCH_FILE" ]] || { echo "missing hairpin patch: $HAIRPIN_PATCH_FILE" >&2; exit 1; }

if [[ ! -d "$SOURCE_DIR/.git" ]]; then
  mkdir -p "$(dirname "$SOURCE_DIR")"
  git clone --branch "$CUBE_SOURCE_TAG" --depth 1 "$CUBE_SOURCE_URL" "$SOURCE_DIR"
else
  if ! git -C "$SOURCE_DIR" apply --reverse --check "$PATCH_FILE" >/dev/null 2>&1; then
    [[ -z "$(git -C "$SOURCE_DIR" status --porcelain)" ]] || {
      echo "CubeSandbox source has local changes; refusing to overwrite: $SOURCE_DIR" >&2
      exit 1
    }
    git -C "$SOURCE_DIR" fetch --depth 1 origin "refs/tags/$CUBE_SOURCE_TAG:refs/tags/$CUBE_SOURCE_TAG"
    git -C "$SOURCE_DIR" checkout --detach "$CUBE_SOURCE_TAG"
  fi
fi

apply_once() {
  local patch_file=$1
  local label=$2
  if git -C "$SOURCE_DIR" apply --reverse --check "$patch_file" >/dev/null 2>&1; then
    echo "$label patch already applied: $SOURCE_DIR"
  elif git -C "$SOURCE_DIR" apply --check "$patch_file" >/dev/null 2>&1; then
    git -C "$SOURCE_DIR" apply "$patch_file"
    echo "applied $label patch: $SOURCE_DIR"
  else
    echo "$label patch does not apply cleanly to $CUBE_SOURCE_TAG" >&2
    exit 1
  fi
}

apply_once "$PATCH_FILE" "semantic endpoint"
apply_once "$HAIRPIN_PATCH_FILE" "same-node HostPort hairpin"

git -C "$SOURCE_DIR" diff --check
printf '%s\n' "$SOURCE_DIR"
