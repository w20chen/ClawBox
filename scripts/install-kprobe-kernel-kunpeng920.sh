#!/usr/bin/env bash
set -euo pipefail

# Explicit, idempotent kernel deployment. This is intentionally invoked by an
# operator after a build or reboot; it is not a boot hook and never overwrites
# the vendor backup or re-registers an identical component.
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ARTIFACT=${1:?usage: $0 /path/to/vmlinux [cube-node]}
NODE=${2:-${CUBE_NODE_NAME:-$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')}}
EXPECTED_DIGEST=f84e3fa28ae692f34645aa3c7034999242760eb25aab0ea667b43f16ac12c27f
VENDOR_DIGEST=a63aa77e9c2db5d3eb7feb8d1b002e0165693533574bce7468d28c3e235423df
VENDOR_SUFFIX=a63aa77e
VERSION_ID=sha256-f84e3fa28ae6
NAMESPACE=${CUBE_NAMESPACE:-cube-system}
STAGE=/data/cubelet/root/.clawbox-kprobe-kernel/${VERSION_ID}

command -v kubectl >/dev/null || { echo "missing kubectl" >&2; exit 1; }
[[ -f "$ARTIFACT" ]] || { echo "kernel artifact not found: $ARTIFACT" >&2; exit 1; }
actual=$(sha256sum "$ARTIFACT" | awk '{print $1}')
[[ "$actual" == "$EXPECTED_DIGEST" ]] || {
  echo "kernel checksum mismatch: $actual != $EXPECTED_DIGEST" >&2; exit 1;
}

POD=$(kubectl -n "$NAMESPACE" get pod \
  -l app.kubernetes.io/component=cube-node-installer \
  --field-selector="spec.nodeName=$NODE" \
  -o jsonpath='{.items[0].metadata.name}')
[[ -n "$POD" ]] || { echo "cube-node-installer pod not found" >&2; exit 1; }

kubectl -n "$NAMESPACE" exec "$POD" -c cube-kernel-install -- \
  sh -c "mkdir -p '$STAGE'"
kubectl -n "$NAMESPACE" cp "$ARTIFACT" "$POD:$STAGE/vmlinux-kprobes" \
  -c cube-kernel-install
kubectl -n "$NAMESPACE" cp "$ROOT_DIR/deploy/cubesandbox/kernel-oc9-arm64-kprobes.version" \
  "$POD:$STAGE/version" -c cube-kernel-install
kubectl -n "$NAMESPACE" cp "$ROOT_DIR/deploy/cubesandbox/kernel-oc9-arm64-kprobes.version.json" \
  "$POD:$STAGE/version.json" -c cube-kernel-install

if ! changed=$(kubectl -n "$NAMESPACE" exec "$POD" -c cube-kernel-install -- \
  sh -s <<'EOF'
set -eu
EXPECTED=f84e3fa28ae692f34645aa3c7034999242760eb25aab0ea667b43f16ac12c27f
VENDOR_DIGEST=a63aa77e9c2db5d3eb7feb8d1b002e0165693533574bce7468d28c3e235423df
VENDOR_SUFFIX=a63aa77e
VERSION_ID=sha256-f84e3fa28ae6
STAGE=/data/cubelet/root/.clawbox-kprobe-kernel/$VERSION_ID
ROOT=/usr/local/services/cubetoolbox/cube-kernel-scf
COMP=/data/cubelet/root/component_versions/cube-kernel-scf/$VERSION_ID
test "$(sha256sum "$STAGE/vmlinux-kprobes" | awk '{print $1}')" = "$EXPECTED" 
test "$(sha256sum "$STAGE/vmlinux-kprobes" | awk '{print $1}')" = \
      "$(sha256sum "$ROOT/vmlinux-bm" | awk '{print $1}')" || {
  current=$(sha256sum "$ROOT/vmlinux-bm" | awk '{print $1}')
  if [ ! -f "$ROOT/vmlinux-bm-original-$VENDOR_SUFFIX" ]; then
    test "$current" = "$VENDOR_DIGEST"
    install -m 0644 "$ROOT/vmlinux-bm" "$ROOT/vmlinux-bm-original-$VENDOR_SUFFIX"
    install -m 0644 "$ROOT/version" "$ROOT/version.original-$VENDOR_SUFFIX"
    install -m 0644 "$ROOT/version.json" "$ROOT/version.json.original-$VENDOR_SUFFIX"
  fi
  test -f "$ROOT/vmlinux-bm-original-$VENDOR_SUFFIX"
  test -f "$ROOT/version.original-$VENDOR_SUFFIX"
  test -f "$ROOT/version.json.original-$VENDOR_SUFFIX"
  install -m 0644 "$STAGE/vmlinux-kprobes" "$ROOT/vmlinux-bm"
  install -m 0644 "$STAGE/version" "$ROOT/version"
  install -m 0644 "$STAGE/version.json" "$ROOT/version.json"
  ln -sfn vmlinux-bm "$ROOT/vmlinux"
  echo changed
}
mkdir -p "$COMP"
if [ ! -f "$COMP/vmlinux-bm" ] || [ "$(sha256sum "$COMP/vmlinux-bm" | awk '{print $1}')" != "$EXPECTED" ]; then
  install -m 0644 "$STAGE/vmlinux-kprobes" "$COMP/vmlinux-bm"
  printf 'bm\n' > "$COMP/variant"
  printf 'sha256:%s\n' "$EXPECTED" > "$COMP/version"
  ln -sfn vmlinux-bm "$COMP/vmlinux"
  echo changed
fi
EOF
); then
  echo "kernel installation or component registration failed" >&2
  exit 1
fi

if grep -q changed <<<"$changed"; then
  kubectl -n "$NAMESPACE" rollout restart daemonset/cube-node
  kubectl -n "$NAMESPACE" rollout status daemonset/cube-node --timeout=10m
else
  echo "kprobe kernel already installed and registered: $VERSION_ID"
fi

echo "kernel=$EXPECTED_DIGEST component=$VERSION_ID node=$NODE"
