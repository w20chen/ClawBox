#!/usr/bin/env bash
# Bootstrap one dedicated openEuler/Kunpeng host for ClawBox Kubernetes + Kata.
# Default mode is read-only. System mutation requires the explicit `apply` mode.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="plan"

KUBERNETES_MINOR="${KUBERNETES_MINOR:-1.35}"
CONTAINERD_VERSION="${CONTAINERD_VERSION:-2.3.4}"
RUNC_VERSION="${RUNC_VERSION:-1.5.1}"
CALICO_VERSION="${CALICO_VERSION:-3.32.1}"
KATA_VERSION="${KATA_VERSION:-3.31.0}"
FIRECRACKER_VERSION="${FIRECRACKER_VERSION:-1.12.1}"
RUNTIME_CLASS="${KUBERNETES_RUNTIME_CLASS:-kata-fc-arm64}"
POD_CIDR="${CLAWBOX_POD_CIDR:-192.168.0.0/22}"
SERVICE_CIDR="${CLAWBOX_SERVICE_CIDR:-10.96.0.0/12}"
ADVERTISE_ADDRESS="${CLAWBOX_ADVERTISE_ADDRESS:-}"
IMAGE_REPOSITORY="${KUBEADM_IMAGE_REPOSITORY:-registry.k8s.io}"
DEVMAPPER_DATA_DEVICE="${CLAWBOX_DEVMAPPER_DATA_DEVICE:-}"
DEVMAPPER_METADATA_DEVICE="${CLAWBOX_DEVMAPPER_METADATA_DEVICE:-}"
DEVMAPPER_CONFIRM="${CLAWBOX_DEVMAPPER_CONFIRM_ERASE:-}"
MAX_PODS="${CLAWBOX_MAX_PODS:-512}"
SYSTEM_RESERVED="${CLAWBOX_SYSTEM_RESERVED:-cpu=4,memory=8Gi,ephemeral-storage=20Gi}"
KUBE_RESERVED="${CLAWBOX_KUBE_RESERVED:-cpu=4,memory=8Gi,ephemeral-storage=20Gi}"

STATE_DIR="/var/lib/clawbox-bootstrap"
STATE_FILE="${STATE_DIR}/versions.env"
OWNER_MARKER="${STATE_DIR}/owned-host"
STATE_SCHEMA_VERSION="2"
ADMIN_CONF="/etc/kubernetes/admin.conf"
CONTAINERD_SOCKET="/run/containerd/containerd.sock"
TMP_DIR=""
USER_KUBECONFIG=""

usage() {
  cat <<'EOF'
Usage:
  bash scripts/bootstrap-openeuler-arm64.sh [plan|apply|status] [options]

Modes:
  plan    Read-only host inspection and exact change plan (default)
  apply   Install and initialize the single-node Kubernetes + Kata substrate
  status  Read-only versions, services, nodes, RuntimeClasses and pods

Options:
  --advertise-address IPV4  Kubernetes API address; default: default-route source
  --pod-cidr CIDR           Calico Pod CIDR; default: 192.168.0.0/22
  --service-cidr CIDR       Kubernetes Service CIDR; default: 10.96.0.0/12
  --runtime-class NAME      Required class; must be kata-fc-arm64
  --devmapper-data-device D Dedicated whole data disk
  --devmapper-meta-device D Dedicated whole metadata disk
  --confirm-erase D,M       Required apply authorization for those exact disks

Reviewed version overrides are environment variables:
  KUBERNETES_MINOR, CONTAINERD_VERSION, RUNC_VERSION, CALICO_VERSION,
  KATA_VERSION, FIRECRACKER_VERSION,
  KUBEADM_IMAGE_REPOSITORY, CLAWBOX_MAX_PODS, CLAWBOX_SYSTEM_RESERVED,
  CLAWBOX_KUBE_RESERVED.

This installer is for a dedicated bare host. It does not adopt an unrelated
existing Kubernetes cluster and does not implement an automatic destructive
reset. Backups of replaced host configuration are kept under
/var/lib/clawbox-bootstrap/backups.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    plan|apply|status) MODE="$1"; shift ;;
    --advertise-address) ADVERTISE_ADDRESS="${2:-}"; shift 2 ;;
    --pod-cidr) POD_CIDR="${2:-}"; shift 2 ;;
    --service-cidr) SERVICE_CIDR="${2:-}"; shift 2 ;;
    --runtime-class) RUNTIME_CLASS="${2:-}"; shift 2 ;;
    --devmapper-data-device) DEVMAPPER_DATA_DEVICE="${2:-}"; shift 2 ;;
    --devmapper-meta-device) DEVMAPPER_METADATA_DEVICE="${2:-}"; shift 2 ;;
    --confirm-erase) DEVMAPPER_CONFIRM="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done

log() { printf '\n==> %s\n' "$*"; }
note() { printf '  %-30s %s\n' "$1" "$2"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }

valid_runtime_name() {
  [[ "$1" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ && ${#1} -le 63 ]]
}

valid_minor() { [[ "$1" =~ ^[0-9]+\.[0-9]+$ ]]; }
valid_version() { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; }

detect_advertise_address() {
  local detected
  detected="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')"
  if [[ -z "${detected}" ]]; then
    detected="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  printf '%s' "${detected}"
}

validate_network_inputs() {
  python3 - "${POD_CIDR}" "${SERVICE_CIDR}" "${ADVERTISE_ADDRESS}" <<'PY'
import ipaddress
import subprocess
import sys

pod = ipaddress.ip_network(sys.argv[1], strict=True)
service = ipaddress.ip_network(sys.argv[2], strict=True)
advertise = ipaddress.ip_address(sys.argv[3])
if pod.version != 4 or service.version != 4 or advertise.version != 4:
    raise SystemExit("only IPv4 bootstrap values are supported")
if pod.overlaps(service):
    raise SystemExit(f"Pod CIDR {pod} overlaps Service CIDR {service}")

routes = subprocess.run(
    ["ip", "-4", "route", "show"], check=True, text=True,
    stdout=subprocess.PIPE,
).stdout.splitlines()
for line in routes:
    first = line.split()[0] if line.split() else ""
    if first in {"", "default", "blackhole", "unreachable", "prohibit"}:
        continue
    try:
        route = ipaddress.ip_network(first, strict=False)
    except ValueError:
        continue
    if pod.overlaps(route):
        raise SystemExit(f"Pod CIDR {pod} overlaps host route {route}: {line}")
    if service.overlaps(route):
        raise SystemExit(f"Service CIDR {service} overlaps host route {route}: {line}")
PY
}

version_at_least() {
  local actual="$1" minimum="$2"
  [[ "$(printf '%s\n%s\n' "${minimum}" "${actual}" | sort -V | head -1)" == "${minimum}" ]]
}

common_preflight() {
  [[ "$(uname -s)" == "Linux" ]] || die "Linux is required"
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${ID,,}" == "openeuler" ]] || die "openEuler is required; detected ${PRETTY_NAME:-${ID:-unknown}}"
  [[ "$(uname -m)" =~ ^(aarch64|arm64)$ ]] || die "arm64 host required; detected $(uname -m)"
  [[ -c /dev/kvm ]] || die "/dev/kvm is missing"
  [[ -f /sys/fs/cgroup/cgroup.controllers ]] || die "cgroup v2 unified hierarchy is required"

  need awk
  need curl
  need df
  need dnf
  need grep
  need ip
  need python3
  need sed
  need sha256sum
  need sort
  need sudo
  need systemctl
  need tar
  need timeout

  valid_minor "${KUBERNETES_MINOR}" || die "invalid KUBERNETES_MINOR: ${KUBERNETES_MINOR}"
  for version in "${CONTAINERD_VERSION}" "${RUNC_VERSION}" "${CALICO_VERSION}" "${KATA_VERSION}"; do
    valid_version "${version}" || die "invalid semantic version: ${version}"
  done
  valid_runtime_name "${RUNTIME_CLASS}" || die "invalid RuntimeClass name: ${RUNTIME_CLASS}"
  [[ "${RUNTIME_CLASS}" == kata-fc-arm64 ]] || die "Firecracker-first bootstrap only supports kata-fc-arm64"
  [[ "${MAX_PODS}" =~ ^[0-9]+$ ]] && (( MAX_PODS >= 64 && MAX_PODS <= 1024 )) \
    || die "CLAWBOX_MAX_PODS must be between 64 and 1024"
  version_at_least "${KATA_VERSION}" "3.31.0" || die "Kata ${KATA_VERSION} is vulnerable to CVE-2026-47243; require >= 3.31.0"

  if [[ -z "${ADVERTISE_ADDRESS}" ]]; then
    ADVERTISE_ADDRESS="$(detect_advertise_address)"
  fi
  [[ -n "${ADVERTISE_ADDRESS}" ]] || die "could not detect API advertise address; pass --advertise-address"

  local cpus memory_kib disk_kib
  cpus="$(getconf _NPROCESSORS_ONLN)"
  memory_kib="$(awk '/MemTotal:/ {print $2}' /proc/meminfo)"
  disk_kib="$(df -Pk /var | awk 'NR == 2 {print $4}')"
  (( cpus >= 4 )) || die "at least 4 online CPUs are required; found ${cpus}"
  (( memory_kib >= 8 * 1024 * 1024 )) || die "at least 8 GiB RAM is required"
  (( disk_kib >= 30 * 1024 * 1024 )) || die "at least 30 GiB free under /var is required"

  if [[ ! -f "${ADMIN_CONF}" ]]; then
    validate_network_inputs
  elif [[ ! -f "${OWNER_MARKER}" ]]; then
    die "${ADMIN_CONF} exists but is not owned by this bootstrap; refusing to adopt the cluster"
  fi
}

print_plan() {
  local current_containerd="missing" current_kube="missing" firewalld="inactive" selinux="unavailable" swap_count
  command -v containerd >/dev/null 2>&1 && current_containerd="$(containerd --version 2>/dev/null || echo present)"
  command -v kubeadm >/dev/null 2>&1 && current_kube="$(kubeadm version -o short 2>/dev/null || echo present)"
  systemctl is-active --quiet firewalld 2>/dev/null && firewalld="active (apply will disable it for Calico)"
  command -v getenforce >/dev/null 2>&1 && selinux="$(getenforce) (apply will persist permissive mode)"
  swap_count="$(swapon --noheadings 2>/dev/null | wc -l | tr -d ' ')"

  log "ClawBox openEuler/arm64 bootstrap plan"
  note "host" "$(. /etc/os-release; printf '%s' "${PRETTY_NAME}") / $(uname -m) / $(uname -r)"
  note "API address" "${ADVERTISE_ADDRESS}"
  note "Pod CIDR" "${POD_CIDR}"
  note "Service CIDR" "${SERVICE_CIDR}"
  note "current containerd" "${current_containerd}"
  note "current Kubernetes" "${current_kube}"
  note "firewalld" "${firewalld}"
  note "SELinux" "${selinux}"
  note "active swap entries" "${swap_count}"
  note "containerd target" "${CONTAINERD_VERSION} (official static arm64 tarball)"
  note "Kubernetes target" "${KUBERNETES_MINOR}.x from pkgs.k8s.io"
  note "Calico target" "${CALICO_VERSION}, VXLAN, BGP disabled"
  note "Kata target" "${KATA_VERSION} / ${RUNTIME_CLASS}"
  note "Firecracker target" "${FIRECRACKER_VERSION} / native arm64 only"
  note "devmapper data disk" "${DEVMAPPER_DATA_DEVICE:-REQUIRED FOR APPLY}"
  note "devmapper metadata disk" "${DEVMAPPER_METADATA_DEVICE:-REQUIRED FOR APPLY}"
  note "kubelet maxPods" "${MAX_PODS}"
  note "system reserved" "${SYSTEM_RESERVED}"
  note "kube reserved" "${KUBE_RESERVED}"
  note "state/backups" "${STATE_DIR}"

  if [[ -n "${DEVMAPPER_DATA_DEVICE}" || -n "${DEVMAPPER_METADATA_DEVICE}" ]]; then
    validate_storage_devices
  fi

  cat <<'EOF'

Apply performs these ordered mutations:
  1. install base RPM dependencies and persist kernel modules/sysctls;
  2. disable swap and firewalld, configure NetworkManager for Calico;
  3. install verified arm64 containerd/runc artifacts and start containerd;
  4. install kubelet/kubeadm/kubectl and initialize one control plane;
  5. install Calico, enable scheduling on the single control-plane node;
  6. build/audit native arm64 Kata + Firecracker artifacts;
  7. initialize the two explicitly authorized disks as an LVM thin pool;
  8. install the audited handler and RuntimeClass, then run both smoke gates.

No changes have been made. Run the same command with `apply` after review.
EOF
}

ensure_sudo() {
  sudo -v
  sudo test -r /dev/kvm -a -w /dev/kvm || die "root cannot read/write /dev/kvm"
  if sudo systemctl is-active --quiet k3s 2>/dev/null || sudo systemctl is-active --quiet crio 2>/dev/null; then
    die "an active k3s or CRI-O installation conflicts with this dedicated-host bootstrap"
  fi
}

validate_storage_devices() {
  [[ -n "${DEVMAPPER_DATA_DEVICE}" && -n "${DEVMAPPER_METADATA_DEVICE}" ]] \
    || die "both --devmapper-data-device and --devmapper-meta-device are required"
  bash "${ROOT}/scripts/setup-devmapper-openeuler-arm64.sh" plan \
    --data-device "${DEVMAPPER_DATA_DEVICE}" \
    --metadata-device "${DEVMAPPER_METADATA_DEVICE}"
}

guard_version_drift() {
  local key requested installed installed_schema installed_runtime
  local -a missing=() migrated=()
  local legacy_runtime_state=false
  sudo test -f "${STATE_FILE}" || return 0

  installed_schema="$(sudo awk -F= '$1 == "STATE_SCHEMA_VERSION" {print substr($0, index($0, "=") + 1)}' "${STATE_FILE}")"
  if [[ -n "${installed_schema}" && "${installed_schema}" != "${STATE_SCHEMA_VERSION}" ]]; then
    die "installed state schema ${installed_schema} is not supported by schema ${STATE_SCHEMA_VERSION}; a reviewed migration is required"
  fi
  installed_runtime="$(sudo awk -F= '$1 == "RUNTIME_CLASS" {print substr($0, index($0, "=") + 1)}' "${STATE_FILE}")"
  if [[ -z "${installed_schema}" \
    && "${installed_runtime}" == kata-qemu-runtime-rs \
    && "${RUNTIME_CLASS}" == kata-fc-arm64 ]] \
    && ! sudo test -s "${ADMIN_CONF}" \
    && ! sudo test -f "${STATE_DIR}/stage0-passed"; then
    legacy_runtime_state=true
  fi
  for key in KUBERNETES_MINOR CONTAINERD_VERSION RUNC_VERSION CALICO_VERSION KATA_VERSION FIRECRACKER_VERSION RUNTIME_CLASS POD_CIDR SERVICE_CIDR ADVERTISE_ADDRESS IMAGE_REPOSITORY MAX_PODS SYSTEM_RESERVED KUBE_RESERVED; do
    requested="${!key}"
    installed="$(sudo awk -F= -v wanted="${key}" '$1 == wanted {print substr($0, index($0, "=") + 1)}' "${STATE_FILE}")"
    if [[ -z "${installed}" ]]; then
      missing+=("${key}")
      continue
    fi
    if [[ "${requested}" != "${installed}" ]]; then
      if [[ "${legacy_runtime_state}" == true ]]; then
        migrated+=("${key}:${installed}->${requested}")
        continue
      fi
      die "requested ${key}=${requested}, but this host owns ${installed}; upgrades require a reviewed migration"
    fi
  done
  if [[ -z "${installed_schema}" ]]; then
    missing+=(STATE_SCHEMA_VERSION)
  fi
  if (( ${#missing[@]} > 0 )); then
    if sudo test -f "${STATE_DIR}/stage0-passed"; then
      die "completed host state is missing fields (${missing[*]}); refusing an automatic migration"
    fi
    printf 'WARN: completing incomplete pre-stage0 state; missing fields: %s\n' "${missing[*]}" >&2
  fi
  if [[ "${legacy_runtime_state}" == true ]]; then
    printf 'WARN: migrating uninitialized QEMU state to Firecracker; changed fields: %s\n' "${migrated[*]}" >&2
  fi
}

prepare_state() {
  sudo install -d -m 0700 "${STATE_DIR}" "${STATE_DIR}/backups"
  sudo touch "${OWNER_MARKER}"
  TMP_DIR="$(mktemp -d)"
  trap '[[ -n "${TMP_DIR}" ]] && rm -rf -- "${TMP_DIR}"' EXIT
}

backup_once() {
  local source="$1"
  local label="$2"
  local destination="${STATE_DIR}/backups/${label}"
  if sudo test -e "${source}" && ! sudo test -e "${destination}"; then
    sudo cp -a "${source}" "${destination}"
  fi
}

download() {
  local url="$1" destination="$2"
  curl -fL --retry 4 --retry-all-errors --retry-delay 2 \
    --connect-timeout 15 --max-time 600 \
    "${url}" -o "${destination}"
}

check_download_endpoints() {
  local url
  for url in \
    "https://github.com/containerd/containerd/releases" \
    "https://pkgs.k8s.io/core:/stable:/v${KUBERNETES_MINOR}/rpm/repodata/repomd.xml" \
    "https://raw.githubusercontent.com/projectcalico/calico/v${CALICO_VERSION}/manifests/tigera-operator.yaml" \
    "https://ghcr.io/v2/" \
    "https://quay.io/v2/" \
    "https://${IMAGE_REPOSITORY}/v2/"; do
    # Probe with GET because some GitHub/CDN paths handle HEAD unreliably.  A
    # registry may legitimately return 401; curl still proves the route/TLS.
    curl -LsS --range 0-0 --retry 4 --retry-all-errors --retry-delay 2 \
      --connect-timeout 10 --max-time 60 -o /dev/null "${url}" \
      || die "required endpoint is unreachable: ${url}"
  done
}

install_host_prerequisites() {
  log "Installing host prerequisites"
  sudo dnf install -y ca-certificates conntrack-tools curl device-mapper e2fsprogs ethtool \
    file findutils gzip iproute iptables lvm2 openssl policycoreutils python3 socat \
    tar util-linux xz zstd chrony
  sudo systemctl enable --now chronyd

  cat >"${TMP_DIR}/clawbox-modules.conf" <<'EOF'
overlay
br_netfilter
vhost_vsock
vhost_net
EOF
  backup_once /etc/modules-load.d/clawbox-kubernetes.conf modules-load.before-clawbox
  sudo install -m 0644 "${TMP_DIR}/clawbox-modules.conf" /etc/modules-load.d/clawbox-kubernetes.conf
  for module in overlay br_netfilter vhost_vsock vhost_net; do
    sudo modprobe "${module}" || die "kernel module is unavailable: ${module}"
  done

  cat >"${TMP_DIR}/clawbox-sysctl.conf" <<'EOF'
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
EOF
  backup_once /etc/sysctl.d/99-clawbox-kubernetes.conf sysctl.before-clawbox
  sudo install -m 0644 "${TMP_DIR}/clawbox-sysctl.conf" /etc/sysctl.d/99-clawbox-kubernetes.conf
  sudo sysctl --system >/dev/null

  cat >"${TMP_DIR}/calico-networkmanager.conf" <<'EOF'
[keyfile]
unmanaged-devices=interface-name:cali*;interface-name:tunl*;interface-name:vxlan.calico;interface-name:vxlan-v6.calico;interface-name:wireguard.cali;interface-name:wg-v6.cali
EOF
  sudo install -d -m 0755 /etc/NetworkManager/conf.d
  backup_once /etc/NetworkManager/conf.d/calico.conf networkmanager-calico.before-clawbox
  sudo install -m 0644 "${TMP_DIR}/calico-networkmanager.conf" /etc/NetworkManager/conf.d/calico.conf
  if sudo systemctl is-active --quiet NetworkManager; then
    command -v nmcli >/dev/null 2>&1 || die "NetworkManager is active but nmcli is unavailable"
    sudo nmcli general reload || die "NetworkManager could not reload the Calico unmanaged-interface rule"
  fi

  if sudo systemctl is-active --quiet firewalld 2>/dev/null \
    || sudo systemctl is-enabled --quiet firewalld 2>/dev/null; then
    sudo systemctl disable --now firewalld
  fi

  if [[ -f /etc/selinux/config ]]; then
    backup_once /etc/selinux/config selinux-config.before-clawbox
    sed -E 's/^SELINUX=enforcing$/SELINUX=permissive/' /etc/selinux/config >"${TMP_DIR}/selinux-config"
    grep -Eq '^SELINUX=(permissive|disabled)$' "${TMP_DIR}/selinux-config" \
      || die "could not render persistent SELinux permissive/disabled mode"
    sudo install -m 0644 "${TMP_DIR}/selinux-config" /etc/selinux/config
  fi
  if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce)" == "Enforcing" ]]; then
    sudo setenforce 0
  fi

  sudo swapoff -a
  if awk '
    /^[[:space:]]*#/ { next }
    NF >= 3 && $3 == "swap" { found=1 }
    END { exit(found ? 0 : 1) }
  ' /etc/fstab; then
    backup_once /etc/fstab etc-fstab.before-clawbox
    awk '
      /^[[:space:]]*#/ { print; next }
      NF >= 3 && $3 == "swap" { print "# clawbox-disabled-swap " $0; next }
      { print }
    ' /etc/fstab >"${TMP_DIR}/fstab"
    sudo install -m 0644 "${TMP_DIR}/fstab" /etc/fstab
  fi
}

install_containerd() {
  local archive checksum_file runc_binary runc_sums
  archive="containerd-static-${CONTAINERD_VERSION}-linux-arm64.tar.gz"
  checksum_file="${archive}.sha256sum"
  log "Installing containerd ${CONTAINERD_VERSION} and runc ${RUNC_VERSION}"
  download "https://github.com/containerd/containerd/releases/download/v${CONTAINERD_VERSION}/${archive}" "${TMP_DIR}/${archive}"
  download "https://github.com/containerd/containerd/releases/download/v${CONTAINERD_VERSION}/${checksum_file}" "${TMP_DIR}/${checksum_file}"
  (cd "${TMP_DIR}" && sha256sum -c "${checksum_file}")

  runc_binary="runc.arm64"
  runc_sums="runc.sha256sum"
  download "https://github.com/opencontainers/runc/releases/download/v${RUNC_VERSION}/${runc_binary}" "${TMP_DIR}/${runc_binary}"
  download "https://github.com/opencontainers/runc/releases/download/v${RUNC_VERSION}/${runc_sums}" "${TMP_DIR}/${runc_sums}"
  grep -E "[ *]${runc_binary}$" "${TMP_DIR}/${runc_sums}" >"${TMP_DIR}/${runc_binary}.sha256sum" \
    || die "runc release checksum does not contain ${runc_binary}"
  (cd "${TMP_DIR}" && sha256sum -c "${runc_binary}.sha256sum")

  backup_once /etc/systemd/system/containerd.service containerd.service.before-clawbox
  backup_once /etc/containerd/config.toml containerd-config.toml.before-clawbox
  sudo tar -C /usr/local -xzf "${TMP_DIR}/${archive}"
  sudo install -m 0755 "${TMP_DIR}/${runc_binary}" /usr/local/sbin/runc
  sudo install -m 0644 "${ROOT}/deploy/containerd-clawbox.service" /etc/systemd/system/containerd.service

  if ! sudo test -f "${STATE_DIR}/containerd-config-owned"; then
    /usr/local/bin/containerd config default >"${TMP_DIR}/containerd-config.toml"
    grep -q 'SystemdCgroup = false' "${TMP_DIR}/containerd-config.toml" \
      || die "containerd default config no longer exposes SystemdCgroup; review ${CONTAINERD_VERSION}"
    sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' "${TMP_DIR}/containerd-config.toml"
    grep -q 'SystemdCgroup = true' "${TMP_DIR}/containerd-config.toml"
    sudo install -d -m 0755 /etc/containerd
    sudo install -m 0644 "${TMP_DIR}/containerd-config.toml" /etc/containerd/config.toml
    sudo touch "${STATE_DIR}/containerd-config-owned"
  fi

  sudo systemctl daemon-reload
  sudo systemctl enable containerd
  sudo systemctl restart containerd
  sudo timeout 20 /usr/local/bin/ctr version >/dev/null || die "containerd socket did not become reachable"
  sudo /usr/local/bin/ctr plugins ls | awk '
    $1 == "io.containerd.grpc.v1" && $2 == "cri" && $NF == "ok" { found=1 }
    $1 == "io.containerd.cri.v1" && $2 == "runtime" && $NF == "ok" { found=1 }
    END { exit(found ? 0 : 1) }
  ' || die "containerd CRI plugin is not healthy"
}

install_kubernetes_packages() {
  log "Installing Kubernetes ${KUBERNETES_MINOR}.x packages"
  cat >"${TMP_DIR}/kubernetes.repo" <<EOF
[kubernetes]
name=Kubernetes
baseurl=https://pkgs.k8s.io/core:/stable:/v${KUBERNETES_MINOR}/rpm/
enabled=1
gpgcheck=1
gpgkey=https://pkgs.k8s.io/core:/stable:/v${KUBERNETES_MINOR}/rpm/repodata/repomd.xml.key
exclude=kubelet kubeadm kubectl cri-tools kubernetes-cni
EOF
  backup_once /etc/yum.repos.d/kubernetes.repo kubernetes.repo.before-clawbox
  sudo install -m 0644 "${TMP_DIR}/kubernetes.repo" /etc/yum.repos.d/kubernetes.repo
  sudo dnf install -y kubelet kubeadm kubectl cri-tools kubernetes-cni --disableexcludes=kubernetes
  cat >"${TMP_DIR}/crictl.yaml" <<EOF
runtime-endpoint: unix://${CONTAINERD_SOCKET}
image-endpoint: unix://${CONTAINERD_SOCKET}
timeout: 10
debug: false
EOF
  backup_once /etc/crictl.yaml crictl.yaml.before-clawbox
  sudo install -m 0644 "${TMP_DIR}/crictl.yaml" /etc/crictl.yaml
  sudo systemctl enable --now kubelet
}

configure_kubelet_capacity() {
  local kubelet_env="/etc/sysconfig/kubelet"
  log "Configuring single-node capacity guardrails"
  backup_once "${kubelet_env}" kubelet-sysconfig.before-clawbox
  cat >"${TMP_DIR}/kubelet" <<EOF
# Managed by ClawBox. Cell admission accounts for two Pods and two VM overheads.
KUBELET_EXTRA_ARGS="--max-pods=${MAX_PODS} --system-reserved=${SYSTEM_RESERVED} --kube-reserved=${KUBE_RESERVED} --eviction-hard=memory.available<5%,nodefs.available<10%,nodefs.inodesFree<5%"
EOF
  sudo install -m 0644 "${TMP_DIR}/kubelet" "${kubelet_env}"
  sudo systemctl daemon-reload
}

configure_user_kubeconfig() {
  local run_user run_home run_uid run_gid
  run_user="${SUDO_USER:-${USER}}"
  [[ "${run_user}" != "root" ]] || die "run apply as the intended administrator account, not a root login shell"
  run_home="$(getent passwd "${run_user}" | cut -d: -f6)"
  run_uid="$(id -u "${run_user}")"
  run_gid="$(id -g "${run_user}")"
  [[ -n "${run_home}" ]] || die "could not determine home for ${run_user}"
  sudo install -d -m 0700 -o "${run_uid}" -g "${run_gid}" "${run_home}/.kube"
  sudo install -m 0600 -o "${run_uid}" -g "${run_gid}" "${ADMIN_CONF}" "${run_home}/.kube/config"
  USER_KUBECONFIG="${run_home}/.kube/config"
  export KUBECONFIG="${USER_KUBECONFIG}"
}

initialize_cluster() {
  local kube_version
  log "Initializing the single-node Kubernetes control plane"
  if [[ ! -s "${ADMIN_CONF}" ]]; then
    kube_version="$(kubeadm version -o short)"
    sudo kubeadm config images pull \
      --kubernetes-version "${kube_version}" \
      --image-repository "${IMAGE_REPOSITORY}" \
      --cri-socket "unix://${CONTAINERD_SOCKET}"
    sudo kubeadm init \
      --kubernetes-version "${kube_version}" \
      --apiserver-advertise-address "${ADVERTISE_ADDRESS}" \
      --pod-network-cidr "${POD_CIDR}" \
      --service-cidr "${SERVICE_CIDR}" \
      --image-repository "${IMAGE_REPOSITORY}" \
      --cri-socket "unix://${CONTAINERD_SOCKET}"
    sudo touch "${STATE_DIR}/cluster-initialized"
  fi
  configure_user_kubeconfig
  timeout 60 kubectl cluster-info >/dev/null || die "Kubernetes API is not reachable"
}

install_calico() {
  local crds operator rendered
  crds="${TMP_DIR}/calico-operator-crds.yaml"
  operator="${TMP_DIR}/tigera-operator.yaml"
  rendered="${TMP_DIR}/calico-installation.yaml"
  log "Installing Calico ${CALICO_VERSION} with VXLAN and NetworkPolicy enforcement"
  download "https://raw.githubusercontent.com/projectcalico/calico/v${CALICO_VERSION}/manifests/v1_crd_projectcalico_org.yaml" "${crds}"
  download "https://raw.githubusercontent.com/projectcalico/calico/v${CALICO_VERSION}/manifests/tigera-operator.yaml" "${operator}"
  if ! kubectl get crd installations.operator.tigera.io >/dev/null 2>&1; then
    kubectl create -f "${crds}"
  fi
  kubectl apply -f "${operator}"
  kubectl wait --for=condition=Established crd/installations.operator.tigera.io --timeout=180s
  kubectl -n tigera-operator rollout status deployment/tigera-operator --timeout=300s
  sed "s|__CLAWBOX_POD_CIDR__|${POD_CIDR}|g" "${ROOT}/deploy/calico-installation.yaml" >"${rendered}"
  grep -q '__CLAWBOX_POD_CIDR__' "${rendered}" && die "Calico Pod CIDR template was not rendered"
  kubectl apply -f "${rendered}"
  # Calico/tigera-operator 3.32 reports readiness as the `Ready` condition on the
  # Installation CR, not `Available`; waiting on the wrong condition always times out.
  kubectl wait --for=condition=Ready installation/default --timeout=600s
  kubectl -n calico-system rollout status daemonset/calico-node --timeout=600s
  kubectl -n calico-system rollout status deployment/calico-kube-controllers --timeout=600s
  kubectl taint nodes --all node-role.kubernetes.io/control-plane- >/dev/null 2>&1 || true
  kubectl wait --for=condition=Ready nodes --all --timeout=600s
}

install_kata_firecracker() {
  log "Auditing Kata ${KATA_VERSION} + Firecracker ${FIRECRACKER_VERSION} before any handler change"
  if sudo bash "${ROOT}/scripts/audit-kata-firecracker-arm64.sh" \
    --root /opt/kata --kata-version "${KATA_VERSION}" --firecracker-version "${FIRECRACKER_VERSION}" \
    --emit "${STATE_DIR}/firecracker-audit.env"; then
    log "Reusing the complete audited Kata/Firecracker artifact tree"
  else
    log "Artifact audit was incomplete; assembling the pinned arm64 stack"
    sudo env KATA_VERSION="${KATA_VERSION}" FIRECRACKER_VERSION="${FIRECRACKER_VERSION}" \
      bash "${ROOT}/scripts/build-kata-firecracker-arm64.sh" install
  fi

  [[ -n "${DEVMAPPER_DATA_DEVICE}" && -n "${DEVMAPPER_METADATA_DEVICE}" ]] \
    || die "apply requires --devmapper-data-device and --devmapper-meta-device"
  sudo bash "${ROOT}/scripts/setup-devmapper-openeuler-arm64.sh" apply \
    --data-device "${DEVMAPPER_DATA_DEVICE}" \
    --metadata-device "${DEVMAPPER_METADATA_DEVICE}" \
    --confirm-erase "${DEVMAPPER_CONFIRM}"

  sudo bash "${ROOT}/scripts/audit-kata-firecracker-arm64.sh" \
    --root /opt/kata --kata-version "${KATA_VERSION}" --firecracker-version "${FIRECRACKER_VERSION}" \
    --emit "${STATE_DIR}/firecracker-audit.env"
  sudo ctr plugins ls | awk \
    '$1 ~ /snapshotter/ && $2 == "devmapper" && $NF == "ok" {found=1} END {exit !found}' \
    || die "devmapper is not healthy; RuntimeClass will not be created"
  sudo containerd config dump | grep -F "runtimes.${RUNTIME_CLASS}" >/dev/null \
    || die "containerd does not expose handler ${RUNTIME_CLASS}"

  kubectl apply -f "${ROOT}/deploy/runtimeclass-firecracker.yaml"
  handler="$(kubectl get runtimeclass "${RUNTIME_CLASS}" -o jsonpath='{.handler}')"
  [[ "${handler}" == "${RUNTIME_CLASS}" ]] || die "RuntimeClass handler mismatch: ${handler}"
}

write_state() {
  cat >"${TMP_DIR}/versions.env" <<EOF
STATE_SCHEMA_VERSION=${STATE_SCHEMA_VERSION}
KUBERNETES_MINOR=${KUBERNETES_MINOR}
CONTAINERD_VERSION=${CONTAINERD_VERSION}
RUNC_VERSION=${RUNC_VERSION}
CALICO_VERSION=${CALICO_VERSION}
KATA_VERSION=${KATA_VERSION}
FIRECRACKER_VERSION=${FIRECRACKER_VERSION}
RUNTIME_CLASS=${RUNTIME_CLASS}
POD_CIDR=${POD_CIDR}
SERVICE_CIDR=${SERVICE_CIDR}
ADVERTISE_ADDRESS=${ADVERTISE_ADDRESS}
IMAGE_REPOSITORY=${IMAGE_REPOSITORY}
MAX_PODS=${MAX_PODS}
SYSTEM_RESERVED=${SYSTEM_RESERVED}
KUBE_RESERVED=${KUBE_RESERVED}
EOF
  sudo install -m 0600 "${TMP_DIR}/versions.env" "${STATE_FILE}"
}

show_status() {
  log "Host services"
  systemctl is-active containerd kubelet 2>/dev/null || true
  command -v containerd >/dev/null 2>&1 && containerd --version || true
  command -v runc >/dev/null 2>&1 && runc --version | head -1 || true
  command -v kubeadm >/dev/null 2>&1 && kubeadm version -o short || true
  bash "${ROOT}/scripts/audit-kata-firecracker-arm64.sh" --root /opt/kata \
    --kata-version "${KATA_VERSION}" --firecracker-version "${FIRECRACKER_VERSION}" || true
  bash "${ROOT}/scripts/setup-devmapper-openeuler-arm64.sh" status || true
  local status_kubeconfig="${KUBECONFIG:-${HOME}/.kube/config}"
  if [[ -r "${status_kubeconfig}" ]] && command -v kubectl >/dev/null 2>&1; then
    KUBECONFIG="${status_kubeconfig}" kubectl get nodes -o wide
    KUBECONFIG="${status_kubeconfig}" kubectl get runtimeclass
    KUBECONFIG="${status_kubeconfig}" kubectl get pods -A -o wide
  fi
}

apply_bootstrap() {
  ensure_sudo
  validate_storage_devices
  guard_version_drift
  prepare_state
  backup_once "${STATE_FILE}" "versions.env.before-schema-${STATE_SCHEMA_VERSION}"
  check_download_endpoints
  write_state
  install_host_prerequisites
  install_containerd
  install_kubernetes_packages
  configure_kubelet_capacity
  initialize_cluster
  install_calico
  install_kata_firecracker
  write_state

  log "Running authoritative ClawBox stage-0 gates"
  sudo rm -f "${STATE_DIR}/stage0-passed"
  kubectl label nodes --all clawbox.openai.com/firecracker-ready=true --overwrite
  if ! sudo env KUBECONFIG="${ADMIN_CONF}" PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    bash "${ROOT}/deploy/check-host.sh" --runtime-class "${RUNTIME_CLASS}"; then
    kubectl label nodes --all clawbox.openai.com/firecracker-ready- >/dev/null 2>&1 || true
    die "static Firecracker host gate failed; ready labels were removed"
  fi
  if ! KUBECONFIG="${USER_KUBECONFIG}" bash "${ROOT}/scripts/arm64-kata-smoke.sh" --runtime-class "${RUNTIME_CLASS}"; then
    kubectl label nodes --all clawbox.openai.com/firecracker-ready- >/dev/null 2>&1 || true
    die "live Firecracker smoke failed; ready labels were removed"
  fi
  sudo touch "${STATE_DIR}/stage0-passed"
  log "Bootstrap complete"
  show_status
}

case "${MODE}" in
  plan)
    common_preflight
    print_plan
    ;;
  apply)
    common_preflight
    apply_bootstrap
    ;;
  status)
    show_status
    ;;
  *) die "unsupported mode: ${MODE}" ;;
esac
