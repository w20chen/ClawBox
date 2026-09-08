#!/usr/bin/env bash
set -euo pipefail
cd /home/weitianc/ClawBox-experiment-five-c40
set -a
source /home/weitianc/.config/clawbox/machine.env
set +a
export CUBE_SOURCE_DIR=/home/weitianc/CubeSandbox-standalone-20260907
export PYTHONUNBUFFERED=1
exec .venv/bin/python scripts/run-short-tiered-study.py \
  --spec examples/experiments/tiered-oracle-rec-a-c40.yaml \
  --steps 5 --arm-seconds 1200 \
  --output /home/weitianc/clawbox-tiered-study-20260907/five-c40-20260908-v1
