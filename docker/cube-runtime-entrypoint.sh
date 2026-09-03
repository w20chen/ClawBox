#!/bin/sh
set -eu

mkdir -p /workspace /var/log
/usr/bin/envd -port "${ENVD_PORT:-49983}" >/var/log/envd.log 2>&1 &
envd_pid=$!
trap 'kill "$envd_pid" 2>/dev/null || true' TERM INT EXIT
wait "$envd_pid"
