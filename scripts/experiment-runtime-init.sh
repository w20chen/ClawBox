#!/bin/bash
# PID 1 for the paper Runtime VM: OpenClaw + ClawTune + SSH tools.
set -euo pipefail

mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mkdir -p /run /tmp /dev/pts /dev/shm /state/traces /state/logs /home/openclaw/.openclaw
mount -t devpts devpts /dev/pts 2>/dev/null || true
mount -t tmpfs tmpfs /dev/shm 2>/dev/null || true
chmod 1777 /tmp
hostname runtime-vm
printf '127.0.0.1 localhost runtime-vm\n::1 localhost\n' >/etc/hosts

source /etc/clawbox/experiment.env
: "${EXPERIMENT_ID:?}" "${MODEL_BASE_URL:?}" "${MODEL_ID:?}" "${RUNTIME_IP:?}" "${TOOL_SSH_TARGET:?}"
export HOME=/home/openclaw OPENCLAW_HOME=/home/openclaw/.openclaw
export CLAWTUNE_POLICY=observe-only CLAWTUNE_TRACE_DIR=/state/traces
export CLAWTUNE_LLM_UPSTREAM_BASE_URL="${MODEL_BASE_URL}"
export CLAWTUNE_LLM_UPSTREAM_API_KEY=experiment-gateway
export CLAWTUNE_LLM_PROXY_EXPOSE_MODEL="${MODEL_ID}"
export CLAWTUNE_LLM_PROXY_UPSTREAM_MODEL="${MODEL_ID}"
export XDG_CACHE_HOME=/opt/clawtune/cache

echo "[experiment] starting ClawTune" >&2
/opt/clawtune/venv/bin/python --version >&2
if ! timeout 30 /opt/clawtune/venv/bin/python -c \
  'import clawtune_sidecar.main; print("[experiment] ClawTune import ready", flush=True)' >&2; then
  echo "[experiment] ClawTune import failed or timed out" >&2
fi
PYTHONUNBUFFERED=1 /opt/clawtune/venv/bin/python -m clawtune_sidecar.main --host 0.0.0.0 --port 8765 \
  >/state/logs/clawtune.log 2>&1 &
sidecar_pid=$!
sleep 2
if ! kill -0 "$sidecar_pid" 2>/dev/null; then
  wait "$sidecar_pid" || status=$?
  echo "[experiment] ClawTune exited during startup: ${status:-0}" >&2
  cat /state/logs/clawtune.log >&2 || true
  while :; do sleep 3600; done
fi
for _ in $(seq 1 120); do
  curl -fsS "http://${RUNTIME_IP}:8765/health/ready" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -fsS "http://${RUNTIME_IP}:8765/health/ready" >/dev/null
echo "[experiment] ClawTune ready; configuring OpenClaw" >&2

openclaw plugins install --link /opt/clawtune/packages/clawtune-plugin \
  >/state/logs/plugin.log 2>&1 || grep -qiE 'already|exists' /state/logs/plugin.log
openclaw plugins enable clawtune >>/state/logs/plugin.log 2>&1 || true
cat >/state/openclaw.patch.json <<EOF
{
  "agents": {"defaults": {"workspace": "/workspace", "sandbox": {
    "mode": "all", "backend": "ssh", "scope": "shared", "workspaceAccess": "rw",
    "ssh": {"target": "${TOOL_SSH_TARGET}", "workspaceRoot": "/testbed",
      "identityFile": "/etc/clawbox/ssh/id_ed25519",
      "knownHostsFile": "/etc/clawbox/ssh/known_hosts",
      "strictHostKeyChecking": true, "updateHostKeys": false}
  }}},
  "tools": {"exec": {"host": "sandbox", "security": "full", "ask": "off"},
    "elevated": {"enabled": false}, "sandbox": {"tools": {
      "allow": ["exec", "process", "read", "write", "edit", "apply_patch"],
      "deny": ["browser", "canvas", "nodes", "cron", "gateway"]}}},
  "plugins": {"entries": {"clawtune": {"enabled": true,
    "hooks": {"allowConversationAccess": true}, "config": {
      "endpoint": "http://${RUNTIME_IP}:8765", "mode": "observe", "failOpen": false,
      "autoStartSidecar": false, "sidecarCommand": "", "executionBackend": "hook-only",
      "sandboxExecEnvelope": true, "instrumentHosts": ["gateway", "sandbox"],
      "instrumentTools": ["exec"], "enableCgroup": false, "enableAffinity": false,
      "enableNuma": false, "securityBoundaryAccepted": true,
      "trace": {"schema_version": 6, "include_raw_events": false,
        "include_llm_messages": true, "include_tool_outputs": true,
        "redact_sensitive_data": true, "flush_span_start": true,
        "trace_dir": "/state/traces"}
    }}}}
}
EOF
openclaw config patch --stdin </state/openclaw.patch.json >>/state/logs/plugin.log 2>&1
openclaw onboard --non-interactive --accept-risk --skip-health --mode local \
  --auth-choice vllm --custom-base-url "http://${RUNTIME_IP}:8765/v1" \
  --custom-api-key experiment-gateway --custom-model-id "${MODEL_ID}" \
  >/state/logs/onboard.log 2>&1

set +e
openclaw agent --local --agent main --session-id "${EXPERIMENT_ID}" \
  --model "vllm/${MODEL_ID}" --message "$(cat /etc/clawbox/prompt.txt)" \
  --timeout "${TASK_TIMEOUT_SECONDS:-600}" --json \
  >/state/final-answer.json 2>/state/logs/openclaw.log
status=$?
set -e
printf '{"ok":%s,"experiment_id":"%s","openclaw_exit_code":%d}\n' \
  "$(if [ "$status" -eq 0 ]; then echo true; else echo false; fi)" "${EXPERIMENT_ID}" "$status"
touch /state/experiment-complete
while :; do sleep 3600; done
