#!/usr/bin/env bash
# Install an isolated Firecracker RuntimeClass backed by Kata's shipped debug
# guest kernel. The production kata-fc-arm64 handler and config are unchanged.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
KATA_SHARE="${KATA_SHARE:-/opt/kata/share/kata-containers}"
KATA_CONFIG_DIR="${KATA_CONFIG_DIR:-/opt/kata/share/defaults/kata-containers}"
BASE_CONFIG="${KATA_CONFIG_DIR}/configuration-fc-arm64.toml"
EBPF_CONFIG="${KATA_CONFIG_DIR}/configuration-fc-arm64-ebpf.toml"
DEBUG_KERNEL="${KATA_SHARE}/vmlinux-debug.container"
CONTAINERD_DROPIN="/etc/containerd/conf.d/20-clawbox-firecracker.toml"

[[ "${1:-}" == apply ]] || {
  echo "usage: install-ebpf-kata-runtime.sh apply" >&2
  exit 64
}
command -v kubectl >/dev/null 2>&1 || { echo "kubectl is required" >&2; exit 69; }
[[ -r "${BASE_CONFIG}" ]] || { echo "missing ${BASE_CONFIG}" >&2; exit 66; }
[[ -r "${DEBUG_KERNEL}" ]] || { echo "missing ${DEBUG_KERNEL}" >&2; exit 66; }
debug_kernel_real="$(readlink -f "${DEBUG_KERNEL}")"
debug_release="$(basename "${debug_kernel_real}")"
debug_release="${debug_release#vmlinux-}"
DEBUG_KERNEL_CONFIG="${KATA_SHARE}/config-${debug_release}"
[[ -r "${DEBUG_KERNEL_CONFIG}" ]] || { echo "missing ${DEBUG_KERNEL_CONFIG}" >&2; exit 66; }

for option in CONFIG_BPF_EVENTS=y CONFIG_DEBUG_INFO_BTF=y CONFIG_KPROBES=y \
  CONFIG_KPROBE_EVENTS=y CONFIG_PERF_EVENTS=y CONFIG_TRACING=y; do
  grep -qx "${option}" "${DEBUG_KERNEL_CONFIG}" || {
    echo "debug guest kernel lacks ${option}" >&2
    exit 65
  }
done

tmp_dir="$(mktemp -d)"
cleanup() { rm -rf -- "${tmp_dir}"; }
trap cleanup EXIT INT TERM
rendered="${tmp_dir}/configuration-fc-arm64-ebpf.toml"
sed "s|^kernel = .*|kernel = \"${DEBUG_KERNEL}\"|" "${BASE_CONFIG}" >"${rendered}"
grep -Fx "kernel = \"${DEBUG_KERNEL}\"" "${rendered}" >/dev/null

sudo install -m 0644 "${rendered}" "${EBPF_CONFIG}"
sudo install -m 0644 "${ROOT}/deploy/containerd-firecracker.toml" "${CONTAINERD_DROPIN}"
sudo systemctl restart containerd
for _ in $(seq 1 60); do
  sudo ctr plugins ls 2>/dev/null | grep -q 'io.containerd.grpc.v1.*cri.*ok' && break
  sleep 1
done
sudo containerd config dump | grep -F 'runtimes.kata-fc-arm64-ebpf' >/dev/null
kubectl apply -f "${ROOT}/deploy/runtimeclass-firecracker-ebpf.yaml"
kubectl get runtimeclass kata-fc-arm64-ebpf -o name
