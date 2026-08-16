#!/usr/bin/env bash
set -euo pipefail

: "${TASK_ID:?TASK_ID is required}"
: "${TRACE_UPLOAD_TOKEN:?TRACE_UPLOAD_TOKEN is required}"
: "${TRACE_INGESTER_URL:?TRACE_INGESTER_URL is required}"
: "${OPENAI_BASE_URL:?OPENAI_BASE_URL is required}"
: "${OPENAI_API_KEY:?OPENAI_API_KEY is required}"
: "${OPENCLAW_MODEL:?OPENCLAW_MODEL is required}"

export CLAWTUNE_POLICY=observe-only
export CLAWTUNE_TRACE_DIR="${CLAWTUNE_TRACE_DIR:-/state/traces}"
export CLAWTUNE_TOOL_RESOURCE_ARTIFACT_DIR="${CLAWTUNE_TRACE_DIR}/tool-resource"
export CLAWTUNE_TOOL_RESOURCE_EBPF_REQUIRED=false
export CLAWTUNE_LLM_UPSTREAM_BASE_URL="${OPENAI_BASE_URL}"
export CLAWTUNE_LLM_UPSTREAM_API_KEY="${OPENAI_API_KEY}"
export CLAWTUNE_LLM_PROXY_EXPOSE_MODEL="${OPENCLAW_MODEL}"
export CLAWTUNE_LLM_PROXY_UPSTREAM_MODEL="${UPSTREAM_MODEL:-${OPENCLAW_MODEL}}"
mkdir -p "${CLAWTUNE_TRACE_DIR}"
completion_request="${CLAWBOX_STATE_DIR}/.runtime-complete"
completion_receipt="${CLAWBOX_STATE_DIR}/.upload-complete"
completion_failure="${CLAWBOX_STATE_DIR}/.upload-failed"
rm -f "${completion_request}" "${completion_receipt}" "${completion_failure}"

/usr/local/bin/artifact-uploader --interval 5 &
uploader_pid=$!
sidecar_pid=""
cleanup() {
  [[ -n "${sidecar_pid}" ]] && kill "${sidecar_pid}" 2>/dev/null || true
  kill "${uploader_pid}" 2>/dev/null || true
  [[ -n "${sidecar_pid}" ]] && wait "${sidecar_pid}" 2>/dev/null || true
  wait "${uploader_pid}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

/opt/clawtune/venv/bin/python -m clawtune_sidecar.main \
  --host 0.0.0.0 --port "${SIDECAR_PORT:-8765}" &
sidecar_pid=$!

while kill -0 "${sidecar_pid}" 2>/dev/null; do
  if [[ -f "${completion_request}" ]]; then
    kill -TERM "${sidecar_pid}" 2>/dev/null || true
    wait "${sidecar_pid}" 2>/dev/null || true
    sidecar_pid=""
    kill "${uploader_pid}" 2>/dev/null || true
    wait "${uploader_pid}" 2>/dev/null || true
    uploader_pid=""
    if /usr/local/bin/artifact-uploader --once --require-result; then
      touch "${completion_receipt}"
      exit 0
    fi
    touch "${completion_failure}"
    exit 1
  fi
  sleep 0.5
done

wait "${sidecar_pid}"
