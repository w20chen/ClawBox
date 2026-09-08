#!/usr/bin/env bash
set -euo pipefail

payload='python3 /dev/fd/3 3<<'"'"'PY'"'"'
print("payload-ok")
PY'

echo inherited_fd3
/bin/sh -c 'exec /bin/sh -c "$1"' gate "$payload" 3</dev/null || true

echo closed_fd3
/bin/sh -c 'exec 3<&-; exec /bin/sh -c "$1"' gate "$payload" 3</dev/null || true

echo old_dd_gate
CLAWBOX_GATE_COMMAND="$payload" /bin/sh -lc \
  'dd bs=1 count=1 <&3 >/dev/null 2>&1 || exit 125; exec /bin/sh -c "$CLAWBOX_GATE_COMMAND"' \
  3< <(printf '\1') || true
