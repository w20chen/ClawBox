#!/usr/bin/env bash
set -euo pipefail
cd /home/weitianc/ClawBox-experiment-full-c40-160g
set -a
source /home/weitianc/.config/clawbox/machine.env
set +a
export CUBE_SOURCE_DIR=/home/weitianc/CubeSandbox-standalone-20260907
export PYTHONUNBUFFERED=1
exec .venv/bin/python scripts/run-short-tiered-study.py \
  --spec examples/experiments/tiered-oracle-rec-a-c40.yaml \
  --full-trace --arm-seconds 3600 \
  --output /home/weitianc/clawbox-tiered-study-20260907/full-c40-160g-20260908-v1
