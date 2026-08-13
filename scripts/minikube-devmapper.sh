#!/usr/bin/env bash
# Configure the loopback thin pool required by Kata's Firecracker runtime.
# This script runs as root inside the Minikube node and is deliberately
# idempotent: existing data/meta files and an active pool are reused.
set -euo pipefail

DATA_DIR="${CLAWBOX_DEVMAPPER_DIR:-/var/lib/clawbox-devmapper}"
POOL_NAME="${CLAWBOX_DEVMAPPER_POOL:-clawbox-devpool}"
DATA_SIZE="${CLAWBOX_DEVMAPPER_DATA_SIZE:-100G}"
META_SIZE="${CLAWBOX_DEVMAPPER_META_SIZE:-2G}"
INSTALL_PATH="/usr/local/sbin/clawbox-devmapper-setup"
SERVICE_PATH="/etc/systemd/system/clawbox-devmapper.service"

[[ "$(id -u)" == "0" ]] || { echo "run as root" >&2; exit 1; }
for command in dmsetup losetup blockdev truncate awk install systemctl; do
  command -v "${command}" >/dev/null 2>&1 || {
    echo "${command} is required in the Minikube node" >&2
    exit 1
  }
done

# Minimal Minikube node images do not necessarily contain /usr/local/sbin.
mkdir -p "${DATA_DIR}" "$(dirname "${INSTALL_PATH}")" "$(dirname "${SERVICE_PATH}")"
if [[ ! -e "${DATA_DIR}/data" ]]; then
  truncate -s "${DATA_SIZE}" "${DATA_DIR}/data"
fi
if [[ ! -e "${DATA_DIR}/meta" ]]; then
  truncate -s "${META_SIZE}" "${DATA_DIR}/meta"
fi

loop_for() {
  local backing="$1" loop
  loop="$(losetup --list --noheadings --output NAME,BACK-FILE | awk -v file="${backing}" '$2 == file { print $1; exit }')"
  if [[ -z "${loop}" ]]; then
    loop="$(losetup --find --show "${backing}")"
  fi
  printf '%s' "${loop}"
}

if ! dmsetup info "${POOL_NAME}" >/dev/null 2>&1; then
  data_dev="$(loop_for "${DATA_DIR}/data")"
  meta_dev="$(loop_for "${DATA_DIR}/meta")"
  sectors="$(( $(blockdev --getsize64 "${data_dev}") / 512 ))"
  dmsetup create "${POOL_NAME}" \
    --table "0 ${sectors} thin-pool ${meta_dev} ${data_dev} 128 32768 1 skip_block_zeroing"
fi

# Persist this exact, tested setup logic in the node so the same loop files are
# attached again before containerd starts after a Minikube VM reboot.
if [[ "${0}" != "${INSTALL_PATH}" ]]; then
  install -m 0755 "${0}" "${INSTALL_PATH}"
fi
cat >"${SERVICE_PATH}" <<EOF
[Unit]
Description=ClawBox containerd devmapper thin pool
Before=containerd.service kubelet.service
RequiresMountsFor=${DATA_DIR}

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=CLAWBOX_DEVMAPPER_DIR=${DATA_DIR}
Environment=CLAWBOX_DEVMAPPER_POOL=${POOL_NAME}
ExecStart=${INSTALL_PATH}

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable clawbox-devmapper.service >/dev/null
dmsetup info "${POOL_NAME}" >/dev/null
