#!/usr/bin/env bash
set -euo pipefail

service_dir=${1:-/usr/local/services/cubetoolbox/cubeproxy}
compose_file="$service_dir/docker-compose.yaml"
compose_template="$service_dir/docker-compose.yaml.template"
nginx_file="$service_dir/nginx.conf"
nginx_template="$service_dir/nginx.conf.template"

test -f "$compose_file"
test -f "$compose_template"
test -f "$nginx_file"
test -f "$nginx_template"

# A fixed worker count avoids one worker per host CPU on large research hosts.
sed -i 's/^worker_processes auto;$/worker_processes 12;/' "$nginx_file" "$nginx_template"

# Compose v1 otherwise inherits Docker's small 1024-fd default.
for candidate in "$compose_file" "$compose_template"; do
  if ! grep -q '^    ulimits:$' "$candidate"; then
    sed -i '/^    restart: unless-stopped$/a\    ulimits:\n      nofile:\n        soft: 65535\n        hard: 65535' "$candidate"
  fi
done

if [[ "$service_dir" == /usr/local/services/cubetoolbox/cubeproxy ]]; then
  systemctl restart cube-sandbox-cube-proxy.service
  systemctl is-active cube-sandbox-cube-proxy.service
  docker inspect cube-proxy --format '{{json .HostConfig.Ulimits}}'
  docker exec cube-proxy sh -c 'grep -E "^worker_processes" /usr/local/openresty/nginx/conf/nginx.conf; ulimit -n'
fi
