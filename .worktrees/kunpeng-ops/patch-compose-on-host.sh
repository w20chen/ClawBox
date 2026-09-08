#!/usr/bin/env bash
set -euo pipefail

target=/usr/lib/python3.11/site-packages/compose/cli/docker_client.py
backup=/usr/lib/python3.11/site-packages/compose/cli/docker_client.py.clawbox-pre-dockerpy7
test "$(id -u)" = 0
test -f "$target"
if grep -q 'kwargs_from_env(environment=environment, ssl_version=tls_version)' "$target"; then
  if test ! -e "$backup"; then
    cp -a "$target" "$backup"
  fi
  python3 - "$target" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
old = 'kwargs = kwargs_from_env(environment=environment, ssl_version=tls_version)'
new = 'kwargs = kwargs_from_env(environment=environment)'
text = path.read_text()
if text.count(old) != 1:
    raise SystemExit(f'expected exactly one compatibility call, found {text.count(old)}')
path.write_text(text.replace(old, new))
PY
fi

docker-compose version
cd /usr/local/services/cubetoolbox/support
docker-compose -f docker-compose.yaml ps >/dev/null
systemctl reset-failed 'cube-sandbox-*' || true
systemctl restart cube-sandbox-mysql.service cube-sandbox-redis.service
