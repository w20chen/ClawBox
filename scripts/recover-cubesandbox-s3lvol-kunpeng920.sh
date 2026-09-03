#!/usr/bin/env bash
set -euo pipefail

# Restore the CubeS3lvol host service required by CubeSandbox v0.7.0's
# cubecow engine.  The Kubernetes chart does not install this host daemon.

NAMESPACE=${CUBE_NAMESPACE:-cube-system}
NODE=${CUBE_NODE_NAME:-$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')}
RELEASE_URL=${CUBE_ONE_CLICK_URL:-https://github.com/TencentCloud/CubeSandbox/releases/download/v0.7.0/cube-sandbox-one-click-v0.7.0-arm64.tar.gz}
WORK=/tmp/clawbox-cube-s3lvol-v0.7.0
ARCHIVE=$WORK/one-click.tar.gz

mkdir -p "$WORK"
if [[ ! -s "$ARCHIVE" ]]; then
  curl -fL --retry 3 -o "$ARCHIVE" "$RELEASE_URL"
fi
rm -rf "$WORK/release"
mkdir -p "$WORK/release"
tar -xzf "$ARCHIVE" -C "$WORK/release"
outer=$(find "$WORK/release" -path '*/assets/package/sandbox-package.tar.gz' -print -quit)
[[ -n "$outer" ]] || { echo "one-click package is missing sandbox-package.tar.gz" >&2; exit 1; }
tar -xzf "$outer" -C "$WORK/release"
pkg="$WORK/release/sandbox-package"
[[ -x "$pkg/CubeS3lvol/bin/s3lvol_tgt" ]] || { echo "s3lvol_tgt is missing" >&2; exit 1; }

# Give the host daemon a stable Kubernetes service IP for the in-cluster MinIO.
if ! kubectl -n "$NAMESPACE" get service cube-minio-s3lvol >/dev/null 2>&1; then
  kubectl -n "$NAMESPACE" expose pod cube-minio-0 \
    --name cube-minio-s3lvol --port 9000 --target-port 9000
fi
minio_ip=$(kubectl -n "$NAMESPACE" get service cube-minio-s3lvol -o jsonpath='{.spec.clusterIP}')

umask 077
secret_env="$WORK/volume-s3.conf"
kubectl -n "$NAMESPACE" get secret cube-volume-s3 \
  -o jsonpath='{.data.volume-s3\.conf}' | base64 -d > "$secret_env"
# shellcheck disable=SC1090
source "$secret_env"
cat > "$WORK/s3.cfg" <<EOF
access_key_id="$ACCESS_KEY_ID"
secret_access_key="$SECRET_ACCESS_KEY"
endpoint="$minio_ip:9000"
region="$REGION"
buckets=["cube-s3lvol"]
path_style="true"
no_tls="true"
EOF
rm -f "$secret_env"

cat > "$WORK/install-root.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
work=/tmp/clawbox-cube-s3lvol-v0.7.0
pkg=$work/release/sandbox-package
toolbox=/usr/local/services/cubetoolbox
systemctl stop cube-sandbox-s3lvol.service 2>/dev/null || true
if ! command -v nvme >/dev/null 2>&1; then
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y nvme-cli
  elif command -v yum >/dev/null 2>&1; then
    yum install -y nvme-cli
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y nvme-cli
  else
    echo "nvme-cli is required but no supported package manager was found" >&2
    exit 1
  fi
fi
install -d -m 755 "$toolbox/CubeS3lvol" "$toolbox/scripts/systemd" \
  "$toolbox/scripts/common" /data/cubelet/rcow /data/log/rcow
cp -a "$pkg/CubeS3lvol/." "$toolbox/CubeS3lvol/"
cp -a "$pkg/scripts/systemd/." "$toolbox/scripts/systemd/"
cp -a "$pkg/scripts/common/." "$toolbox/scripts/common/"
install -m 644 "$pkg/systemd/cube-sandbox-s3lvol.service" \
  /etc/systemd/system/cube-sandbox-s3lvol.service
install -m 600 "$work/s3.cfg" /data/cubelet/s3.cfg
cat > "$toolbox/.one-click.env" <<ENV
ONE_CLICK_ENABLE_S3LVOL=1
RCOW_WAL_MB=32768
RCOW_JOURNAL_MB=1024
RCOW_CAPACITY_GB=16384
RCOW_TGT_CPUMASK=0x3
RCOW_TGT_MEM_MB=16384
RCOW_LISTEN_ADDR=127.0.0.1
RCOW_LISTEN_PORT=4420
ENV
chmod 600 "$toolbox/.one-click.env"
chmod +x "$toolbox/CubeS3lvol/bin/s3lvol_tgt" \
  "$toolbox/CubeS3lvol/scripts/"*.sh "$toolbox/scripts/systemd/"*.sh
# s3lvol needs one additional 4 KiB superblock beyond journal + WAL.
wal_bytes=35433484288
if [[ ! -f /data/cubelet/rcow/wal_bdev.img ]] ||
   (( $(stat -c %s /data/cubelet/rcow/wal_bdev.img) < wal_bytes )); then
  truncate -s "$wal_bytes" /data/cubelet/rcow/wal_bdev.img
fi
systemctl daemon-reload
systemctl add-wants multi-user.target cube-sandbox-s3lvol.service
systemctl restart cube-sandbox-s3lvol.service
sleep 15
systemctl is-active --quiet cube-sandbox-s3lvol.service
test -S /var/run/s3lvol.sock
EOF
chmod 700 "$WORK/install-root.sh"

pod=$(kubectl debug "node/$NODE" \
  --image=cube-sandbox-int.tencentcloudcr.com/cube-sandbox/cube-node-init:v0.7.0 \
  --profile=sysadmin -- chroot /host bash "$WORK/install-root.sh" 2>&1 \
  | sed -n 's/^Creating debugging pod \([^ ]*\).*/\1/p' | tail -1)
[[ -n "$pod" ]] || { echo "failed to create privileged recovery pod" >&2; exit 1; }
trap 'kubectl delete pod "$pod" --wait=false >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 60); do
  phase=$(kubectl get pod "$pod" -o jsonpath='{.status.phase}' 2>/dev/null || true)
  [[ "$phase" == Succeeded || "$phase" == Failed ]] && break
  sleep 2
done
kubectl logs "$pod"
[[ "${phase:-}" == Succeeded ]] || exit 1
echo "CubeS3lvol restored: /var/run/s3lvol.sock is active and boot-persistent"
