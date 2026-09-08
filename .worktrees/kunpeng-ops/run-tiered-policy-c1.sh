#!/usr/bin/env bash
set -euo pipefail
cd /home/weitianc/ClawBox-experiment-1b427db
set -a
source /home/weitianc/.config/clawbox/machine.env
set +a
export CUBE_SOURCE_DIR=/home/weitianc/CubeSandbox-standalone-20260907
export CLAWBOX_OUTPUT_ROOT=/home/weitianc/clawbox-tiered-study-20260907/raw-standalone
export PYTHONUNBUFFERED=1
.venv/bin/python -m clawbox.experiments.worker \
  --spec /home/weitianc/clawbox-tiered-study-20260907/gates/tiered-policy-c1-v1.yaml \
  --run-id gate-tiered-policy-c1-v1 --attempt-id gate-tiered-policy-c1-v1 \
  --task-uid gate-tiered-policy-c1-v1
