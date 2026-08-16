#!/usr/bin/env bash
# FC-2: production LVM thin pool for containerd.  Loop devices are forbidden.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="plan"
DATA_DEVICE=""
METADATA_DEVICE=""
DATA_SIZE="${CLAWBOX_DEVMAPPER_DATA_SIZE:-90%PVS}"
METADATA_SIZE="${CLAWBOX_DEVMAPPER_METADATA_SIZE:-16G}"
VG="${CLAWBOX_DEVMAPPER_VG:-clawbox}"
POOL="${CLAWBOX_DEVMAPPER_POOL:-fc-pool}"
BASE_IMAGE_SIZE="${CLAWBOX_BASE_IMAGE_SIZE:-64GB}"
THRESHOLD="${CLAWBOX_DEVMAPPER_THRESHOLD:-80}"
CONFIRM=""
STATE_DIR="/var/lib/clawbox-bootstrap"
CONTAINERD_CONFIG="/etc/containerd/config.toml"
DROPIN_DIR="/etc/containerd/conf.d"
SERVICE="/etc/systemd/system/clawbox-devmapper.service"
TMP_DIR=""

usage() {
  cat >&2 <<'EOF'
usage: setup-devmapper-openeuler-arm64.sh plan|apply|status [options]
  --data-device /dev/DISK       Dedicated data disk (whole block device)
  --metadata-device /dev/DISK   Dedicated metadata disk (whole block device)
  --data-size SIZE              LVM size, default 90%PVS
  --metadata-size SIZE          LVM size, default 16G
  --confirm-erase D,M           Required by apply; exact two canonical devices

plan and status are read-only. apply irreversibly initializes only the two
explicit devices after proving neither backs /, this checkout, or any mount.
EOF
  exit 64
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    plan|apply|status) MODE="$1"; shift ;;
    --data-device) DATA_DEVICE="${2:-}"; shift 2 ;;
    --metadata-device) METADATA_DEVICE="${2:-}"; shift 2 ;;
    --data-size) DATA_SIZE="${2:-}"; shift 2 ;;
    --metadata-size) METADATA_SIZE="${2:-}"; shift 2 ;;
    --confirm-erase) CONFIRM="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

valid_lvm_name() { [[ "$1" =~ ^[A-Za-z0-9_+.-]+$ ]]; }
valid_lvm_name "${VG}" && valid_lvm_name "${POOL}" || usage
[[ "${THRESHOLD}" =~ ^[0-9]+$ ]] && (( THRESHOLD >= 1 && THRESHOLD <= 99 )) || usage

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing command: $1" >&2; exit 69; }; }
for command in findmnt lsblk readlink; do need "${command}"; done

canonical() { readlink -f -- "$1"; }
device_chain() { lsblk -snrpo PATH "$1" 2>/dev/null || true; }

validate_device() {
  local input="$1" role="$2" resolved type rows mountpoint base
  [[ -n "${input}" ]] || { echo "--${role}-device is required" >&2; exit 64; }
  resolved="$(canonical "${input}")"
  [[ -b "${resolved}" ]] || { echo "${role} device is not a block device: ${resolved}" >&2; exit 1; }
  type="$(lsblk -dnro TYPE "${resolved}")"
  [[ "${type}" == disk ]] || { echo "${role} device must be a whole disk, got ${type}: ${resolved}" >&2; exit 1; }
  [[ "${resolved}" != /dev/loop* ]] || { echo "loopback devices are forbidden in production: ${resolved}" >&2; exit 1; }
  rows="$(lsblk -nrpo PATH "${resolved}")"
  [[ "$(wc -l <<<"${rows}" | tr -d ' ')" == 1 ]] \
    || { echo "${role} device has partitions/children and will not be adopted: ${resolved}" >&2; exit 1; }
  mountpoint="$(lsblk -dnro MOUNTPOINTS "${resolved}" | tr -d '[:space:]')"
  [[ -z "${mountpoint}" ]] || { echo "${role} device is mounted: ${resolved}" >&2; exit 1; }

  for protected in "$(findmnt -no SOURCE /)" "$(findmnt -no SOURCE -T "${ROOT}")"; do
    [[ -n "${protected}" ]] || continue
    if device_chain "${protected}" | grep -Fxq "${resolved}"; then
      echo "${role} device backs a protected filesystem: ${resolved}" >&2
      exit 1
    fi
  done
  base="$(basename "${resolved}")"
  if [[ -d "/sys/class/block/${base}/holders" ]] \
    && find "/sys/class/block/${base}/holders" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "${role} device has active holders and will not be adopted: ${resolved}" >&2
    exit 1
  fi
  printf '%s' "${resolved}"
}

show_status() {
  echo "== ClawBox devmapper status =="
  if command -v lvs >/dev/null 2>&1 && lvs "${VG}/${POOL}" >/dev/null 2>&1; then
    lvs --units g -o vg_name,lv_name,lv_attr,lv_size,data_percent,metadata_percent "${VG}/${POOL}"
    usage_values="$(lvs --noheadings --nosuffix -o data_percent,metadata_percent "${VG}/${POOL}" | xargs)"
    read -r data_used metadata_used <<<"${usage_values}"
    awk -v d="${data_used:-100}" -v m="${metadata_used:-100}" -v t="${THRESHOLD}" \
      'BEGIN { if (d+0 >= t || m+0 >= t) exit 1 }' \
      || { echo "thin pool usage reached ${THRESHOLD}% (data=${data_used}% metadata=${metadata_used}%)" >&2; return 1; }
  else
    echo "thin pool ${VG}/${POOL}: missing"
    return 1
  fi
  if command -v ctr >/dev/null 2>&1; then
    ctr plugins ls | awk '$1 ~ /snapshotter/ && $2 == "devmapper" {print; found=($NF == "ok")} END {exit !found}'
  else
    echo "ctr is unavailable; devmapper plugin health is unproven" >&2
    return 1
  fi
}

if [[ "${MODE}" == status ]]; then
  show_status
  exit
fi

DATA_DEVICE="$(validate_device "${DATA_DEVICE}" data)"
METADATA_DEVICE="$(validate_device "${METADATA_DEVICE}" metadata)"
[[ "${DATA_DEVICE}" != "${METADATA_DEVICE}" ]] || { echo "data and metadata devices must differ" >&2; exit 1; }
echo "data=${DATA_DEVICE} size=${DATA_SIZE}"
echo "metadata=${METADATA_DEVICE} size=${METADATA_SIZE}"
echo "thin-pool=${VG}/${POOL} base-image=${BASE_IMAGE_SIZE} threshold=${THRESHOLD}%"

if [[ "${MODE}" == plan ]]; then
  echo "No changes made. apply requires: --confirm-erase ${DATA_DEVICE},${METADATA_DEVICE}"
  exit 0
fi

[[ "${MODE}" == apply ]] || usage
[[ "${CONFIRM}" == "${DATA_DEVICE},${METADATA_DEVICE}" ]] || {
  echo "refusing destructive initialization: pass --confirm-erase ${DATA_DEVICE},${METADATA_DEVICE}" >&2
  exit 1
}
[[ "$(id -u)" == 0 ]] || { echo "apply must run as root" >&2; exit 1; }
for command in pvcreate vgcreate lvcreate lvconvert lvs dmsetup containerd ctr systemctl; do need "${command}"; done

install -d -m 0700 "${STATE_DIR}/backups"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
for backup in \
  "${CONTAINERD_CONFIG}:containerd-config-${stamp}.toml" \
  "${DROPIN_DIR}/20-clawbox-firecracker.toml:containerd-firecracker-${stamp}.toml" \
  "${SERVICE}:clawbox-devmapper-${stamp}.service"; do
  source_path="${backup%%:*}"
  backup_name="${backup#*:}"
  [[ ! -f "${source_path}" ]] || cp -a "${source_path}" "${STATE_DIR}/backups/${backup_name}"
done

pvcreate --yes --force "${DATA_DEVICE}" "${METADATA_DEVICE}"
vgcreate "${VG}" "${DATA_DEVICE}" "${METADATA_DEVICE}"
if [[ "${DATA_SIZE}" == *%* ]]; then
  lvcreate --yes -l "${DATA_SIZE}" -n "${POOL}" "${VG}" "${DATA_DEVICE}"
else
  lvcreate --yes -L "${DATA_SIZE}" -n "${POOL}" "${VG}" "${DATA_DEVICE}"
fi
if [[ "${METADATA_SIZE}" == *%* ]]; then
  lvcreate --yes -l "${METADATA_SIZE}" -n "${POOL}-meta" "${VG}" "${METADATA_DEVICE}"
else
  lvcreate --yes -L "${METADATA_SIZE}" -n "${POOL}-meta" "${VG}" "${METADATA_DEVICE}"
fi
lvconvert --yes --type thin-pool --poolmetadata "${VG}/${POOL}-meta" "${VG}/${POOL}"
lvchange -ay "${VG}/${POOL}"

dm_name="$(lvs --noheadings -o dm_name "${VG}/${POOL}" | xargs)"
[[ -n "${dm_name}" ]] && dmsetup info "${dm_name}" >/dev/null \
  || { echo "LVM did not expose a usable thin-pool mapping" >&2; exit 1; }

install -d -m 0755 "${DROPIN_DIR}" /var/lib/containerd/io.containerd.snapshotter.v1.devmapper
sed \
  -e "s/pool_name = '[^']*'/pool_name = '${dm_name}'/" \
  -e "s/base_image_size = '[^']*'/base_image_size = '${BASE_IMAGE_SIZE}'/" \
  "${ROOT}/deploy/containerd-firecracker.toml" >"${DROPIN_DIR}/20-clawbox-firecracker.toml"

[[ -f "${CONTAINERD_CONFIG}" ]] || { echo "containerd config is missing: ${CONTAINERD_CONFIG}" >&2; exit 1; }
if grep -Eq '^[[:space:]]*imports[[:space:]]*=' "${CONTAINERD_CONFIG}"; then
  grep -Eq '/etc/containerd/conf\.d/\*\.toml' "${CONTAINERD_CONFIG}" \
    || { echo "existing containerd imports do not include ${DROPIN_DIR}/*.toml; merge manually from backup" >&2; exit 1; }
else
  TMP_DIR="$(mktemp -d)"
  trap '[[ -n "${TMP_DIR}" ]] && rm -rf -- "${TMP_DIR}"' EXIT
  { printf 'imports = ["/etc/containerd/conf.d/*.toml"]\n'; cat "${CONTAINERD_CONFIG}"; } >"${TMP_DIR}/config.toml"
  install -m 0644 "${TMP_DIR}/config.toml" "${CONTAINERD_CONFIG}"
fi

cat >"${SERVICE}" <<EOF
[Unit]
Description=Activate the ClawBox production devmapper thin pool
Requires=lvm2-monitor.service
After=lvm2-monitor.service local-fs.target
Before=containerd.service kubelet.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/lvchange -ay ${VG}/${POOL}
ExecStart=/usr/sbin/dmsetup info ${dm_name}

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now clawbox-devmapper.service
containerd config dump >/dev/null
systemctl restart containerd
for _ in $(seq 1 30); do
  if ctr plugins ls 2>/dev/null | awk '$1 ~ /snapshotter/ && $2 == "devmapper" && $NF == "ok" {found=1} END {exit !found}'; then
    show_status
    echo "FC-2 devmapper is ready"
    exit 0
  fi
  sleep 1
done
ctr plugins ls | grep -E 'TYPE|devmapper' >&2 || true
echo "containerd devmapper snapshotter did not become healthy" >&2
exit 1
