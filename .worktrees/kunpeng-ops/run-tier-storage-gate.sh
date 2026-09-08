#!/usr/bin/env bash
set -euo pipefail
cd /home/weitianc/ClawBox-experiment-8bc19e6
set -a
source /home/weitianc/.config/clawbox/machine.env
set +a
.venv/bin/python scripts/validate-tiered-storage.py \
  --template tpl-f623b38f249f485cafc91478 --node 193.124.7.2 \
  --warm /data/cubelet/clawbox-tiered-20260907/warm \
  --cold /data/cubelet/clawbox-tiered-20260907/cold \
  --helper-image 127.0.0.1:5000/clawbox/tool-cube-arm64:final-ready-gate-20260907 \
  --require-local-numa 0 \
  --ready-timeout 180 \
  --output "/home/weitianc/clawbox-tiered-study-20260907/gates/${1:-tier-storage-private-copy-v2}.json"
