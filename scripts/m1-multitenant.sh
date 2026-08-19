#!/usr/bin/env bash
# Multi-tenant submission simulator against the real M1 smoke stack.
#
# Submits K tenants x N real runs through the Managed API and watches each
# tenant's Run phase until terminal.  Every run becomes its own SandboxTask
# CR -> dual-VM Cell, so K*N concurrent Cells will be created on the host.
#
# Env overrides: API, TOKEN, TENANTS, RUNS_PER_TENANT, DEADLINE_SECONDS,
# INPUT_REF, TEMPLATE.
set -euo pipefail

API="${API:-http://127.0.0.1:8085}"
TOKEN="${TOKEN:-clawbox-m1-smoke-token-0001}"
TENANTS="${TENANTS:-tenant-a tenant-b tenant-c}"
RUNS_PER_TENANT="${RUNS_PER_TENANT:-1}"
DEADLINE_SECONDS="${DEADLINE_SECONDS:-300}"
INPUT_REF="${INPUT_REF:-15five__scim2-filter-parser-13}"
TEMPLATE="${TEMPLATE:-swe-rebench-arm64}"
NS="${NS:-clawbox-benchmarks}"
WATCH_TIMEOUT="${WATCH_TIMEOUT:-1800}"

echo "== submit ${TENANTS} x ${RUNS_PER_TENANT} runs via ${API} =="
clawbox-multitenant \
  --api-url "${API}" \
  --token "${TOKEN}" \
  --tenants ${TENANTS} \
  --runs-per-tenant "${RUNS_PER_TENANT}" \
  --template "${TEMPLATE}" \
  --input-ref "${INPUT_REF}" \
  --deadline-seconds "${DEADLINE_SECONDS}" \
  --watch --watch-timeout "${WATCH_TIMEOUT}" --poll-seconds 15

echo
echo "== per-tenant SandboxTask CRs =="
kubectl get sandboxtask -n "${NS}" -o wide \
  | grep -E "run-|NAME" \
  | awk '{print $1, $2, $3}' | sort | tail -40
