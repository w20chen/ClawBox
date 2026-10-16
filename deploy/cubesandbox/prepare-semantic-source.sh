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
PROVENANCE_PATCH_FILE=${CUBE_PROVENANCE_PATCH:-$SCRIPT_DIR/template-image-provenance.patch}
TIER_API_PATCH_FILE=$SCRIPT_DIR/tiered-memory-api.patch
TIER_ISOLATION_PATCH_FILE=$SCRIPT_DIR/tiered-memory-isolation.patch
TIMING_PATCH_FILE=$SCRIPT_DIR/checkpoint-phase-timing.patch
INCREMENTAL_PATCH_FILE=$SCRIPT_DIR/incremental-cow-snapshot.patch
TEMPLATE_BASE_PATCH_FILE=$SCRIPT_DIR/template-base-lineage.patch
PRIORITY_PATCH_FILE=$SCRIPT_DIR/agentcgroup-priority.patch
NETWORK_PATCH_FILE=$SCRIPT_DIR/build-network.patch
BASE_REUSE_PATCH_FILE=$SCRIPT_DIR/cold-base-reuse.patch
SOURCE_DIR=${CUBE_SOURCE_DIR:-$SCRIPT_DIR/../../.cubesandbox}
PATCHSET_STAMP=$SOURCE_DIR/.clawbox-patchset.sha256
PATCHSET_PENDING=$SOURCE_DIR/.clawbox-patchset.pending

require() {
  command -v "$1" >/dev/null || { echo "missing command: $1" >&2; exit 1; }
}

require git
require sha256sum
require awk

PATCH_FILES=(
  "$PATCH_FILE" "$HAIRPIN_PATCH_FILE" "$PROVENANCE_PATCH_FILE"
  "$TIER_API_PATCH_FILE" "$TIER_ISOLATION_PATCH_FILE" "$TIMING_PATCH_FILE"
  "$INCREMENTAL_PATCH_FILE" "$TEMPLATE_BASE_PATCH_FILE"
  "$PRIORITY_PATCH_FILE" "$NETWORK_PATCH_FILE" "$BASE_REUSE_PATCH_FILE"
)
for patch_file in "${PATCH_FILES[@]}"; do
  [[ -f "$patch_file" ]] || { echo "missing CubeSandbox patch: $patch_file" >&2; exit 1; }
done
PATCHSET_DIGEST=$(
  for patch_file in "${PATCH_FILES[@]}"; do
    sha256sum "$patch_file" | awk '{print $1}'
  done | sha256sum | awk '{print $1}'
)
PATCHSET_ID="$CUBE_SOURCE_TAG $PATCHSET_DIGEST"

require_current_patchset() {
  local marker=$1
  local state=$2
  if [[ ! -f "$marker" || "$(cat "$marker")" != "$PATCHSET_ID" ]]; then
    echo "CubeSandbox source contains a different or unrecorded ClawBox patch set: $SOURCE_DIR" >&2
    echo "Refusing to reuse overlapping patched sources. Preserve this checkout and rerun with --source pointing to a new empty directory." >&2
    echo "Expected patch set: $PATCHSET_ID ($state marker: $marker)" >&2
    exit 1
  fi
}

# The priority patch supersedes hunks in incremental/tier-isolation patches.
if [[ -d "$SOURCE_DIR/.git" ]] && git -C "$SOURCE_DIR" apply --reverse --check "$PRIORITY_PATCH_FILE" >/dev/null 2>&1; then
  if [[ -f "$PATCHSET_STAMP" ]]; then
    require_current_patchset "$PATCHSET_STAMP" complete
  else
    require_current_patchset "$PATCHSET_PENDING" interrupted
  fi
  if ! git -C "$SOURCE_DIR" apply --reverse --check "$NETWORK_PATCH_FILE" >/dev/null 2>&1; then
    git -C "$SOURCE_DIR" apply --check "$NETWORK_PATCH_FILE"
    git -C "$SOURCE_DIR" apply "$NETWORK_PATCH_FILE"
  fi
  if ! git -C "$SOURCE_DIR" apply --reverse --check "$BASE_REUSE_PATCH_FILE" >/dev/null 2>&1; then
    git -C "$SOURCE_DIR" apply --check "$BASE_REUSE_PATCH_FILE"
    git -C "$SOURCE_DIR" apply "$BASE_REUSE_PATCH_FILE"
  fi
  git -C "$SOURCE_DIR" diff --check
  printf '%s\n' "$PATCHSET_ID" > "$PATCHSET_STAMP"
  rm -f "$PATCHSET_PENDING"
  echo "standalone priority patch set already prepared: $SOURCE_DIR"
  exit 0
fi

[[ -f "$PATCH_FILE" ]] || { echo "missing endpoint patch: $PATCH_FILE" >&2; exit 1; }
[[ -f "$HAIRPIN_PATCH_FILE" ]] || { echo "missing hairpin patch: $HAIRPIN_PATCH_FILE" >&2; exit 1; }
[[ -f "$PROVENANCE_PATCH_FILE" ]] || { echo "missing provenance patch: $PROVENANCE_PATCH_FILE" >&2; exit 1; }
[[ -f "$TIER_API_PATCH_FILE" && -f "$TIER_ISOLATION_PATCH_FILE" ]] || { echo "missing tiered memory patches" >&2; exit 1; }
[[ -f "$TIMING_PATCH_FILE" ]] || { echo "missing checkpoint timing patch" >&2; exit 1; }
[[ -f "$INCREMENTAL_PATCH_FILE" ]] || { echo "missing incremental CoW patch" >&2; exit 1; }
[[ -f "$TEMPLATE_BASE_PATCH_FILE" ]] || { echo "missing template base lineage patch" >&2; exit 1; }

# Later patches change some endpoint hunks, so the first patch alone cannot
# recognize a fully prepared checkout. The final patch is applied last.
if [[ -d "$SOURCE_DIR/.git" ]] && { git -C "$SOURCE_DIR" apply --reverse --check "$TIMING_PATCH_FILE" >/dev/null 2>&1 || git -C "$SOURCE_DIR" apply --reverse --check "$TIER_ISOLATION_PATCH_FILE" >/dev/null 2>&1; }; then
  require_current_patchset "$PATCHSET_PENDING" interrupted
  if ! git -C "$SOURCE_DIR" apply --reverse --check "$TIMING_PATCH_FILE" >/dev/null 2>&1; then
    git -C "$SOURCE_DIR" apply --check "$TIMING_PATCH_FILE"
    git -C "$SOURCE_DIR" apply "$TIMING_PATCH_FILE"
  fi
  if ! git -C "$SOURCE_DIR" apply --reverse --check "$INCREMENTAL_PATCH_FILE" >/dev/null 2>&1; then
    git -C "$SOURCE_DIR" apply --check "$INCREMENTAL_PATCH_FILE"
    git -C "$SOURCE_DIR" apply "$INCREMENTAL_PATCH_FILE"
  fi
  if ! git -C "$SOURCE_DIR" apply --reverse --check "$TEMPLATE_BASE_PATCH_FILE" >/dev/null 2>&1; then
    git -C "$SOURCE_DIR" apply --check "$TEMPLATE_BASE_PATCH_FILE"
    git -C "$SOURCE_DIR" apply "$TEMPLATE_BASE_PATCH_FILE"
  fi
  git -C "$SOURCE_DIR" diff --check
  if ! git -C "$SOURCE_DIR" apply --reverse --check "$PRIORITY_PATCH_FILE" >/dev/null 2>&1; then
    git -C "$SOURCE_DIR" apply --check "$PRIORITY_PATCH_FILE"
    git -C "$SOURCE_DIR" apply "$PRIORITY_PATCH_FILE"
  fi
  if ! git -C "$SOURCE_DIR" apply --reverse --check "$NETWORK_PATCH_FILE" >/dev/null 2>&1; then
    git -C "$SOURCE_DIR" apply --check "$NETWORK_PATCH_FILE"
    git -C "$SOURCE_DIR" apply "$NETWORK_PATCH_FILE"
  fi
  if ! git -C "$SOURCE_DIR" apply --reverse --check "$BASE_REUSE_PATCH_FILE" >/dev/null 2>&1; then
    git -C "$SOURCE_DIR" apply --check "$BASE_REUSE_PATCH_FILE"
    git -C "$SOURCE_DIR" apply "$BASE_REUSE_PATCH_FILE"
  fi
  printf '%s\n' "$PATCHSET_ID" > "$PATCHSET_STAMP"
  rm -f "$PATCHSET_PENDING"
  echo "standalone patch set already prepared: $SOURCE_DIR"
  exit 0
fi

if [[ ! -d "$SOURCE_DIR/.git" ]]; then
  mkdir -p "$(dirname "$SOURCE_DIR")"
  git clone --branch "$CUBE_SOURCE_TAG" --depth 1 "$CUBE_SOURCE_URL" "$SOURCE_DIR"
else
  if git -C "$SOURCE_DIR" apply --reverse --check "$PATCH_FILE" >/dev/null 2>&1; then
    require_current_patchset "$PATCHSET_PENDING" interrupted
  else
    [[ -z "$(git -C "$SOURCE_DIR" status --porcelain)" ]] || {
      echo "CubeSandbox source has local changes; refusing to overwrite: $SOURCE_DIR" >&2
      exit 1
    }
    git -C "$SOURCE_DIR" fetch --depth 1 origin "refs/tags/$CUBE_SOURCE_TAG:refs/tags/$CUBE_SOURCE_TAG"
    git -C "$SOURCE_DIR" checkout --detach "$CUBE_SOURCE_TAG"
  fi
fi

printf '%s\n' "$PATCHSET_ID" > "$PATCHSET_PENDING"

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
apply_once "$PROVENANCE_PATCH_FILE" "template image provenance"
apply_once "$TIER_API_PATCH_FILE" "tiered snapshot API and lifecycle timing"
apply_once "$TIER_ISOLATION_PATCH_FILE" "physical memory tier isolation"
apply_once "$TIMING_PATCH_FILE" "checkpoint phase timing"
apply_once "$INCREMENTAL_PATCH_FILE" "incremental CoW RAM snapshots"
apply_once "$TEMPLATE_BASE_PATCH_FILE" "template RAM external base lineage"
apply_once "$PRIORITY_PATCH_FILE" "stable AgentCgroup priority domains"
apply_once "$NETWORK_PATCH_FILE" "configurable Go build module proxy"
apply_once "$BASE_REUSE_PATCH_FILE" "owned COLD template base references"

git -C "$SOURCE_DIR" diff --check
printf '%s\n' "$PATCHSET_ID" > "$PATCHSET_STAMP"
rm -f "$PATCHSET_PENDING"
printf '%s\n' "$SOURCE_DIR"
