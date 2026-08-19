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

tool_host="${TOOL_SSH_TARGET#*@}"
tool_host="${tool_host%:*}"
tool_port="${TOOL_SSH_TARGET##*:}"
if [[ "${tool_host}" == "${tool_port}" ]]; then tool_port=22; fi
tool_target="${TOOL_SSH_TARGET%:*}"

host_public_key="$(cut -d ' ' -f 1,2 /var/run/secrets/tool-ssh/ssh_host_ed25519_key.pub)"
[[ "${host_public_key}" == ssh-ed25519\ * ]] || { echo "invalid Tool SSH host public key" >&2; exit 1; }
printf '[%s]:%s %s\n' "${tool_host}" "${tool_port}" "${host_public_key}" >"${KNOWN_HOSTS}"
chmod 0600 "${KNOWN_HOSTS}"

# ── P2: resolve the KB repo key and pull the control-plane KB snapshot ─────
# (benchmark mode).  The ClawTune plugin resolves its repo namespace from
# CLAWTUNE_REPO_KEY, then the git remote of its process cwd, then basename;
# the benchmark repo lives on the tool VM at /testbed, so the runtime derives
# the key from the tool VM's git remote when it is not explicitly pinned.  The
# sidecar loads the KB artifact exactly once at startup, so this pull MUST
# happen before the sidecar starts (fail-open: the image cold-start snapshot
# remains the fallback when the control plane is unreachable).
repo_key="${CLAWBOX_REPO_KEY:-${CLAWTUNE_REPO_KEY:-}}"
kb_tenant="${CLAWBOX_TENANT_ID:-${TENANT_ID}}"
if [[ "${CLAWBOX_TASK_MODE:-gateway}" == benchmark ]]; then
  if [[ -z "${repo_key}" ]]; then
    raw_origin="$(timeout -k 5 30 ssh -p "${tool_port}" -i /var/run/secrets/tool-ssh/id_ed25519 \
      -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
      -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
      -o "UserKnownHostsFile=${KNOWN_HOSTS}" "${tool_target}" \
      'cd /testbed && git remote get-url origin 2>/dev/null || true' 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ -n "${raw_origin}" ]]; then
      repo_key="$(printf '%s' "${raw_origin}" | python3 -c '
import re, sys
url = sys.stdin.read().strip()
url = re.sub(r"\.git/?$", "", url).rstrip("/")
scp = re.match(r"^[^/@]+@[^:]+:(.+)$", url)
path = scp.group(1).lstrip("/") if scp else url
m = re.match(r"^[a-z][a-z0-9+.-]*://(?:[^/@]+@)?[^/]+/(.+)$", path, re.I)
if m:
    path = m.group(1)
parts = [p for p in path.split("/") if p]
print("/".join(parts) if len(parts) >= 2 else "")
' 2>/dev/null)"
    fi
    [[ -n "${repo_key}" ]] || repo_key="testbed"
  fi
  export CLAWTUNE_REPO_KEY="${repo_key}"
  export CLAWBOX_REPO_KEY="${repo_key}"
  echo "[runtime] KB repo_key=${repo_key}" >&2
  if [[ -n "${CLAWBOX_KB_ENDPOINT:-}" && -n "${CLAWBOX_KB_TOKEN:-}" ]]; then
    kb_dest="${ARTIFACT_DIR}/runtime-tool-resource-kb.json"
    kb_tmp="${ARTIFACT_DIR}/.runtime-tool-resource-kb.json.tmp"
    kb_response="${ARTIFACT_DIR}/.runtime-tool-resource-response.json.tmp"
    echo "[runtime] pulling KB snapshot from ${CLAWBOX_KB_ENDPOINT} (repo=${repo_key})" >&2
    if curl -fsS --max-time 30 \
        -H "Authorization: Bearer ${CLAWBOX_KB_TOKEN}" \
        --get --data-urlencode "tenant_id=${kb_tenant}" \
        --data-urlencode "repo=${CLAWBOX_REPO_KEY}" --data-urlencode "format=clawtune" \
        "${CLAWBOX_KB_ENDPOINT}/v1/kb/snapshot" \
        >"${kb_response}" 2>"${LOG_DIR}/kb-pull.log" && \
        python3 - "${kb_response}" "${kb_tmp}" >>"${LOG_DIR}/kb-pull.log" 2>&1 <<'PY'
import json, pathlib, sys
response = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
snapshot = response["snapshot"]
if not isinstance(snapshot, dict):
    raise ValueError("clawtune snapshot is not an object")
pathlib.Path(sys.argv[2]).write_text(json.dumps(snapshot), encoding="utf-8")
print(f"generation={response['generation']} input_count={response['input_count']}")
PY
    then
      mv "${kb_tmp}" "${kb_dest}"
      rm -f "${kb_response}"
      kb_generation="$(sed -n 's/^generation=\([0-9][0-9]*\).*/\1/p' "${LOG_DIR}/kb-pull.log" | tail -1)"
      echo "[runtime] KB snapshot pulled (tenant=${kb_tenant} repo=${CLAWBOX_REPO_KEY} generation=${kb_generation:-unknown})" >&2
    else
      echo "[runtime] WARN: KB pull failed; keeping cold-start snapshot" >&2
      rm -f "${kb_tmp}" "${kb_response}"
    fi
  fi
fi

# Kata on this host cannot share volumes across containers, so the runtime Job
# is a SINGLE container and the clawtune sidecar runs in-process instead of as
# a sibling container. Everything the sidecar needs (TASK_ID, TRACE_UPLOAD_TOKEN,
# TRACE_INGESTER_URL, OPENAI_*, OPENCLAW_MODEL, CLAWBOX_STATE_DIR,
# CLAWTUNE_TRACE_DIR) is already set on this container by the controller.
echo "[runtime] starting clawtune sidecar in-process (port ${SIDECAR_PORT})" >&2
/usr/local/bin/clawtune-sidecar-entrypoint >"${LOG_DIR}/sidecar.log" 2>&1 &
SIDECAR_PID=$!

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
      "sandboxExecEnvelope": true,
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
  # Startup connectivity probe: record DNS + ingester reachability at boot so
  # a later upload failure can be correlated with the VM network state.
  ingester_base="${TRACE_INGESTER_URL:-http://clawbox-ingester.clawbox-system.svc:8084}"
  echo "[runtime] startup probe: resolv.conf:" >&2
  cat /etc/resolv.conf >&2 2>/dev/null || true
  echo "[runtime] startup probe: getent ingester svc:" >&2
  getent hosts clawbox-ingester.clawbox-system.svc >&2 2>/dev/null \
    || echo "[runtime] startup probe: getent ingester FAILED" >&2
  echo "[runtime] startup probe: ingester /healthz:" >&2
  if curl -fsS --max-time 10 "${ingester_base}/healthz" >&2 2>/dev/null; then
    echo "[runtime] startup probe: ingester OK" >&2
  else
    echo "[runtime] startup probe: ingester UNREACHABLE" >&2
  fi
fi

if [[ "${CLAWBOX_TASK_MODE:-gateway}" == benchmark ]]; then
  : "${TASK_ID:?TASK_ID is required in benchmark mode}"
  : "${TASK_PROMPT_FILE:?TASK_PROMPT_FILE is required in benchmark mode}"
  : "${TRACE_UPLOAD_TOKEN:?TRACE_UPLOAD_TOKEN is required in benchmark mode}"
  : "${TRACE_INGESTER_URL:?TRACE_INGESTER_URL is required in benchmark mode}"
  # openclaw's SSH sandbox mirrors the local workspace into a per-scope dir on
  # the tool VM and refuses file tools outside it. The runtime image patches
  # openclaw (see Dockerfile.runtime) so the sandbox container root IS
  # workspaceRoot (/testbed). Pre-create the per-scope marker dir so openclaw's
  # ensureRuntime guard sees it and skips its destructive "replace remote
  # workspace from local" copy, which would otherwise wipe /testbed.
  runtime_root="/testbed/openclaw-ssh-shared-8198076c"
  echo "[runtime] pre-creating sandbox runtime root ${runtime_root} on tool VM" >&2
  timeout -k 10 60 ssh -p "${tool_port}" -i /var/run/secrets/tool-ssh/id_ed25519 \
    -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -o "UserKnownHostsFile=${KNOWN_HOSTS}" "${tool_target}" \
    "mkdir -p '${runtime_root}' && echo runtime-root-ok" >&2 \
    || echo "[runtime] WARN: could not pre-create sandbox runtime root" >&2
  # Record the tool VM's baseline HEAD BEFORE the agent runs so the final patch
  # can include committed changes the agent makes.  `git diff` alone only sees
  # uncommitted working-tree edits, so an agent that `git commit`s its fix
  # would otherwise be reported as patch_status=empty and wrongly fail the
  # "task success" metric.
  echo "[runtime] recording tool VM baseline HEAD for patch extraction" >&2
  timeout -k 5 30 ssh -p "${tool_port}" -i /var/run/secrets/tool-ssh/id_ed25519 \
    -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -o "UserKnownHostsFile=${KNOWN_HOSTS}" "${tool_target}" \
    'cd /testbed && git rev-parse HEAD 2>/dev/null || true' \
    >"${STATE_DIR}/baseline-head" 2>"${LOG_DIR}/baseline-head.log" || true
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
  # -k 10 guarantees the ssh dies even if it ignores SIGTERM while stuck in a
  # syscall inside the Kata guest; otherwise `timeout` waits forever.
  # Merge working-tree changes with committed changes since the recorded
  # baseline HEAD (see the baseline capture before the agent started).  This
  # makes patch extraction robust to agents that `git commit` their fix.
  # A heredoc script is piped over `sh -s` to avoid fragile inline SSH quoting.
  baseline_head="$(tr -d '[:space:]' <"${STATE_DIR}/baseline-head" 2>/dev/null || true)"
  echo "[runtime] collecting patch via ssh (baseline=${baseline_head:-none})" >&2
  # __BASELINE_HEAD__ is a standalone placeholder word (no $ prefix) so the sed
  # below is unambiguous.  When baseline capture failed, it is replaced with an
  # empty string and the committed-diff branch naturally falls through, leaving
  # only the working-tree diff (the pre-fix behaviour).
  cat >"${STATE_DIR}/collect-patch.sh" <<'PATCHEOF'
#!/bin/sh
cd /testbed || exit 0
git diff --binary --no-ext-diff
current="$(git rev-parse HEAD 2>/dev/null || true)"
baseline_head="__BASELINE_HEAD__"
if [ -n "$current" ] && [ -n "$baseline_head" ] && [ "$current" != "$baseline_head" ]; then
  printf '\n# committed changes since baseline %s\n' "$baseline_head"
  git diff --binary --no-ext-diff "$baseline_head" HEAD
fi
PATCHEOF
  sed -i "s/__BASELINE_HEAD__/${baseline_head:-}/g" "${STATE_DIR}/collect-patch.sh"
  timeout -k 10 120 ssh -p "${tool_port}" -i /var/run/secrets/tool-ssh/id_ed25519 \
    -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -o "UserKnownHostsFile=${KNOWN_HOSTS}" "${tool_target}" \
    'sh -s' <"${STATE_DIR}/collect-patch.sh" >"${STATE_DIR}/patch.diff" 2>"${LOG_DIR}/patch.log" || true
  echo "[runtime] collecting tool-bridge trace via ssh" >&2
  timeout -k 10 120 ssh -p "${tool_port}" -i /var/run/secrets/tool-ssh/id_ed25519 \
    -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
    -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -o "UserKnownHostsFile=${KNOWN_HOSTS}" "${tool_target}" \
    'cat /testbed/.clawbox/tool-bridge.jsonl 2>/dev/null || true' \
    >"${TRACE_DIR}/tool-bridge.jsonl" 2>"${LOG_DIR}/bridge-trace.log" || true

  echo "[runtime] writing result.json" >&2
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
  echo "[runtime] result written; awaiting final upload" >&2

  # ── P2: flush this cell's joined observations to the control-plane KB ─────
  # Gather ClawTune span JSONL + tool-bridge JSONL, join on execution_id
  # (exact), HMAC-sign, and POST to the KB endpoint.  Fail-open: any error is
  # logged (kb-flush.log) but never blocks finalization/upload.
  if [[ -n "${CLAWBOX_KB_ENDPOINT:-}" && -n "${CLAWBOX_KB_TOKEN:-}" \
        && -n "${CLAWBOX_KB_INGEST_SECRET:-}" && -n "${CLAWBOX_REPO_KEY:-}" ]]; then
    echo "[runtime] flushing observations to ${CLAWBOX_KB_ENDPOINT} (repo=${CLAWBOX_REPO_KEY})" >&2
    KB_ENDPOINT="${CLAWBOX_KB_ENDPOINT}" KB_TOKEN="${CLAWBOX_KB_TOKEN}" \
      KB_INGEST_SECRET="${CLAWBOX_KB_INGEST_SECRET}" \
      KB_TENANT="${kb_tenant}" KB_REPO="${CLAWBOX_REPO_KEY}" \
      KB_TRACE_DIR="${TRACE_DIR}" KB_BRIDGE="${TRACE_DIR}/tool-bridge.jsonl" \
      KB_LOG="${LOG_DIR}/kb-flush.log" \
      python3 /usr/local/bin/kb-flush.py || \
      echo "[runtime] WARN: KB flush failed (non-fatal)" >&2
  else
    echo "[runtime] KB flush skipped (endpoint/token/ingest-secret/repo not all set)" >&2
  fi

  upload_deadline=$((SECONDS + 300))
  while [[ ! -f "${STATE_DIR}/.upload-complete" ]]; do
    if [[ -f "${STATE_DIR}/.upload-failed" ]]; then
      echo "central artifact upload failed" >&2
      # The sidecar is the one that runs the final upload; dump its log plus
      # live connectivity probes so the root cause lands in `kubectl logs`
      # (survives pod exit, unlike exec).
      echo "--- upload failure diagnostics ---" >&2
      echo "sidecar.log tail (300):" >&2
      tail -n 300 "${LOG_DIR}/sidecar.log" >&2 2>/dev/null || true
      echo "/etc/resolv.conf:" >&2
      cat /etc/resolv.conf >&2 2>/dev/null || true
      echo "getent hosts clawbox-ingester.clawbox-system.svc:" >&2
      getent hosts clawbox-ingester.clawbox-system.svc >&2 2>/dev/null || echo "getent FAILED rc=$?" >&2
      echo "curl ingester /healthz:" >&2
      curl -v --max-time 10 "${TRACE_INGESTER_URL:-http://clawbox-ingester.clawbox-system.svc:8084}/healthz" >&2 2>&1 || echo "curl FAILED rc=$?" >&2
      echo "default route + interfaces:" >&2
      ip route 2>&1 || true
      ip -o addr show 2>&1 | head -10 || true
      echo "--- end upload failure diagnostics ---" >&2
      exit 1
    fi
    (( SECONDS < upload_deadline )) || { echo "central artifact upload timed out" >&2; exit 1; }
    sleep 1
  done
  exit "${agent_status}"
fi

exec openclaw gateway run --bind loopback --auth none
