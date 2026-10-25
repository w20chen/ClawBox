#!/usr/bin/env bash
# Restore the Cubelet/egress ordering needed by the standalone experiment host.
set -Eeuo pipefail

(( EUID == 0 )) || { echo 'run as root' >&2; exit 1; }
timeout_seconds=${1:-1200}
[[ $timeout_seconds =~ ^[1-9][0-9]*$ ]] || {
  echo 'usage: sudo scripts/recover-cubesandbox-after-reboot.sh [TIMEOUT_SECONDS]' >&2
  exit 64
}

# Starting the aggregate target can expose an ordering cycle between Cubelet,
# cube-egress, and the host-rule helper.  Starting this dependency chain in its
# real runtime order is safe to repeat and does not restart active services.
systemctl start cube-sandbox-cubelet.service
systemctl start cube-sandbox-cube-egress.service

deadline=$((SECONDS + timeout_seconds))
while [[ ! -e /sys/fs/cgroup/cube_sandbox/cubelet/cgroup.procs ]]; do
  (( SECONDS < deadline )) || {
    echo 'Cubelet is active but its delegated cgroup did not appear; inspect cube-sandbox-cubelet.service' >&2
    exit 1
  }
  sleep 1
done

systemctl start cube-sandbox-cube-egress-net.service
for service in cubelet cube-egress cube-egress-net; do
  systemctl is-active --quiet "cube-sandbox-$service.service" || {
    echo "cube-sandbox-$service.service is not active" >&2
    exit 1
  }
done

storage_status=
while :; do
  storage_status=$(
    env HTTP_PROXY= HTTPS_PROXY= ALL_PROXY= NO_PROXY=127.0.0.1 \
      curl --fail --silent --show-error --max-time 5 \
      'http://127.0.0.1:8089/cube/snapshot/storage?refresh=true' 2>/dev/null || true
  )
  if grep -Eq '"data"[[:space:]]*:[[:space:]]*\[[[:space:]]*\{' <<<"$storage_status"; then
    break
  fi
  (( SECONDS < deadline )) || {
    echo 'Cubelet services are active, but no storage node heartbeat is ready; inspect cube-sandbox-cubelet.service and CubeMaster' >&2
    [[ -z $storage_status ]] || echo "last storage response: $storage_status" >&2
    exit 1
  }
  sleep 2
done

echo 'Cubelet storage heartbeat, egress proxy, host network rules, and delegated cgroup are ready.'
