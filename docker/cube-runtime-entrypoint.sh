#!/bin/sh
set -eu

mkdir -p /workspace /var/log /run/clawtune /var/lib/clawtune/artifacts/tool-resource
collector_pid=""
if [ "${CLAWBOX_VM_ROLE:-runtime}" = "tool" ]; then
  collector_helper="${CLAWTUNE_GUEST_COLLECTOR_HELPER:-/opt/clawtune-guest/tools/guest_collector_server.py}"
  collector_python="${CLAWTUNE_GUEST_COLLECTOR_PYTHON:-/opt/clawtune/venv/bin/python}"
  collector_socket="${CLAWTUNE_GUEST_COLLECTOR_SOCKET:-/run/clawtune/guest-collector.sock}"
  collector_token_file="${CLAWTUNE_GUEST_COLLECTOR_TOKEN_FILE:-/run/clawtune/guest-collector.token}"
  if [ ! -f "$collector_helper" ] || [ ! -x "$collector_python" ]; then
    printf '%s\n' "guest collector helper or Python is unavailable" \
      >/var/log/clawtune-guest-collector.unavailable
  elif ! /usr/local/bin/tool-bridge --prepare-collector \
      >/var/log/clawtune-collector-prepare.log 2>&1; then
    printf '%s\n' "guest tracefs preparation failed; see clawtune-collector-prepare.log" \
      >/var/log/clawtune-guest-collector.unavailable
  else
    umask 077
    token="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
    printf '%s\n' "$token" >"$collector_token_file"
    CLAWTUNE_GUEST_COLLECTOR_TOKEN="$token" "$collector_python" "$collector_helper" \
      --socket "$collector_socket" \
      --artifact-root /var/lib/clawtune/artifacts/tool-resource \
      --max-active "${TOOL_MAX_CONCURRENCY:-1}" \
      >/var/log/clawtune-guest-collector.log 2>&1 &
    collector_pid=$!
  fi
fi
/usr/bin/envd -port "${ENVD_PORT:-49983}" >/var/log/envd.log 2>&1 &
envd_pid=$!
trap 'kill "$envd_pid" 2>/dev/null || true; [ -z "$collector_pid" ] || kill "$collector_pid" 2>/dev/null || true' TERM INT EXIT
wait "$envd_pid"
