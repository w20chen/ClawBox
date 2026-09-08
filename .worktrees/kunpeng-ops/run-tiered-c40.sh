#!/usr/bin/env bash
set -euo pipefail
cd /home/weitianc/ClawBox-experiment-b65b007
set -a
source /home/weitianc/.config/clawbox/machine.env
set +a
export CUBE_SOURCE_DIR=/home/weitianc/CubeSandbox-standalone-20260907
export CLAWBOX_OUTPUT_ROOT=/home/weitianc/clawbox-tiered-study-20260907/raw-standalone
export PYTHONUNBUFFERED=1
exec .venv/bin/python -m clawbox.experiments.worker \
  --spec examples/experiments/tiered-oracle-rec-a-c40.yaml \
  --run-id tiered-c40-20260908-v1 --attempt-id tiered-c40-20260908-v1 \
  --task-uid tiered-c40-20260908-v1
