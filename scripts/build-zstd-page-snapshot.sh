#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT=${1:-/usr/local/bin/clawbox-zstd-page-snapshot}
mkdir -p "$(dirname "$OUTPUT")"
gcc -O3 -std=gnu11 -Wall -Wextra \
  "$ROOT/native/zstd_page_snapshot.c" -Wl,-l:libzstd.so.1 -o "$OUTPUT"
("$OUTPUT" 2>&1 || true) | grep -q '^usage:' || {
  echo "built helper did not return usage" >&2
  exit 1
}
echo "$OUTPUT"
