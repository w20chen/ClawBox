#!/usr/bin/env bash
set -euo pipefail
cd /home/weitianc/ClawBox-experiment-short-c40-v2
set -a
source /home/weitianc/.config/clawbox/machine.env
set +a
export CUBE_SOURCE_DIR=/home/weitianc/CubeSandbox-standalone-20260907
export PYTHONUNBUFFERED=1
test "$(cat /sys/fs/cgroup/cube_sandbox/sandbox/memory.max)" = 68719476736
test "$(findmnt -n -o FSTYPE --target /data/cubelet/clawbox-tiered-20260907/warm)" = tmpfs
exec .venv/bin/python scripts/run-short-tiered-study.py \
  --spec examples/experiments/tiered-oracle-rec-a-c40.yaml \
  --steps 23 --arm-seconds 1800 \
  --output /home/weitianc/clawbox-tiered-study-20260907/short-c40-20260908-v3
