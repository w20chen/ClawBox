#!/usr/bin/env bash
set -euo pipefail

CUBE_TAG=v0.7.0
CUBE_COMMIT=d0081641c59822e4e5653b7462e914410b81910a
NAMESPACE=${CUBE_NAMESPACE:-cube-system}
RELEASE=${CUBE_RELEASE:-cube}
SOURCE_DIR=${CUBE_SOURCE_DIR:-$HOME/CubeSandbox}
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUNTIME_VALUES="$ROOT_DIR/deploy/cubesandbox/runtime-values-kunpeng920.yaml"

usage() {
  echo "usage: $0 {check|install|status|uninstall}"
}

require() {
  command -v "$1" >/dev/null || { echo "missing command: $1" >&2; exit 1; }
}

node_name() {
  if [[ -n ${CUBE_NODE_NAME:-} ]]; then printf '%s\n' "$CUBE_NODE_NAME"; else kubectl get nodes -o jsonpath='{.items[0].metadata.name}'; fi
}

check() {
  require git
  require helm
  require kubectl
  [[ $(uname -m) == aarch64 ]] || { echo "host must be aarch64" >&2; exit 1; }
  [[ -c /dev/kvm ]] || { echo "/dev/kvm is missing" >&2; exit 1; }
  [[ $(stat -fc %T /sys/fs/cgroup) == cgroup2fs ]] || { echo "cgroup v2 is required" >&2; exit 1; }
  kubectl cluster-info >/dev/null
  local service_cidr
  service_cidr=$(kubectl cluster-info dump 2>/dev/null | sed -n 's/.*--service-cluster-ip-range=\([^ "\\]*\).*/\1/p' | head -n1)
  echo "node=$(node_name) service_cidr=${service_cidr:-unknown} sandbox_cidr=172.16.0.0/18"
  echo "Verify those CIDRs do not overlap before install. The chart also enforces this check."
  if mountpoint -q /data/cubelet; then
    [[ $(stat -fc %T /data/cubelet) == xfs ]] || { echo "/data/cubelet is mounted but is not XFS" >&2; exit 1; }
    xfs_info /data/cubelet | grep -q 'reflink=1' || { echo "/data/cubelet XFS lacks reflink=1" >&2; exit 1; }
  else
    local free_kib
    free_kib=$(df --output=avail /data | tail -n1)
    (( free_kib >= 210 * 1024 * 1024 )) || { echo "need at least 210 GiB free for the 200G loopback image" >&2; exit 1; }
  fi
  echo "host checks passed"
}

prepare_source() {
  if [[ ! -d "$SOURCE_DIR/.git" ]]; then
    git clone --branch "$CUBE_TAG" --depth 1 https://github.com/TencentCloud/CubeSandbox.git "$SOURCE_DIR"
  fi
  git -C "$SOURCE_DIR" fetch --depth 1 origin "refs/tags/$CUBE_TAG:refs/tags/$CUBE_TAG"
  git -C "$SOURCE_DIR" checkout --detach "$CUBE_TAG"
  [[ $(git -C "$SOURCE_DIR" rev-parse HEAD) == "$CUBE_COMMIT" ]] || { echo "unexpected CubeSandbox commit" >&2; exit 1; }

  # v0.7.0 does not expose these research controls as Helm values. Patch the
  # pinned chart inputs deterministically before rendering it.
  python3 - "$SOURCE_DIR" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
changes = {
    root / "Cubelet/dynamicconf/conf.yaml": (
        "paused_resource_release_ratio: 0.0",
        "paused_resource_release_ratio: 1.0",
    ),
    root / "CubeMaster/conf.yaml": (
        "cpu_ratio: 3.0\n    mem_ratio: 2.0",
        "cpu_ratio: 1.0\n    mem_ratio: 1.0",
    ),
}
for path, (old, new) in changes.items():
    text = path.read_text()
    if new in text:
        continue
    if old not in text:
        raise SystemExit(f"pin patch target changed in {path}")
    path.write_text(text.replace(old, new, 1))

# The Helm chart embeds the master config from its files directory.
source = root / "CubeMaster/conf.yaml"
target = root / "deploy/kubernetes/chart/files/cube-master/conf.yaml"
target.write_text(source.read_text())
PY
}

install_cube() {
  check
  [[ ${CUBE_MYSQL_PASSWORD:-} ]] || { echo "set CUBE_MYSQL_PASSWORD" >&2; exit 1; }
  [[ ${CUBE_MYSQL_ROOT_PASSWORD:-} ]] || { echo "set CUBE_MYSQL_ROOT_PASSWORD" >&2; exit 1; }
  [[ ${CUBE_REDIS_PASSWORD:-} ]] || { echo "set CUBE_REDIS_PASSWORD" >&2; exit 1; }
  local node ip
  node=$(node_name)
  ip=${CUBE_NODE_IP:-$(kubectl get node "$node" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')}
  prepare_source
  kubectl label node "$node" cube.tencent.com/cube-control=true cube.tencent.com/cube-node=true --overwrite
  local mirror_args=()
  if [[ ${CUBE_USE_CN_MIRROR:-0} == 1 ]]; then
    mirror_args=(-f "$SOURCE_DIR/deploy/kubernetes/chart/values-cn.yaml")
  fi
  helm upgrade --install "$RELEASE" "$SOURCE_DIR/deploy/kubernetes/chart" \
    --namespace "$NAMESPACE" --create-namespace \
    -f "$SOURCE_DIR/deploy/kubernetes/chart/values-single-node.yaml" \
    "${mirror_args[@]}" \
    -f "$RUNTIME_VALUES" \
    --set-string mysql.password="$CUBE_MYSQL_PASSWORD" \
    --set-string mysql.rootPassword="$CUBE_MYSQL_ROOT_PASSWORD" \
    --set-string redis.password="$CUBE_REDIS_PASSWORD" \
    --set-string cubeProxy.advertiseIP="$ip" \
    --wait --timeout 20m
  status
}

status() {
  kubectl get nodes -L cube.tencent.com/cube-control,cube.tencent.com/cube-node
  kubectl -n "$NAMESPACE" get pods,svc
  helm -n "$NAMESPACE" status "$RELEASE"
}

uninstall_cube() {
  helm -n "$NAMESPACE" uninstall "$RELEASE"
  kubectl delete namespace "$NAMESPACE" --ignore-not-found
  echo "Persistent data and /data/cubelet were intentionally retained."
}

case ${1:-} in
  check) check ;;
  install) install_cube ;;
  status) status ;;
  uninstall) uninstall_cube ;;
  *) usage; exit 2 ;;
esac
