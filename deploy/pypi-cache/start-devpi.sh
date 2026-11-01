#!/bin/sh
set -eu

if [ ! -f /data/.nodeinfo ]; then
    devpi-init --serverdir /data
fi

exec devpi-server \
    --serverdir /data \
    --host "${CLAWBOX_PYPI_BIND_ADDRESS:-0.0.0.0}" \
    --port "${CLAWBOX_PYPI_PORT:-3141}" \
    --threads 64
