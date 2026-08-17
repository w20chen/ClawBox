#!/usr/bin/env bash
set -euo pipefail

: "${TENANT_ID:?TENANT_ID is required}"
: "${RUNTIME_ID:?RUNTIME_ID is required}"
: "${TOOL_SSH_TARGET:?TOOL_SSH_TARGET is required}"
: "${OPENAI_BASE_URL:?OPENAI_BASE_URL is required}"
: "${OPENAI_API_KEY:?OPENAI_API_KEY is required}"
: "${OPENCLAW_MODEL:?OPENCLAW_MODEL is required}"
: "${OPENCLAW_MODEL_REF:?OPENCLAW_MODEL_REF is required}"

SIDECAR_PORT="${SIDECAR_PORT:-8765}"
CLAWTUNE_HOME="${CLAWTUNE_HOME:-/opt/clawtune}"
PLUGIN_DIR="${CLAWTUNE_HOME}/packages/clawtune-plugin"
STATE_DIR="/state/${TENANT_ID}"
TRACE_DIR="${STATE_DIR}/traces"
ARTIFACT_DIR="${TRACE_DIR}/tool-resource"
LOG_DIR="${STATE_DIR}/logs"
KNOWN_HOSTS="${STATE_DIR}/ssh/known_hosts"

mkdir -p "${ARTIFACT_DIR}" "${LOG_DIR}" "$(dirname "${KNOWN_HOSTS}")" /workspace
for snapshot in "${CLAWTUNE_HOME}"/cold-start/tool-resource/*-kb.json; do
  [[ -f "${snapshot}" ]] || continue
  destination="${ARTIFACT_DIR}/$(basename "${snapshot}")"
  [[ -e "${destination}" ]] || cp "${snapshot}" "${destination}"
done
rm -f "${STATE_DIR}/ready"

echo "[runtime] tenant_id=${TENANT_ID} runtime_id=${RUNTIME_ID} sandbox=runtime" >&2

# Kata on this host cannot share volumes across containers, so the runtime Job
# is a SINGLE container and the clawtune sidecar runs in-process instead of as
# a sibling container. Everything the sidecar needs (TASK_ID, TRACE_UPLOAD_TOKEN,
# TRACE_INGESTER_URL, OPENAI_*, OPENCLAW_MODEL, CLAWBOX_STATE_DIR,
# CLAWTUNE_TRACE_DIR) is already set on this container by the controller.
echo "[runtime] starting clawtune sidecar in-process (port ${SIDECAR_PORT})" >&2
/usr/local/bin/clawtune-sidecar-entrypoint >"${LOG_DIR}/sidecar.log" 2>&1 &
SIDECAR_PID=$!

tool_host="${TOOL_SSH_TARGET#*@}"
tool_host="${tool_host%:*}"
tool_port="${TOOL_SSH_TARGET##*:}"
if [[ "${tool_host}" == "${tool_port}" ]]; then tool_port=22; fi

host_public_key="$(cut -d ' ' -f 1,2 /var/run/secrets/tool-ssh/ssh_host_ed25519_key.pub)"
[[ "${host_public_key}" == ssh-ed25519\ * ]] || { echo "invalid Tool SSH host public key" >&2; exit 1; }
printf '[%s]:%s %s\n' "${tool_host}" "${tool_port}" "${host_public_key}" >"${KNOWN_HOSTS}"
chmod 0600 "${KNOWN_HOSTS}"

for _ in $(seq 1 120); do
  curl -fsS "http://127.0.0.1:${SIDECAR_PORT}/health/ready" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -fsS "http://127.0.0.1:${SIDECAR_PORT}/health/ready" >/dev/null

openclaw plugins install --link "${PLUGIN_DIR}" >"${LOG_DIR}/plugin.log" 2>&1 || \
  grep -qiE 'already|exists' "${LOG_DIR}/plugin.log"
openclaw plugins enable clawtune >>"${LOG_DIR}/plugin.log" 2>&1 || true

cat >"${STATE_DIR}/openclaw.patch.json" <<EOF
{
  "agents": {"defaults": {"workspace": "/workspace", "sandbox": {
    "mode": "all", "backend": "ssh", "scope": "shared", "workspaceAccess": "rw",
    "ssh": {
      "target": "${TOOL_SSH_TARGET}",
      "workspaceRoot": "/testbed",
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

if [[ "${CLAWBOX_TASK_MODE:-gateway}" == benchmark ]]; then
  : "${TASK_ID:?TASK_ID is required in benchmark mode}"
  : "${TASK_PROMPT_FILE:?TASK_PROMPT_FILE is required in benchmark mode}"
  : "${TRACE_UPLOAD_TOKEN:?TRACE_UPLOAD_TOKEN is required in benchmark mode}"
  : "${TRACE_INGESTER_URL:?TRACE_INGESTER_URL is required in benchmark mode}"
  session_id="clawbox-${TASK_ID}"
  task_timeout="${TASK_TIMEOUT_SECONDS:-1800}"
  set +e
  # OpenClaw's `agent --local --json` can linger in its event loop after the run
  # ends (observed: "run ... ended with stopReason=stop" but the process never
  # exits, blocking the whole pipeline). final-answer.json is only populated
  # when the run completes, so once it is non-empty give the process a short
  # grace period, then reap it and treat the run as done.
  openclaw agent --local --agent main --session-id "${session_id}" \
    --model "${OPENCLAW_MODEL_REF}" --message "$(cat "${TASK_PROMPT_FILE}")" \
    --timeout "${task_timeout}" --json \
    >"${STATE_DIR}/final-answer.json" 2>"${LOG_DIR}/agent.log" &
  agent_pid=$!
  agent_status=1
  answer_seen=0
  agent_deadline=$((SECONDS + task_timeout + 120))
  while :; do
    if ! kill -0 "${agent_pid}" 2>/dev/null; then
      wait "${agent_pid}"
      agent_status=$?
      break
    fi
    if [[ -s "${STATE_DIR}/final-answer.json" && "${answer_seen}" == 0 ]]; then
      answer_seen=1
      echo "[runtime] agent run complete; waiting up to 30s for clean exit" >&2
      for _ in $(seq 1 6); do
        sleep 5
        kill -0 "${agent_pid}" 2>/dev/null || break
      done
      if kill -0 "${agent_pid}" 2>/dev/null; then
        echo "[runtime] agent completed but CLI lingered; reaping as success" >&2
        kill -TERM "${agent_pid}" 2>/dev/null || true
        sleep 5
        kill -KILL "${agent_pid}" 2>/dev/null || true
        wait "${agent_pid}" 2>/dev/null
        agent_status=0
      else
        wait "${agent_pid}"
        agent_status=$?
      fi
      break
    fi
    if (( SECONDS >= agent_deadline )); then
      echo "[runtime] agent exceeded deadline; killing" >&2
      kill -KILL "${agent_pid}" 2>/dev/null || true
      wait "${agent_pid}" 2>/dev/null
      agent_status=124
      break
    fi
    sleep 5
  done
  set -e

  tool_target="${TOOL_SSH_TARGET%:*}"
  tool_port="${TOOL_SSH_TARGET##*:}"
  # Bounded SSH: an unbounded raw ssh can stall the whole pipeline and let the
  # job's activeDeadlineSeconds kill it mid-upload (observed in 2026-08-17 E2E).
  timeout 120 ssh -p "${tool_port}" -i /var/run/secrets/tool-ssh/id_ed25519 \
    -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -o "UserKnownHostsFile=${KNOWN_HOSTS}" "${tool_target}" \
    'cd /testbed && git diff --binary --no-ext-diff' >"${STATE_DIR}/patch.diff" 2>"${LOG_DIR}/patch.log" || true
  timeout 120 ssh -p "${tool_port}" -i /var/run/secrets/tool-ssh/id_ed25519 \
    -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -o "UserKnownHostsFile=${KNOWN_HOSTS}" "${tool_target}" \
    'cat /testbed/.clawbox/tool-bridge.jsonl 2>/dev/null || true' \
    >"${TRACE_DIR}/tool-bridge.jsonl" 2>"${LOG_DIR}/bridge-trace.log" || true

  TASK_STATE_DIR="${STATE_DIR}" SESSION_ID="${session_id}" AGENT_STATUS="${agent_status}" \
    "${CLAWTUNE_HOME}/venv/bin/python" - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["TASK_STATE_DIR"])
def read(name: str, limit: int) -> str:
    path = root / name
    if not path.exists():
        return ""
    value = path.read_text(encoding="utf-8", errors="replace")
    return value[-limit:]

status = int(os.environ["AGENT_STATUS"])
payload = {
    "status": "succeeded" if status == 0 else ("timed-out" if status == 124 else "failed"),
    "final_answer": read("final-answer.json", 8_000_000),
    "patch": read("patch.diff", 32_000_000),
    "logs": {
        "agent": read("logs/agent.log", 2_000_000),
        "patch": read("logs/patch.log", 500_000),
        "plugin": read("logs/plugin.log", 500_000),
        "onboard": read("logs/onboard.log", 500_000),
    },
    "session_id": os.environ["SESSION_ID"],
    "metadata": {
        "task_id": os.environ.get("TASK_ID", ""),
        "cell_id": os.environ.get("CELL_ID", ""),
        "resource_profile": os.environ.get("RESOURCE_PROFILE", ""),
        "tool_bridge": os.environ.get("TOOL_SSH_TARGET", ""),
        "agent_exit_code": status,
        "patch_status": "present" if (root / "patch.diff").stat().st_size else "empty",
    },
}
(root / "result.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
PY
  touch "${STATE_DIR}/.runtime-complete"
  upload_deadline=$((SECONDS + 300))
  while [[ ! -f "${STATE_DIR}/.upload-complete" ]]; do
    [[ ! -f "${STATE_DIR}/.upload-failed" ]] || { echo "central artifact upload failed" >&2; exit 1; }
    (( SECONDS < upload_deadline )) || { echo "central artifact upload timed out" >&2; exit 1; }
    sleep 1
  done
  exit "${agent_status}"
fi

exec openclaw gateway run --bind loopback --auth none
