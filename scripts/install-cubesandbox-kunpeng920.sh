#!/usr/bin/env bash
set -euo pipefail

CUBE_TAG=v0.7.0
CUBE_COMMIT=d0081641c59822e4e5653b7462e914410b81910a
NAMESPACE=${CUBE_NAMESPACE:-cube-system}
RELEASE=${CUBE_RELEASE:-cube}
SOURCE_DIR=${CUBE_SOURCE_DIR:-$HOME/.cache/clawbox/CubeSandbox-v0.7.0}
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUNTIME_VALUES="$ROOT_DIR/deploy/cubesandbox/runtime-values-kunpeng920.yaml"
SEMANTIC_ENDPOINT_PATCH="$ROOT_DIR/deploy/cubesandbox/semantic-tcp-endpoint-v0.7.0.patch"
CUBE_API_IMAGE_REPOSITORY=${CUBE_API_IMAGE_REPOSITORY:-127.0.0.1:5000/clawbox/cube-api}
CUBE_API_IMAGE_TAG=${CUBE_API_IMAGE_TAG:-v0.7.0-clawbox-endpoint-v1}
REGISTRY_CONTAINER=${CLAWBOX_REGISTRY_CONTAINER:-clawbox-registry}
REGISTRY_GUEST_PORT=${CLAWBOX_REGISTRY_GUEST_PORT:-5001}

usage() {
  echo "usage: $0 {check|prepare|install|status|uninstall}"
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
  require docker
  require python3
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

  # Rebuild the deterministic patch from the pristine pin on every run.  In
  # particular, never replace the chart copy with CubeMaster/conf.yaml: the
  # chart file contains Helm templates for the Kubernetes MySQL/Redis hosts.
  git -C "$SOURCE_DIR" restore --source=HEAD -- \
    CubeAPI/src/cubemaster/mod.rs \
    CubeAPI/src/handlers/sandboxes.rs \
    CubeAPI/src/models/mod.rs \
    CubeAPI/src/openapi.rs \
    CubeAPI/src/routes.rs \
    CubeAPI/src/services/sandboxes.rs \
    CubeMaster/conf.yaml \
    Cubelet/dynamicconf/conf.yaml \
    deploy/kubernetes/chart/files/cube-master/conf.yaml \
    sdk/python/cubesandbox/__init__.py \
    sdk/python/cubesandbox/_models.py \
    sdk/python/cubesandbox/sandbox.py

  # ClawBox needs a semantic raw-TCP lookup; upstream's HTTP virtual-host API
  # cannot carry native SSH. Keep the exact CubeAPI and SDK delta in this repo.
  git -C "$SOURCE_DIR" apply --check "$SEMANTIC_ENDPOINT_PATCH"
  git -C "$SOURCE_DIR" apply "$SEMANTIC_ENDPOINT_PATCH"

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

# The chart has a separate, Helm-templated CubeMaster config.  Add the fixed
# research ratio there without disturbing its service and secret templates.
chart = root / "deploy/kubernetes/chart/files/cube-master/conf.yaml"
text = chart.read_text()
anchor = "  local_metric_update_timeout: 300s\n"
addition = (
    anchor
    + "  overcommit_ratio:\n"
    + "    cpu_ratio: 1.0\n"
    + "    mem_ratio: 1.0\n"
)
if addition not in text:
    if anchor not in text:
        raise SystemExit(f"pin patch target changed in {chart}")
    chart.write_text(text.replace(anchor, addition, 1))

rendered = chart.read_text()
required_templates = ('include "cube.dbHost"', 'include "cube.redisNodes"')
if not all(token in rendered for token in required_templates):
    raise SystemExit("refusing chart patch: Kubernetes database templates were lost")

# The pinned chart does not expose the host S3lvol socket as a value, but the
# Cubelet must reach the host service to initialize CubeCoW root disks. Keep
# this as a deterministic source patch so a later reinstall reproduces the
# live fix instead of silently reverting to a READY-but-unusable node.
helpers = root / "deploy/kubernetes/chart/templates/_helpers.tpl"
text = helpers.read_text()
mount_anchor = "- name: run-vc\n  mountPath: /run/vc\n"
mount_addition = mount_anchor + "- name: s3lvol-socket\n  mountPath: /var/run/s3lvol.sock\n"
if mount_addition not in text:
    if mount_anchor not in text:
        raise SystemExit("pin patch target changed in node volume mounts")
    text = text.replace(mount_anchor, mount_addition, 1)
helpers.write_text(text)

node_template = root / "deploy/kubernetes/chart/templates/node-daemonset.yaml"
text = node_template.read_text()
volume_anchor = "        - name: run-vc\n          hostPath:\n            path: {{ .Values.hostPaths.runVc }}\n            type: DirectoryOrCreate\n"
volume_addition = volume_anchor + "        - name: s3lvol-socket\n          hostPath:\n            path: /var/run/s3lvol.sock\n            type: Socket\n"
if volume_addition not in text:
    if volume_anchor not in text:
        raise SystemExit("pin patch target changed in node volumes")
    node_template.write_text(text.replace(volume_anchor, volume_addition, 1))
PY
}

install_registry() {
  require socat
  if ! docker inspect "$REGISTRY_CONTAINER" >/dev/null 2>&1; then
    docker run -d --restart=always --name "$REGISTRY_CONTAINER" \
      -p 127.0.0.1:5000:5000 registry:2 >/dev/null
  fi
  docker start "$REGISTRY_CONTAINER" >/dev/null
  curl --noproxy '*' -fsS http://127.0.0.1:5000/v2/ >/dev/null

  local bridge_ip
  bridge_ip=${CLAWBOX_REGISTRY_BRIDGE_IP:-$(docker network inspect bridge \
    --format '{{(index .IPAM.Config 0).Gateway}}')}
  [[ "$bridge_ip" =~ ^[0-9a-fA-F:.]+$ ]] || {
    echo "invalid Docker bridge address: $bridge_ip" >&2; exit 1;
  }
  sudo tee /etc/systemd/system/clawbox-registry-mirror.service >/dev/null <<EOF
[Unit]
Description=Expose the loopback research registry to CubeSandbox guests
After=docker.service network-online.target
Requires=docker.service

[Service]
ExecStart=/usr/bin/socat TCP-LISTEN:${REGISTRY_GUEST_PORT},bind=${bridge_ip},fork,reuseaddr TCP:127.0.0.1:5000
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now clawbox-registry-mirror.service
  curl --noproxy '*' -fsS "http://${bridge_ip}:${REGISTRY_GUEST_PORT}/v2/" >/dev/null
  printf 'registry_loopback=http://127.0.0.1:5000 registry_guest=http://%s:%s\n' \
    "$bridge_ip" "$REGISTRY_GUEST_PORT"
}

build_cube_api() {
  local image="${CUBE_API_IMAGE_REPOSITORY}:${CUBE_API_IMAGE_TAG}"
  docker build --network=host \
    --build-arg CUBE_VERSION="${CUBE_TAG}-clawbox" \
    --build-arg CUBE_COMMIT="${CUBE_COMMIT}+semantic-endpoint-v1" \
    -t "$image" "$SOURCE_DIR/CubeAPI"
  docker push "$image"
  docker image inspect "$image" --format '{{index .RepoDigests 0}}'
}

prepare() {
  check
  prepare_source
  install_registry
  build_cube_api
  python3 -m pip install --user -e "$SOURCE_DIR/sdk/python"
}

configure_cluster_dns() {
  # CubeAPI returns per-sandbox hosts such as <id>.cube.local.  Route the
  # wildcard suffix to cube-proxy through cluster DNS.  `answer auto` is
  # required so DNS replies retain the name originally queried by the client.
  python3 - "$NAMESPACE" "$RELEASE" <<'PY'
import json
import re
import subprocess
import sys

namespace, release = sys.argv[1:]
begin = f"# BEGIN cube-sandbox-dns {release}"
end = f"# END cube-sandbox-dns {release}"
proxy = f"{release}-proxy.{namespace}.svc.cluster.local."
block = f"""{begin}
cube.local:53 {{
    errors
    cache 60
    rewrite stop name exact cube.local. {proxy} answer auto
    rewrite stop name regex (.*)[.]cube[.]local[.]? {proxy} answer auto
    kubernetes cluster.local
    forward . /etc/resolv.conf
}}
{end}"""
raw = subprocess.check_output([
    "kubectl", "-n", "kube-system", "get", "configmap", "coredns", "-o", "json"
])
value = json.loads(raw)
corefile = value["data"]["Corefile"]
corefile = re.sub(re.escape(begin) + r".*?" + re.escape(end), "", corefile,
                  flags=re.DOTALL).rstrip() + "\n\n" + block + "\n"
value["data"]["Corefile"] = corefile
subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(value).encode(), check=True)
subprocess.run(["kubectl", "-n", "kube-system", "rollout", "restart", "deployment/coredns"], check=True)
subprocess.run(["kubectl", "-n", "kube-system", "rollout", "status", "deployment/coredns",
                "--timeout=120s"], check=True)
PY
}

install_cube() {
  prepare
  [[ ${CUBE_MYSQL_PASSWORD:-} ]] || { echo "set CUBE_MYSQL_PASSWORD" >&2; exit 1; }
  [[ ${CUBE_MYSQL_ROOT_PASSWORD:-} ]] || { echo "set CUBE_MYSQL_ROOT_PASSWORD" >&2; exit 1; }
  [[ ${CUBE_REDIS_PASSWORD:-} ]] || { echo "set CUBE_REDIS_PASSWORD" >&2; exit 1; }
  local node ip
  node=$(node_name)
  ip=${CUBE_NODE_IP:-$(kubectl get node "$node" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')}
  kubectl label node "$node" cube.tencent.com/cube-control=true cube.tencent.com/cube-node=true --overwrite
  local mirror_args=()
  if [[ ${CUBE_USE_CN_MIRROR:-0} == 1 ]]; then
    mirror_args=(-f "$SOURCE_DIR/deploy/kubernetes/chart/values-cn.yaml")
  fi
  local helm_args=(upgrade --install "$RELEASE" "$SOURCE_DIR/deploy/kubernetes/chart"
    --namespace "$NAMESPACE" --create-namespace \
    -f "$SOURCE_DIR/deploy/kubernetes/chart/values-single-node.yaml" \
    "${mirror_args[@]}" \
    -f "$RUNTIME_VALUES" \
    --set-string mysql.password="$CUBE_MYSQL_PASSWORD" \
    --set-string mysql.rootPassword="$CUBE_MYSQL_ROOT_PASSWORD" \
    --set-string redis.password="$CUBE_REDIS_PASSWORD" \
    --set-string cubeProxy.advertiseIP="$ip" \
    --set-string images.api.repository="$CUBE_API_IMAGE_REPOSITORY" \
    --set-string images.api.tag="$CUBE_API_IMAGE_TAG" \
    --set-string images.api.pullPolicy=IfNotPresent)

  # A fresh chart needs MinIO and its generated secret before the host S3lvol
  # service can be configured. Bring up control-plane services first, install
  # S3lvol, then enable cube-node with its Socket hostPath.
  if ! helm -n "$NAMESPACE" status "$RELEASE" >/dev/null 2>&1; then
    helm "${helm_args[@]}" --set cubeNode.enabled=false --wait --timeout 20m
  fi
  if ! systemctl is-active --quiet cube-sandbox-s3lvol.service \
     || [[ ! -S /var/run/s3lvol.sock ]]; then
    CUBE_NAMESPACE="$NAMESPACE" CUBE_NODE_NAME="$node" \
      bash "$ROOT_DIR/scripts/recover-cubesandbox-s3lvol-kunpeng920.sh"
  fi
  helm "${helm_args[@]}" --wait --timeout 20m
  configure_cluster_dns
  status
}

status() {
  kubectl get nodes -L cube.tencent.com/cube-control,cube.tencent.com/cube-node
  kubectl -n "$NAMESPACE" get pods,svc
  helm -n "$NAMESPACE" status "$RELEASE"
  systemctl is-active cube-sandbox-s3lvol.service
  systemctl is-active clawbox-registry-mirror.service
  curl --noproxy '*' -fsS http://127.0.0.1:5000/v2/ >/dev/null
  curl --noproxy '*' -fsS http://127.0.0.1:30030/health >/dev/null
  echo "Kunpeng CubeSandbox services are healthy"
}

uninstall_cube() {
  # DNS cleanup is deliberately conservative: remove only this release's
  # marker-delimited block and leave every other CoreDNS setting untouched.
  python3 - "$RELEASE" <<'PY'
import json, re, subprocess, sys
release = sys.argv[1]
begin, end = f"# BEGIN cube-sandbox-dns {release}", f"# END cube-sandbox-dns {release}"
raw = subprocess.check_output(["kubectl", "-n", "kube-system", "get", "configmap", "coredns", "-o", "json"])
value = json.loads(raw)
value["data"]["Corefile"] = re.sub(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", "",
                                      value["data"]["Corefile"], flags=re.DOTALL)
subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(value).encode(), check=True)
subprocess.run(["kubectl", "-n", "kube-system", "rollout", "restart", "deployment/coredns"], check=True)
PY
  helm -n "$NAMESPACE" uninstall "$RELEASE"
  kubectl delete namespace "$NAMESPACE" --ignore-not-found
  echo "Persistent data and /data/cubelet were intentionally retained."
}

case ${1:-} in
  check) check ;;
  prepare) prepare ;;
  install) install_cube ;;
  status) status ;;
  uninstall) uninstall_cube ;;
  *) usage; exit 2 ;;
esac
