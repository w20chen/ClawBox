#!/usr/bin/env bash
# run-once.sh — one-shot OpenClaw runner, the container entrypoint.
#
# Inside one Firecracker microVM (one Pod == one tenant) it:
#   1. reads TENANT_ID / TASK_MESSAGE / OPENAI_BASE_URL / OPENAI_API_KEY /
#      OPENCLAW_MODEL / SIDECAR_PORT from the environment;
#   2. creates /state/<TENANT_ID>/;
#   3. initializes the tenant's independent KB (copies the base sqlite KB, or
#      creates an empty one);
#   4. starts the ClawTune scheduler sidecar in the background with that
#      tenant's KB path;
#   5. waits for the sidecar readiness endpoint;
#   6. runs exactly one OpenClaw task (plugin loaded, external LLM via the
#      sidecar proxy, non-interactive);
#   7. prints one JSON result line to stdout;
#   8. kills the sidecar via trap so no process outlives the Pod.
#
# No supervisor/systemd/Redis: just a backgrounded process + a trap.
set -euo pipefail

# ── 1. Inputs ─────────────────────────────────────────────────────────
TENANT_ID="${TENANT_ID:-0}"
TASK_MESSAGE="${TASK_MESSAGE:?TASK_MESSAGE is required}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:?OPENAI_BASE_URL is required}"
OPENAI_API_KEY="${OPENAI_API_KEY:?OPENAI_API_KEY is required}"
OPENCLAW_MODEL="${OPENCLAW_MODEL:?OPENCLAW_MODEL is required}"
SIDECAR_PORT="${SIDECAR_PORT:-8765}"
OPENCLAW_TIMEOUT="${OPENCLAW_TIMEOUT:-300}"

# Paths baked into the runner image.
CLAWTUNE_HOME="${CLAWTUNE_HOME:-/opt/clawtune}"
PLUGIN_DIR="${CLAWTUNE_HOME}/packages/openclaw-plugin"
SIDECAR_PY="${CLAWTUNE_HOME}/venv/bin/python"
BASE_KB="${CLAWTUNE_HOME}/base-kb.sqlite"
OPENCLAW_BIN="${OPENCLAW_BIN:-openclaw}"

# ── 2. State layout for this tenant ───────────────────────────────────
STATE_ROOT="${STATE_ROOT:-/state}"
STATE_DIR="${STATE_ROOT}/${TENANT_ID}"
KB_PATH="${STATE_DIR}/kb.sqlite"
TRACE_DIR="${STATE_DIR}/traces"
LOG_DIR="${STATE_DIR}/logs"
SIDECAR_LOG="${LOG_DIR}/sidecar.log"
OPENCLAW_LOG="${LOG_DIR}/openclaw.log"
PLUGIN_LOG="${LOG_DIR}/plugin.log"
ONBOARD_LOG="${LOG_DIR}/onboard.log"
mkdir -p "${STATE_DIR}" "${TRACE_DIR}" "${LOG_DIR}"

start_ts="$(date +%s)"
status="failed"
rc=1
err=""
SIDECAR_PID=""

# ── cleanup + result reporting (trap) ─────────────────────────────────
cleanup() {
  if [ -n "${SIDECAR_PID}" ] && kill -0 "${SIDECAR_PID}" 2>/dev/null; then
    kill "${SIDECAR_PID}" 2>/dev/null || true
    wait "${SIDECAR_PID}" 2>/dev/null || true
  fi
}

finalize() {
  local end_ts elapsed safe_err
  end_ts="$(date +%s)"
  elapsed="$(awk -v s="${start_ts}" -v e="${end_ts}" 'BEGIN { printf "%.1f", e - s }')"
  if [ "${status}" = "success" ]; then
    printf '{"tenant_id":"%s","status":"success","elapsed_s":%s,"exit_code":%d}\n' \
      "${TENANT_ID}" "${elapsed}" "${rc}"
  else
    safe_err="${err:-unknown}"
    # Error strings are controlled by this script; just flatten newlines so
    # the JSON line stays on one stdout line.
    safe_err="$(printf '%s' "${safe_err}" | tr '\n\r' '  ')"
    printf '{"tenant_id":"%s","status":"failed","elapsed_s":%s,"exit_code":%d,"error":"%s"}\n' \
      "${TENANT_ID}" "${elapsed}" "${rc}" "${safe_err}"
  fi
}

# One EXIT trap (bash overwrites a trap re-set on the same signal): kill the
# sidecar, then print the JSON result line.
_on_exit() {
  cleanup
  finalize
}
trap _on_exit EXIT

fail() {
  err="$1"
  rc="${2:-1}"
  status="failed"
  exit "${rc}"
}

log() { echo "[run-once] $*" >&2; }

# ── 3. Tool presence (fail loudly, never fall back to runc silently) ──
command -v "${OPENCLAW_BIN}" >/dev/null 2>&1 \
  || fail "OpenClaw CLI '${OPENCLAW_BIN}' not found on PATH"
[ -x "${SIDECAR_PY}" ] \
  || fail "sidecar python not found: ${SIDECAR_PY}"
[ -f "${PLUGIN_DIR}/dist/index.js" ] \
  || fail "OpenClaw plugin not found at ${PLUGIN_DIR} (plugin load would fail)"

# ── 4. Per-tenant KB ──────────────────────────────────────────────────
if [ -f "${BASE_KB}" ]; then
  cp "${BASE_KB}" "${KB_PATH}" \
    || fail "KB initialization failed (copy ${BASE_KB} -> ${KB_PATH})"
else
  : > "${KB_PATH}" \
    || fail "KB initialization failed (create ${KB_PATH})"
fi
log "tenant ${TENANT_ID} KB ready: ${KB_PATH}"

# ── 5. Start the sidecar (background) ─────────────────────────────────
# Stage-2 eBPF and cgroup/affinity collection are disabled on purpose: inside
# the microVM there is no privileged eBPF, so requiring it would fail closed.
# The sidecar still serves the plugin hooks and the LLM proxy.
env \
  AGENT_SCHEDULER_DB_PATH="${KB_PATH}" \
  AGENT_SCHEDULER_TRACE_DIR="${TRACE_DIR}" \
  AGENT_SCHEDULER_TOOL_RESOURCE_ARTIFACT_DIR="${TRACE_DIR}/tool-resource" \
  AGENT_SCHEDULER_TOOL_RESOURCE_STAGE2_REQUIRED=false \
  AGENT_SCHEDULER_TOOL_RESOURCE_EBPF_REQUIRED=false \
  AGENT_SCHEDULER_POLICY=observe-only \
  AGENT_SCHEDULER_LLM_UPSTREAM_BASE_URL="${OPENAI_BASE_URL}" \
  AGENT_SCHEDULER_LLM_UPSTREAM_API_KEY="${OPENAI_API_KEY}" \
  AGENT_SCHEDULER_LLM_PROXY_EXPOSE_MODEL="${OPENCLAW_MODEL}" \
  AGENT_SCHEDULER_LLM_PROXY_UPSTREAM_MODEL="${UPSTREAM_MODEL:-${OPENCLAW_MODEL}}" \
  "${SIDECAR_PY}" -m agent_scheduler.main \
    --host 127.0.0.1 --port "${SIDECAR_PORT}" \
  >"${SIDECAR_LOG}" 2>&1 &
SIDECAR_PID=$!
log "sidecar pid ${SIDECAR_PID} on port ${SIDECAR_PORT}"

# ── 6. Wait for sidecar ready (short, bounded polling) ────────────────
ready=0
for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:${SIDECAR_PORT}/health/ready" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! kill -0 "${SIDECAR_PID}" 2>/dev/null; then
    break
  fi
  sleep 0.5
done
[ "${ready}" = 1 ] || fail "sidecar not ready within ~10s (see ${SIDECAR_LOG})"
log "sidecar ready"

# Quick LLM preflight through the sidecar proxy: this surfaces an unreachable
# external endpoint before OpenClaw burns its run.
if ! curl -fsS -H "Authorization: Bearer ${OPENAI_API_KEY}" \
     "http://127.0.0.1:${SIDECAR_PORT}/v1/models" >/dev/null 2>&1; then
  fail "LLM endpoint unreachable via sidecar proxy (OPENAI_BASE_URL=${OPENAI_BASE_URL})"
fi
log "LLM proxy reachable"

# ── 7. Configure OpenClaw for this tenant ─────────────────────────────
# Install + enable the current project's plugin (ClawTune's agent-scheduler).
if ! "${OPENCLAW_BIN}" plugins install --link "${PLUGIN_DIR}" >"${PLUGIN_LOG}" 2>&1; then
  if grep -qiE "already|exists" "${PLUGIN_LOG}"; then
    log "plugin already installed"
  else
    fail "plugin install failed (see ${PLUGIN_LOG})"
  fi
fi
"${OPENCLAW_BIN}" plugins enable agent-scheduler >>"${PLUGIN_LOG}" 2>&1 || true

# Patch the plugin entry: point at this VM's sidecar, never auto-start a
# second one, and use hook-only execution (no privileged launcher needed).
cat >"${LOG_DIR}/plugin-patch.json" <<PATCH
{
  "plugins": {
    "entries": {
      "agent-scheduler": {
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
      }
    }
  }
}
PATCH
"${OPENCLAW_BIN}" config patch --stdin <"${LOG_DIR}/plugin-patch.json" >>"${PLUGIN_LOG}" 2>&1 || true
log "plugin configured"

# Onboard the model provider. This is ClawTune's verified command: OpenClaw
# talks to this VM's sidecar LLM proxy, which forwards to the shared external
# OpenAI-compatible API configured in AGENT_SCHEDULER_LLM_UPSTREAM_*.
if ! "${OPENCLAW_BIN}" onboard --non-interactive --accept-risk --skip-health \
     --mode local --auth-choice vllm \
     --custom-base-url "http://127.0.0.1:${SIDECAR_PORT}/v1" \
     --custom-api-key "${OPENAI_API_KEY}" \
     --custom-model-id "${OPENCLAW_MODEL}" \
     >"${ONBOARD_LOG}" 2>&1; then
  fail "openclaw onboard failed (see ${ONBOARD_LOG})"
fi
log "openclaw onboarded (model ${OPENCLAW_MODEL})"

# ── 8. Run the one-shot task ──────────────────────────────────────────
# CLAW_REPO_KEY gives this tenant its own KB repo namespace in the sidecar.
set +e
CLAW_REPO_KEY="tenant-${TENANT_ID}" \
  "${OPENCLAW_BIN}" agent --local --agent main \
    --model "vllm/${OPENCLAW_MODEL}" \
    --message "${TASK_MESSAGE}" \
    --timeout "${OPENCLAW_TIMEOUT}" \
  >"${OPENCLAW_LOG}" 2>&1
rc=$?
set -e
log "openclaw exited with ${rc}"

if [ "${rc}" -eq 0 ]; then
  status="success"
else
  err="openclaw agent failed (exit ${rc}); log: ${OPENCLAW_LOG}"
fi
exit "${rc}"
