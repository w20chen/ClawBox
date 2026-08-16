#!/usr/bin/env bash
set -euo pipefail

: "${TENANT_ID:?TENANT_ID is required}"
: "${RUNTIME_ID:?RUNTIME_ID is required}"
: "${TOOL_SSH_TARGET:?TOOL_SSH_TARGET is required}"
: "${OPENAI_BASE_URL:?OPENAI_BASE_URL is required}"
: "${OPENAI_API_KEY:?OPENAI_API_KEY is required}"
: "${OPENCLAW_MODEL:?OPENCLAW_MODEL is required}"

SIDECAR_PORT="${SIDECAR_PORT:-8765}"
CLAWTUNE_HOME="${CLAWTUNE_HOME:-/opt/clawtune}"
PLUGIN_DIR="${CLAWTUNE_HOME}/packages/clawtune-plugin"
STATE_DIR="/state/${TENANT_ID}"
TRACE_DIR="${STATE_DIR}/traces"
ARTIFACT_DIR="${TRACE_DIR}/tool-resource"
LOG_DIR="${STATE_DIR}/logs"
KNOWN_HOSTS="${STATE_DIR}/ssh/known_hosts"
SIDECAR_PID=""

mkdir -p "${ARTIFACT_DIR}" "${LOG_DIR}" "$(dirname "${KNOWN_HOSTS}")" /workspace
for snapshot in "${CLAWTUNE_HOME}"/cold-start/tool-resource/*-kb.json; do
  [[ -f "${snapshot}" ]] || continue
  destination="${ARTIFACT_DIR}/$(basename "${snapshot}")"
  [[ -e "${destination}" ]] || cp "${snapshot}" "${destination}"
done
rm -f "${STATE_DIR}/ready"

cleanup() {
  if [[ -n "${SIDECAR_PID}" ]] && kill -0 "${SIDECAR_PID}" 2>/dev/null; then
    kill "${SIDECAR_PID}" 2>/dev/null || true
    wait "${SIDECAR_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "[runtime] tenant_id=${TENANT_ID} runtime_id=${RUNTIME_ID} sandbox=runtime" >&2

tool_host="${TOOL_SSH_TARGET#*@}"
tool_host="${tool_host%:*}"
tool_port="${TOOL_SSH_TARGET##*:}"
if [[ "${tool_host}" == "${tool_port}" ]]; then tool_port=22; fi

host_public_key="$(cut -d ' ' -f 1,2 /var/run/secrets/tool-ssh/ssh_host_ed25519_key.pub)"
[[ "${host_public_key}" == ssh-ed25519\ * ]] || { echo "invalid Tool SSH host public key" >&2; exit 1; }
printf '[%s]:%s %s\n' "${tool_host}" "${tool_port}" "${host_public_key}" >"${KNOWN_HOSTS}"
chmod 0600 "${KNOWN_HOSTS}"

env \
  CLAWTUNE_TRACE_DIR="${TRACE_DIR}" \
  CLAWTUNE_TOOL_RESOURCE_ARTIFACT_DIR="${ARTIFACT_DIR}" \
  CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED=false \
  CLAWTUNE_POLICY=observe-only \
  CLAWTUNE_LLM_UPSTREAM_BASE_URL="${OPENAI_BASE_URL}" \
  CLAWTUNE_LLM_UPSTREAM_API_KEY="${OPENAI_API_KEY}" \
  CLAWTUNE_LLM_PROXY_EXPOSE_MODEL="${OPENCLAW_MODEL}" \
  CLAWTUNE_LLM_PROXY_UPSTREAM_MODEL="${UPSTREAM_MODEL:-${OPENCLAW_MODEL}}" \
  "${CLAWTUNE_HOME}/venv/bin/python" -m clawtune_sidecar.main \
    --host 127.0.0.1 --port "${SIDECAR_PORT}" >"${LOG_DIR}/sidecar.log" 2>&1 &
SIDECAR_PID=$!

for _ in $(seq 1 40); do
  curl -fsS "http://127.0.0.1:${SIDECAR_PORT}/health/ready" >/dev/null 2>&1 && break
  kill -0 "${SIDECAR_PID}" 2>/dev/null || { echo "sidecar exited" >&2; exit 1; }
  sleep 0.5
done
curl -fsS "http://127.0.0.1:${SIDECAR_PORT}/health/ready" >/dev/null

openclaw plugins install --link "${PLUGIN_DIR}" >"${LOG_DIR}/plugin.log" 2>&1 || \
  grep -qiE 'already|exists' "${LOG_DIR}/plugin.log"
openclaw plugins enable clawtune >>"${LOG_DIR}/plugin.log" 2>&1 || true

cat >"${STATE_DIR}/openclaw.patch.json" <<EOF
{
  "agents": {"defaults": {"workspace": "/workspace", "sandbox": {
    "mode": "all", "backend": "ssh", "scope": "agent", "workspaceAccess": "rw",
    "ssh": {
      "target": "${TOOL_SSH_TARGET}",
      "workspaceRoot": "/tmp/openclaw-sandboxes",
      "identityFile": "/var/run/secrets/tool-ssh/id_ed25519",
      "knownHostsFile": "${KNOWN_HOSTS}",
      "strictHostKeyChecking": true,
      "updateHostKeys": false
    }
  }}},
  "tools": {
    "exec": {"host": "sandbox", "security": "full", "ask": "off"},
    "elevated": {"enabled": false},
    "sandbox": {"tools": {
      "allow": ["exec", "process", "read", "write", "edit", "apply_patch"],
      "deny": ["browser", "canvas", "nodes", "cron", "gateway"]
    }}
  },
  "plugins": {"entries": {"clawtune": {
    "enabled": true,
    "hooks": {"allowConversationAccess": true},
    "config": {
      "endpoint": "http://127.0.0.1:${SIDECAR_PORT}",
      "mode": "observe",
      "failOpen": true,
      "autoStartSidecar": false,
      "sidecarCommand": "",
      "executionBackend": "hook-only",
      "enableCgroup": false,
      "enableAffinity": false,
      "enableNuma": false,
      "securityBoundaryAccepted": true,
      "trace": {
        "schema_version": 6,
        "include_raw_events": false,
        "include_llm_messages": true,
        "include_tool_outputs": true,
        "redact_sensitive_data": true,
        "flush_span_start": true,
        "trace_dir": "${TRACE_DIR}"
      }
    }
  }}}
}
EOF
openclaw config patch --stdin <"${STATE_DIR}/openclaw.patch.json" >>"${LOG_DIR}/plugin.log" 2>&1

openclaw onboard --non-interactive --accept-risk --skip-health \
  --mode local --auth-choice vllm \
  --custom-base-url "http://127.0.0.1:${SIDECAR_PORT}/v1" \
  --custom-api-key "${OPENAI_API_KEY}" \
  --custom-model-id "${OPENCLAW_MODEL}" >"${LOG_DIR}/onboard.log" 2>&1

touch "${STATE_DIR}/ready"
echo "[runtime] tenant_id=${TENANT_ID} runtime_id=${RUNTIME_ID} ready=true" >&2
exec openclaw gateway run --bind loopback --auth none
