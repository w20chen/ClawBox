#!/usr/bin/env bash
# FC-2: production LVM thin pool for containerd.  Loop devices are forbidden.
set -euo pipefail

# This host's udev worker is wedged: default LVM/udev synchronization hangs in
# semtimedop holding the VG lock, and devices created with --noudevsync never
# get /dev nodes (udev never processes their events).  DM_DISABLE_UDEV is the
# official LVM escape hatch for exactly this: it hardcodes udev_rules=0,
# udev_sync=0 and udev_fallback=1, so LVM/libdevmapper create the device
# nodes and /dev/<vg>/<lv> symlinks themselves, and lvconvert's internal
# deactivate/reactivate of the metadata LV also recreates its node -- without
# this, the metadata wipe fails with "device not cleared" (device not found).
export DM_DISABLE_UDEV=1

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="plan"
DATA_DEVICE=""
METADATA_DEVICE=""
DATA_SIZE="${CLAWBOX_DEVMAPPER_DATA_SIZE:-90%PVS}"
# 8G not 16G: with the default 64KiB chunk LVM caps a thin pool's usable
# metadata LV at <15.88 GiB, so a 16G metadata LV creates fine but
# lvconvert then aborts with "failed to wipe metadata lv".
METADATA_SIZE="${CLAWBOX_DEVMAPPER_METADATA_SIZE:-8G}"
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
  --metadata-size SIZE          LVM size, default 8G (cap: <15.88G @64KiB chunk)
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

# LVM caps the usable metadata LV of a 64KiB-chunk thin pool at <15.88 GiB.
# A 16G metadata LV is created successfully but lvconvert then aborts with
# "failed to wipe metadata lv". Validate literal sizes so plan/apply fail
# fast with an actionable message (percentage sizes are left unchecked).
metadata_mib="$(awk -v s="${METADATA_SIZE}" '
  BEGIN {
    if (s ~ /%/) exit
    if (!match(s, /[0-9.]+/)) exit
    n = substr(s, RSTART, RLENGTH)
    u = s
    gsub(/[0-9.]/, "", u)
    u = toupper(u)
    if (u == "T" || u == "TB") m = n * 1024 * 1024
    else if (u == "G" || u == "GB") m = n * 1024
    else if (u == "M" || u == "MB") m = n
    else if (u == "K" || u == "KB") m = n / 1024
    else m = n / 1024 / 1024
    printf "%d", m
  }')"
if [[ -n "${metadata_mib}" ]] && (( metadata_mib > 15360 )); then
  echo "--metadata-size ${METADATA_SIZE} exceeds the LVM thin-pool metadata cap (15.88 GiB with a 64 KiB chunk); use <= 8G" >&2
  exit 64
fi

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
for command in wipefs pvcreate vgcreate lvcreate lvconvert lvchange lvs vgs vgscan dmsetup containerd ctr systemctl; do need "${command}"; done

if vgs "${VG}" >/dev/null 2>&1; then
  echo "Volume group ${VG} already exists (left over from a partial or complete run); aborting rather than clobbering it." >&2
  echo "Inspect with: vgs ${VG}; lvs ${VG}" >&2
  echo "If the thin pool is incomplete, remove it and re-run apply:" >&2
  echo "  vgremove -y ${VG} && pvremove -y ${DATA_DEVICE} ${METADATA_DEVICE}" >&2
  exit 1
fi

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

# Provisioning images (e.g. Foreman/PXE) stamp every disk with an empty GPT
# label; LVM refuses any device that carries a partition table, even one with
# zero partitions. validate_device has already confirmed these are dedicated
# whole disks with no children, mounts, or holders, so clearing the signatures
# is part of the explicit --confirm-erase authorization.
wipefs -a "${DATA_DEVICE}" "${METADATA_DEVICE}"
pvcreate --yes --force "${DATA_DEVICE}" "${METADATA_DEVICE}"
# A wedged udev leaves /dev/${VG} behind after a previous vgremove, and
# vgcreate then fails with "already exists in filesystem".  The VG was
# already proven absent by the vgs precheck above, so every entry inside
# can only be a stale symlink dangling to a removed device: delete the
# entries, then the empty directory.  If anything survives, abort with a
# clear message instead of clobbering it.
if [[ -d "/dev/${VG}" ]]; then
  find "/dev/${VG}" -mindepth 1 -maxdepth 1 -delete 2>/dev/null || true
  rmdir "/dev/${VG}" 2>/dev/null || {
    echo "/dev/${VG} is not empty; remove stale LVM symlinks and re-run apply" >&2
    exit 1
  }
fi
vgcreate "${VG}" "${DATA_DEVICE}" "${METADATA_DEVICE}"
if [[ "${DATA_SIZE}" =~ ^([0-9]+)%PVS$ ]]; then
  # %PVS is a percentage of ALL PVs in the VG, which over-sizes the request when
  # the metadata PV is also a whole multi-TB disk. Size against the data PV's
  # own extent count instead.
  pct="${BASH_REMATCH[1]}"
  pe_total="$(pvs --noheadings -o pv_name,pv_pe_count | awk -v d="${DATA_DEVICE}" '$1 == d {print $2}')"
  [[ "${pe_total}" =~ ^[0-9]+$ && "${pe_total}" -gt 0 ]] \
    || { echo "could not determine PE count for ${DATA_DEVICE}" >&2; exit 1; }
  pe_count=$(( pe_total * pct / 100 ))
  lvcreate --yes --noudevsync --zero n -l "${pe_count}" -n "${POOL}" "${VG}" "${DATA_DEVICE}"
elif [[ "${DATA_SIZE}" == *%* ]]; then
  lvcreate --yes --noudevsync --zero n -l "${DATA_SIZE}" -n "${POOL}" "${VG}" "${DATA_DEVICE}"
else
  lvcreate --yes --noudevsync --zero n -L "${DATA_SIZE}" -n "${POOL}" "${VG}" "${DATA_DEVICE}"
fi
if [[ "${METADATA_SIZE}" == *%* ]]; then
  lvcreate --yes --noudevsync --zero n -l "${METADATA_SIZE}" -n "${POOL}-meta" "${VG}" "${METADATA_DEVICE}"
else
  lvcreate --yes --noudevsync --zero n -L "${METADATA_SIZE}" -n "${POOL}-meta" "${VG}" "${METADATA_DEVICE}"
fi
# Do not wait on udev: on some openEuler hosts a stuck udev worker wedges the
# default LVM/udev synchronization and every lvcreate/lvconvert hangs in
# semtimedop while holding the VG lock.
#
# --noudevsync also means udev never creates the /dev nodes.  lvconvert must
# open the metadata LV to wipe it (thin-pool conversion performs "metadata
# wiping") and aborts with "/dev/clawbox/fc-pool-meta: not found: device not
# cleared" when the node is missing.  Recreate the nodes (udev-free) BEFORE
# converting so the fresh lvconvert device scan finds the metadata device.
lvchange -ay --noudevsync "${VG}/${POOL}" "${VG}/${POOL}-meta" || true
dmsetup mknodes || true
vgscan --mknodes >/dev/null 2>&1 || true
if [[ ! -e "/dev/${VG}/${POOL}-meta" && ! -e "/dev/mapper/${VG}-${POOL}-meta" ]]; then
  echo "metadata LV device node is missing after node recreation: /dev/${VG}/${POOL}-meta" >&2
  exit 1
fi
lvconvert --yes --noudevsync --type thin-pool --poolmetadata "${VG}/${POOL}-meta" "${VG}/${POOL}"
lvchange -ay --noudevsync "${VG}/${POOL}"
# Ensure /dev/mapper nodes exist even though udev was bypassed.
dmsetup mknodes
vgscan --mknodes >/dev/null 2>&1 || true

dm_path="$(lvs --noheadings -o lv_dm_path "${VG}/${POOL}" | xargs)"
[[ -n "${dm_path}" ]] || { echo "LVM did not report a dm path for ${VG}/${POOL}" >&2; exit 1; }
dm_name="$(basename "${dm_path}")"
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
# LVM manages the /dev nodes itself; do not wait on (possibly wedged) udev at boot.
Environment=DM_DISABLE_UDEV=1
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
