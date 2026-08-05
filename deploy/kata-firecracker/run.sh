#!/usr/bin/env bash
# run.sh — launch the Indexed Job that simulates multiple concurrent users,
# one per Firecracker microVM.
#
# Usage:
#   IMAGE=registry.local/openclaw-runner:latest \
#   PARALLELISM=4 COMPLETIONS=4 \
#   TASK_MESSAGE="Use the shell to print claw-cloud-ok" \
#   OPENAI_BASE_URL=http://<llm-host>:8000/v1 \
#   OPENAI_API_KEY=<key> \
#   OPENCLAW_MODEL=<model> \
#   ./deploy/kata-firecracker/run.sh
#
# Steps: check env -> apply RuntimeClass -> create Secret -> render+apply Job
# -> wait for Pods -> show status -> dump all Pod logs when done. The Job is
# intentionally NOT deleted so you can inspect it.
set -euo pipefail

cd "$(dirname "$0")"

# ── 1. Required inputs ────────────────────────────────────────────────
IMAGE="${IMAGE:?set IMAGE=registry.local/openclaw-runner:latest}"
OPENCLAW_MODEL="${OPENCLAW_MODEL:?set OPENCLAW_MODEL=<model>}"
TASK_MESSAGE="${TASK_MESSAGE:?set TASK_MESSAGE=\"task prompt\"}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:?set OPENAI_BASE_URL=http://<llm-host>:8000/v1}"
OPENAI_API_KEY="${OPENAI_API_KEY:?set OPENAI_API_KEY=<key>}"

JOB_NAME="${JOB_NAME:-claw-runner}"
PARALLELISM="${PARALLELISM:-4}"
COMPLETIONS="${COMPLETIONS:-4}"
NAMESPACE="${NAMESPACE:-default}"
OPENCLAW_TIMEOUT="${OPENCLAW_TIMEOUT:-300}"
SECRET_NAME="${SECRET_NAME:-claw-llm}"

command -v kubectl >/dev/null 2>&1 || { echo "kubectl not found" >&2; exit 1; }
command -v envsubst >/dev/null 2>&1 || { echo "envsubst not found (install gettext/gettext-base)" >&2; exit 1; }

echo "==> Applying RuntimeClass kata-fc"
kubectl -n "${NAMESPACE}" apply -f runtimeclass.yaml

echo "==> Creating/updating Secret ${SECRET_NAME}"
# Write secret values to a temp dir and use --from-file so the API key never
# appears in argv / shell history; then render+apply (idempotent).
tmpdir="$(mktemp -d)"
cleanup_tmp() { rm -rf "${tmpdir}"; }
trap cleanup_tmp EXIT
printf '%s' "${OPENAI_BASE_URL}" > "${tmpdir}/openai-base-url"
printf '%s' "${OPENAI_API_KEY}" > "${tmpdir}/openai-api-key"
printf '%s' "${TASK_MESSAGE}" > "${tmpdir}/task-message"
kubectl -n "${NAMESPACE}" create secret generic "${SECRET_NAME}" \
  --from-file=openai-base-url="${tmpdir}/openai-base-url" \
  --from-file=openai-api-key="${tmpdir}/openai-api-key" \
  --from-file=task-message="${tmpdir}/task-message" \
  --dry-run=client -o yaml | kubectl -n "${NAMESPACE}" apply -f -
rm -rf "${tmpdir}"

echo "==> Rendering and applying Job ${JOB_NAME}"
export JOB_NAME IMAGE PARALLELISM COMPLETIONS OPENCLAW_MODEL OPENCLAW_TIMEOUT
envsubst < job.yaml | kubectl -n "${NAMESPACE}" apply -f -

echo "==> Waiting for Pods to be created (runtimeClassName kata-fc)"
# A completing container never reports Ready, so poll until every pod exists.
pods_up=0
for _ in $(seq 1 60); do
  pods_up="$(kubectl -n "${NAMESPACE}" get pods -l "job-name=${JOB_NAME}" \
    --no-headers 2>/dev/null | wc -l | tr -d ' ')"
  [ "${pods_up}" -ge "${COMPLETIONS}" ] && break
  sleep 5
done
kubectl -n "${NAMESPACE}" get pods -l "job-name=${JOB_NAME}" -o wide
echo "==> Pods created: ${pods_up} / ${COMPLETIONS}"

echo "==> Waiting for the Job to complete"
kubectl -n "${NAMESPACE}" wait --for=condition=complete \
  --timeout=900s "job/${JOB_NAME}" || \
kubectl -n "${NAMESPACE}" wait --for=condition=failed \
  --timeout=120s "job/${JOB_NAME}" || true

echo "==> Final Pod state"
kubectl -n "${NAMESPACE}" get pods -l "job-name=${JOB_NAME}" -o wide

echo "==> Pod logs"
for pod in $(kubectl -n "${NAMESPACE}" get pods -l "job-name=${JOB_NAME}" -o name 2>/dev/null); do
  echo "----- ${pod} -----"
  kubectl -n "${NAMESPACE}" logs "${pod}" || true
done

echo
echo "Job ${JOB_NAME} left in place for debugging."
echo "Inspect: kubectl get pods -l job-name=${JOB_NAME} -o wide"
echo "Cleanup: kubectl delete job ${JOB_NAME}"
