#!/bin/sh
set -eu

mkdir -p /workspace /var/log /run/clawtune /var/lib/clawtune/artifacts/tool-resource
tool_bridge_pid=""
if [ "${CLAWBOX_VM_ROLE:-runtime}" = "tool" ]; then
  : "${CLAWBOX_TOOL_HOST_KEY_B64:?CLAWBOX_TOOL_HOST_KEY_B64 is required}"
  : "${CLAWBOX_TOOL_AUTHORIZED_KEY_B64:?CLAWBOX_TOOL_AUTHORIZED_KEY_B64 is required}"
  mkdir -p /run/clawbox-ssh
  printf '%s' "$CLAWBOX_TOOL_HOST_KEY_B64" | base64 -d >/run/clawbox-ssh/host_key
  printf '%s' "$CLAWBOX_TOOL_AUTHORIZED_KEY_B64" | base64 -d >/run/clawbox-ssh/authorized_key
  chmod 0600 /run/clawbox-ssh/host_key /run/clawbox-ssh/authorized_key
  TOOL_BRIDGE_HOST_KEY=/run/clawbox-ssh/host_key \
  TOOL_BRIDGE_AUTHORIZED_KEY=/run/clawbox-ssh/authorized_key \
  TOOL_BRIDGE_LISTEN=0.0.0.0:2222 \
    /usr/local/bin/tool-bridge >/var/log/tool-bridge.log 2>&1 &
  tool_bridge_pid=$!
fi
/usr/bin/envd -port "${ENVD_PORT:-49983}" >/var/log/envd.log 2>&1 &
envd_pid=$!
trap 'kill "$envd_pid" 2>/dev/null || true; [ -z "$tool_bridge_pid" ] || kill "$tool_bridge_pid" 2>/dev/null || true' TERM INT EXIT
wait "$envd_pid"
