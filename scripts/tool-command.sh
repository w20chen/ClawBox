#!/usr/bin/env bash
set -euo pipefail

: "${TOOL_EXEC_TIMEOUT_SECONDS:=300}"
: "${TOOL_PIDS_LIMIT:=128}"

ulimit -u "${TOOL_PIDS_LIMIT}" 2>/dev/null || true

if [[ -z "${SSH_ORIGINAL_COMMAND:-}" ]]; then
  exec /bin/bash --noprofile --norc
fi

exec timeout --foreground --signal=TERM --kill-after=5s \
  "${TOOL_EXEC_TIMEOUT_SECONDS}s" /bin/bash --noprofile --norc -c "${SSH_ORIGINAL_COMMAND}"
