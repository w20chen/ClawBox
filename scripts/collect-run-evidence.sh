#!/usr/bin/env bash
# CBX-M0-006: collect + verify re-verifiable evidence for a completed run.
#
# usage: collect-run-evidence.sh RUN_ID [CLUSTER] [RELEASE]
#   RUN_ID     the SandboxTask name (e.g. m0r1787043542-001)
#   CLUSTER    evidence cluster dir (default $(hostname))
#   RELEASE    evidence release dir (default m0-YYYY-MM-DD)
#
# Wraps scripts/evidence-manifest.py with the validated image set so every run
# produces the same, re-verifiable evidence layout:
#   release-evidence/<RELEASE>/<CLUSTER>/<RUN_ID>/manifest.json
set -euo pipefail

RUN="${1:?usage: collect-run-evidence.sh RUN_ID [CLUSTER] [RELEASE]}"
CLUSTER="${2:-$(hostname)}"
RELEASE="${3:-m0-$(date +%Y-%m-%d)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

TASK_IMAGE="127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:bdf4637498a4b765f0e91333ff292c226bd011a31c220d76609daff43e2c39fd"

python3 scripts/evidence-manifest.py collect \
  --release "${RELEASE}" --cluster "${CLUSTER}" --run "${RUN}" \
  --result-task "${RUN}" \
  --image 127.0.0.1:5000/clawbox/control-plane-arm64:dev \
  --image 127.0.0.1:5000/clawbox/runtime-arm64:dev \
  --image 127.0.0.1:5000/clawbox/tool-bridge-arm64:dev \
  --image "${TASK_IMAGE}"

python3 scripts/evidence-manifest.py verify \
  --manifest "release-evidence/${RELEASE}/${CLUSTER}/${RUN}/manifest.json"

echo "evidence: release-evidence/${RELEASE}/${CLUSTER}/${RUN}/manifest.json"
