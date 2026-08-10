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
PLUGIN_DIR="${CLAWTUNE_HOME}/packages/openclaw-plugin"
STATE_DIR="/state/${TENANT_ID}"
TRACE_DIR="${STATE_DIR}/traces"
LOG_DIR="${STATE_DIR}/logs"
KNOWN_HOSTS="${STATE_DIR}/ssh/known_hosts"
SIDECAR_PID=""

mkdir -p "${TRACE_DIR}" "${LOG_DIR}" "$(dirname "${KNOWN_HOSTS}")" /workspace
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
  AGENT_SCHEDULER_DB_PATH="${STATE_DIR}/kb.sqlite" \
  AGENT_SCHEDULER_TRACE_DIR="${TRACE_DIR}" \
  AGENT_SCHEDULER_TOOL_RESOURCE_ARTIFACT_DIR="${TRACE_DIR}/tool-resource" \
  AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED=false \
  AGENT_SCHEDULER_TOOL_RESOURCE_EBPF_REQUIRED=false \
  AGENT_SCHEDULER_POLICY=observe-only \
  AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL="${OPENAI_BASE_URL}" \
  AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY="${OPENAI_API_KEY}" \
  AGENT_SCHEDULER_LLM_PROXY_EXPOSE_MODEL="${OPENCLAW_MODEL}" \
  AGENT_SCHEDULER_LLM_PROXY_UPSTREAM_MODEL="${UPSTREAM_MODEL:-${OPENCLAW_MODEL}}" \
  "${CLAWTUNE_HOME}/venv/bin/python" -m agent_scheduler.main \
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
openclaw plugins enable agent-scheduler >>"${LOG_DIR}/plugin.log" 2>&1 || true

cat >"${STATE_DIR}/openclaw.patch.json" <<EOF
{
  "agents": {"defaults": {"workspace": "/workspace", "sandbox": {
    "mode": "all", "backend": "ssh", "scope": "agent", "workspaceAccess": "none",
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
  "plugins": {"entries": {"agent-scheduler": {
    "enabled": true,
    "hooks": {"allowConversationAccess": true},
    "config": {
      "endpoint": "http://127.0.0.1:${SIDECAR_PORT}",
      "autoStartSidecar": false,
      "sidecarCommand": "",
      "recordRawTrace": true,
      "executionBackend": "hook-only",
      "enableCgroup": false,
      "enableAffinity": false,
      "enableNuma": false,
      "securityBoundaryAccepted": true
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
