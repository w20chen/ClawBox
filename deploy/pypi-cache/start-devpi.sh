#!/bin/sh
set -eu

if [ ! -f /data/.nodeinfo ]; then
    devpi-init --serverdir /data
fi

exec devpi-server \
    --serverdir /data \
    --host 0.0.0.0 \
    --port 3141 \
    --threads 64
