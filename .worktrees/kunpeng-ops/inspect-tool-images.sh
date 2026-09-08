#!/usr/bin/env bash
set -euo pipefail

images=(
  '127.0.0.1:5000/clawbox/tool-cube-arm64@sha256:f0d65d4474aa97ca471062771bda8b0bdffa831e34b8536a522d56d4e0b7cc10'
  '127.0.0.1:5000/clawbox/tool-cube-arm64@sha256:0d32237b0e130820b4cab3c881d3656276ce7612874b7f421d500e8a83b84d3e'
)
for image in "${images[@]}"; do
  echo "IMAGE: $image"
  docker inspect --format '{{json .Config.Entrypoint}} {{json .Config.Cmd}}' "$image"
  docker run --rm --entrypoint /bin/ls "$image" -l /dev/fd || true
  docker run --rm --entrypoint /usr/bin/strings "$image" /usr/local/bin/tool-bridge \
    | grep -E 'dd bs=1 count=1|CLAWBOX_GATE_RELEASE|login shell gate readiness' \
    | head -5 || true
done
