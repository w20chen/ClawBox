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

pull_devmapper() {
  ctr --namespace k8s.io images pull \
    --snapshotter devmapper --platform linux/amd64 "$1"
}

pull_devmapper "${pause_image}"
pull_devmapper docker.io/library/alpine:3.22
