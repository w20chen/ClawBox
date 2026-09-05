#!/usr/bin/env bash
# Build immutable Runtime/Tool images, register fresh Cube templates, and emit
# one sourceable manifest. This contains no host passwords or model API keys.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
MODE=${1:-plan}
REGISTRY=${CLAWBOX_REGISTRY:-127.0.0.1:5000/clawbox}
GUEST_REGISTRY=${CLAWBOX_GUEST_REGISTRY:-172.17.0.1:5001/clawbox}
KERNEL_DIGEST=${CUBE_GUEST_KERNEL_DIGEST:-f84e3fa28ae692f34645aa3c7034999242760eb25aab0ea667b43f16ac12c27f}
KERNEL_VERSION=${CUBE_GUEST_KERNEL_VERSION:-sha256-f84e3fa28ae6}
CLAWTUNE_ROOT=${CLAWTUNE_ROOT:-$ROOT/../ClawTune}
CUBE_SOURCE_DIR=${CUBE_SOURCE_DIR:-$HOME/.cache/clawbox/CubeSandbox-v0.7.0}
VENV=${CLAWBOX_VENV:-$ROOT/.venv-kunpeng}
OUTPUT=${CLAWBOX_KUNPENG_MANIFEST:-$ROOT/.artifacts/kunpeng-openclaw.env}
NODE=${CUBE_NODE_NAME:-$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)}
REVISION=${CLAWBOX_IMAGE_REVISION:-$(git -C "$ROOT" rev-parse --short=12 HEAD)}

die() { echo "$*" >&2; exit 1; }
require() { command -v "$1" >/dev/null || die "missing command: $1"; }

check() {
  for command in docker kubectl python3 git curl; do require "$command"; done
  [[ $(uname -m) =~ ^(aarch64|arm64)$ ]] || die "native ARM64 host required"
  [[ -n "$NODE" ]] || die "CUBE_NODE_NAME is unset and no Kubernetes node was found"
  [[ -d "$CLAWTUNE_ROOT/.git" ]] || die "ClawTune checkout missing: $CLAWTUNE_ROOT"
  [[ -f "$CUBE_SOURCE_DIR/sdk/python/cubesandbox/sandbox.py" ]] \
    || die "prepared CubeSandbox SDK missing; run install-cubesandbox-kunpeng920.sh prepare"
  grep -q 'def get_tcp_endpoint' "$CUBE_SOURCE_DIR/sdk/python/cubesandbox/sandbox.py" \
    || die "CubeSandbox SDK lacks the ClawBox semantic endpoint patch"
  [[ ${CLAWBOX_CONTROL_HOST:-} ]] || die "set CLAWBOX_CONTROL_HOST to the host IP reachable from Runtime VMs"
  systemctl is-active --quiet cube-sandbox-s3lvol.service || die "CubeS3lvol is not active"
  systemctl is-active --quiet clawbox-registry-mirror.service || die "registry guest mirror is not active"
  curl --noproxy '*' -fsS "${CUBE_API_URL:-http://127.0.0.1:30030}/health" >/dev/null
  curl --noproxy '*' -fsS "http://${GUEST_REGISTRY%/clawbox}/v2/" >/dev/null
  kubectl -n "${CUBE_NAMESPACE:-cube-system}" wait --for=condition=Ready \
    pod -l app.kubernetes.io/component=cube-node --timeout=120s >/dev/null
  printf 'preflight=PASS node=%s revision=%s\n' "$NODE" "$REVISION"
}

digest_ref() {
  local tagged=$1 repository=${1%:*} candidate
  while IFS= read -r candidate; do
    [[ "$candidate" == "$repository"@sha256:* ]] && { printf '%s\n' "$candidate"; return; }
  done < <(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$tagged")
  die "immutable registry digest unavailable for $tagged"
}

guest_ref() {
  local host_ref=$1 path
  path=${host_ref#*/clawbox/}
  path=${path%@*}
  printf 'http://%s/%s@%s\n' "$GUEST_REGISTRY" "$path" "${host_ref#*@}"
}

build() {
  check
  local runtime_tag="$REGISTRY/runtime-cube-arm64:$REVISION"
  local tool_tag="$REGISTRY/tool-cube-arm64:$REVISION"
  docker build --network host --build-context clawtune="$CLAWTUNE_ROOT" \
    --build-arg CLAWTUNE_REVISION="$(git -C "$CLAWTUNE_ROOT" rev-parse HEAD)" \
    --build-arg CLAWBOX_REVISION="$(git -C "$ROOT" rev-parse HEAD)" \
    -f "$ROOT/docker/Dockerfile.runtime" -t "$REGISTRY/runtime-arm64:$REVISION" "$ROOT"
  docker build --network host --build-arg CUBE_GUEST_KERNEL_DIGEST="$KERNEL_DIGEST" \
    --build-arg RUNTIME_BASE_IMAGE="$REGISTRY/runtime-arm64:$REVISION" \
    -f "$ROOT/docker/Dockerfile.runtime-cube" -t "$runtime_tag" "$ROOT"
  docker build --network host --build-arg CUBE_GUEST_KERNEL_DIGEST="$KERNEL_DIGEST" \
    --build-arg TOOL_BASE_IMAGE="${CLAWBOX_TOOL_BASE_IMAGE:-127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:c9e44ea0283ce7a6545fd942c6d85d63a7230a9b71c6dcefd1dc4fd0d883629b}" \
    -f "$ROOT/docker/Dockerfile.tool-cube" -t "$tool_tag" "$ROOT"
  docker push "$runtime_tag"
  docker push "$tool_tag"
  local runtime_ref tool_ref
  runtime_ref=$(digest_ref "$runtime_tag")
  tool_ref=$(digest_ref "$tool_tag")

  [[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
  "$VENV/bin/pip" install -e "$CUBE_SOURCE_DIR/sdk/python" >/dev/null
  "$VENV/bin/pip" install -e "$ROOT" >/dev/null
  local runtime_json tool_json runtime_template tool_template
  runtime_json=$(CUBE_API_URL="${CUBE_API_URL:-http://127.0.0.1:30030}" \
    "$VENV/bin/python" "$ROOT/scripts/register-cube-template.py" "$(guest_ref "$runtime_ref")" \
    --alias "clawbox-runtime-$REVISION" --node "$NODE" --memory-mib 2048 \
    --exposed-port 49983 --probe-port 49983 --expected-kernel-version "$KERNEL_VERSION")
  tool_json=$(CUBE_API_URL="${CUBE_API_URL:-http://127.0.0.1:30030}" \
    "$VENV/bin/python" "$ROOT/scripts/register-cube-template.py" "$(guest_ref "$tool_ref")" \
    --alias "clawbox-tool-$REVISION" --node "$NODE" --memory-mib 4096 \
    --exposed-port 49983 --exposed-port 2222 --probe-port 49983 \
    --expected-kernel-version "$KERNEL_VERSION")
  runtime_template=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["template_id"])' "$runtime_json")
  tool_template=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["template_id"])' "$tool_json")
  mkdir -p "$(dirname "$OUTPUT")"
  umask 077
  {
    printf "export CLAWBOX_RUNTIME_TEMPLATE='%s'\n" "$runtime_template"
    printf "export CLAWBOX_TOOL_TEMPLATE='%s'\n" "$tool_template"
    printf "export CLAWBOX_RUNTIME_IMAGE='%s'\n" "$runtime_ref"
    printf "export CLAWBOX_TOOL_IMAGE='%s'\n" "$tool_ref"
    printf "export CUBE_NODE_NAME='%s'\n" "$NODE"
  } >"$OUTPUT"
  echo "$runtime_json"
  echo "$tool_json"
  echo "manifest=$OUTPUT"
}

verify() {
  check
  [[ -r "$OUTPUT" ]] || die "manifest missing: run build first"
  # shellcheck disable=SC1090
  source "$OUTPUT"
  export CUBE_API_URL=${CUBE_API_URL:-http://127.0.0.1:30030}
  export CUBE_PROXY_NODE_IP=${CUBE_PROXY_NODE_IP:-127.0.0.1}
  export CUBE_PROXY_PORT_HTTP=${CUBE_PROXY_PORT_HTTP:-30080}
  "$VENV/bin/python" "$ROOT/scripts/validate-cubesandbox-tcp-endpoints.py" \
    --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
    --node "$CUBE_NODE_NAME" --control-host "$CLAWBOX_CONTROL_HOST" --count 1 \
    --output "$ROOT/.artifacts/tcp-endpoints-c1.json"
  "$VENV/bin/python" "$ROOT/scripts/smoke-cubesandbox-agent-pair.py" \
    --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
    --node "$CUBE_NODE_NAME" --control-host "$CLAWBOX_CONTROL_HOST" --policy-port 18080
  "$VENV/bin/python" "$ROOT/scripts/audit-cube-sandboxes.py" --json
}

case "$MODE" in
  plan) echo "check -> build Runtime/Tool -> publish digests -> register fresh templates -> write $OUTPUT" ;;
  check) check ;;
  build) build ;;
  verify) verify ;;
  *) echo "usage: $0 {plan|check|build|verify}" >&2; exit 2 ;;
esac
