#!/usr/bin/env bash
# Verify the rebuilt control-plane image carries the M1 managed components.
set -euo pipefail
IMG="127.0.0.1:5000/clawbox/control-plane-arm64:dev"
echo "== entry points =="
docker run --rm --entrypoint sh "${IMG}" -lc 'for c in clawbox-managed-api clawbox-managed-dispatcher clawbox-managed-benchmark clawbox-cell-controller clawbox-trace-ingester; do command -v "$c" >/dev/null && echo "OK  $c" || echo "MISS $c"; done'
echo "== python imports =="
docker run --rm --entrypoint python3 "${IMG}" -c 'import clawbox.managed, clawbox.api, clawbox.cell, alembic; print("imports ok")'
echo "== alembic available =="
docker run --rm --entrypoint sh "${IMG}" -lc 'python3 -m alembic --version'
