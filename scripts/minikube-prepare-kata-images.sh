#!/usr/bin/env bash
# Restore content and devmapper snapshots needed before the first kata-fc Pod.
set -euo pipefail

[[ "$(id -u)" == "0" ]] || { echo "run as root" >&2; exit 1; }
for command in containerd ctr awk; do
  command -v "${command}" >/dev/null 2>&1 || {
    echo "${command} is required in the Minikube node" >&2
    exit 1
  }
done

# Read the effective CRI sandbox image so this follows Minikube's Kubernetes
# version instead of hard-coding a pause tag.
pause_image="$(containerd config dump | awk \
  '$1 == "sandbox" && $2 == "=" { gsub(/\047|\042/, "", $3); print $3; exit }')"
[[ -n "${pause_image}" ]] || {
  echo "could not determine the CRI sandbox image from containerd config" >&2
  exit 1
}
pause_tag="${pause_image##*:}"
pause_mirror="${CLAWBOX_PAUSE_MIRROR:-registry.cn-hangzhou.aliyuncs.com/google_containers/pause}"
alpine_image="${CLAWBOX_ALPINE_IMAGE:-docker.io/library/alpine:3.22}"

pull_devmapper() {
  ctr --namespace k8s.io images pull \
    --snapshotter devmapper --platform linux/amd64 "$1"
}

# registry.k8s.io redirects to Google Artifact Registry, which is often
# unreachable from mainland China. Pull the same image from a configurable
# mirror, then add the exact name requested by kubelet. Fall back to upstream.
pause_source="${pause_mirror}:${pause_tag}"
if ! pull_devmapper "${pause_source}"; then
  echo "pause mirror failed (${pause_source}); trying ${pause_image}" >&2
  pull_devmapper "${pause_image}"
  pause_source="${pause_image}"
fi
if [[ "${pause_source}" != "${pause_image}" ]]; then
  ctr --namespace k8s.io images tag --force "${pause_source}" "${pause_image}"
fi

pull_devmapper "${alpine_image}"
if [[ "${alpine_image}" != "docker.io/library/alpine:3.22" ]]; then
  ctr --namespace k8s.io images tag --force "${alpine_image}" docker.io/library/alpine:3.22
fi
