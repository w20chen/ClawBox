#!/usr/bin/env bash
set -eu
image=127.0.0.1:5000/clawbox/cubelet:tiered-fdc1a8c
docker run --rm --entrypoint /bin/bash "$image" -lc "find /usr/local -type f | grep -E 'cubevs|cubelet$|network/bin' | head -100"
