#!/usr/bin/env bash
# Live stage-0 gate: two arm64 Kata Pods, pair-only networking and cleanup.
set -euo pipefail

RUNTIME_CLASS="${CLAWBOX_RUNTIME_CLASS:-kata-qemu-runtime-rs}"
IMAGE="${CLAWBOX_SMOKE_IMAGE:-alpine:3.22}"
NAMESPACE="clawbox-stage0-${RANDOM:-0}"
KEEP=false

usage() {
  cat >&2 <<'EOF'
usage: arm64-kata-smoke.sh [--runtime-class NAME] [--image IMAGE]
                           [--namespace NAME] [--keep]

The image must publish a linux/arm64 manifest and contain /bin/sh, uname,
busybox httpd and wget (alpine:3.22 is the default).
EOF
  exit 64
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runtime-class) RUNTIME_CLASS="${2:-}"; shift 2 ;;
    --image) IMAGE="${2:-}"; shift 2 ;;
    --namespace) NAMESPACE="${2:-}"; shift 2 ;;
    --keep) KEEP=true; shift ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

valid_name() { [[ "$1" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ && ${#1} -le 63 ]]; }
valid_name "${RUNTIME_CLASS}" || { echo "invalid RuntimeClass name" >&2; exit 64; }
valid_name "${NAMESPACE}" || { echo "invalid namespace" >&2; exit 64; }
[[ "${IMAGE}" =~ ^[A-Za-z0-9._/@:-]+$ ]] || { echo "invalid image reference" >&2; exit 64; }

command -v kubectl >/dev/null 2>&1 || { echo "kubectl is required" >&2; exit 69; }
kubectl cluster-info >/dev/null
kubectl get runtimeclass "${RUNTIME_CLASS}" >/dev/null

created=false
cleanup() {
  if [[ "${created}" == true && "${KEEP}" != true ]]; then
    kubectl delete namespace "${NAMESPACE}" --wait=true --timeout=180s >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

kubectl create namespace "${NAMESPACE}" >/dev/null
created=true
kubectl label namespace "${NAMESPACE}" app.kubernetes.io/managed-by=clawbox-stage0 >/dev/null

cat <<EOF | kubectl -n "${NAMESPACE}" apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: tool
  labels: {app: clawbox-stage0, role: tool}
spec:
  automountServiceAccountToken: false
  runtimeClassName: ${RUNTIME_CLASS}
  restartPolicy: Never
  nodeSelector: {kubernetes.io/arch: arm64}
  containers:
    - name: tool
      image: ${IMAGE}
      command: ["/bin/sh", "-ec", "mkdir -p /www; uname -m > /www/index.html; exec busybox httpd -f -p 8080 -h /www"]
      ports: [{name: http, containerPort: 8080}]
      readinessProbe: {httpGet: {path: /, port: http}, periodSeconds: 1}
      resources:
        requests: {cpu: 100m, memory: 128Mi}
        limits: {cpu: 100m, memory: 128Mi}
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: {drop: ["ALL"]}
---
apiVersion: v1
kind: Service
metadata:
  name: tool
spec:
  selector: {app: clawbox-stage0, role: tool}
  ports: [{name: http, port: 8080, targetPort: http}]
---
apiVersion: v1
kind: Pod
metadata:
  name: runtime
  labels: {app: clawbox-stage0, role: runtime}
spec:
  automountServiceAccountToken: false
  runtimeClassName: ${RUNTIME_CLASS}
  restartPolicy: Never
  nodeSelector: {kubernetes.io/arch: arm64}
  containers:
    - name: runtime
      image: ${IMAGE}
      command: ["/bin/sh", "-ec", "exec sleep 3600"]
      resources:
        requests: {cpu: 100m, memory: 128Mi}
        limits: {cpu: 100m, memory: 128Mi}
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: {drop: ["ALL"]}
---
apiVersion: v1
kind: Pod
metadata:
  name: attacker
  labels: {app: clawbox-stage0, role: attacker}
spec:
  automountServiceAccountToken: false
  restartPolicy: Never
  nodeSelector: {kubernetes.io/arch: arm64}
  containers:
    - name: attacker
      image: ${IMAGE}
      command: ["/bin/sh", "-ec", "exec sleep 3600"]
      resources:
        requests: {cpu: 10m, memory: 32Mi}
        limits: {cpu: 10m, memory: 32Mi}
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: {drop: ["ALL"]}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
spec:
  podSelector: {matchLabels: {app: clawbox-stage0}}
  policyTypes: [Ingress, Egress]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: runtime-egress
spec:
  podSelector: {matchLabels: {app: clawbox-stage0, role: runtime}}
  policyTypes: [Egress]
  egress:
    - to:
        - podSelector: {matchLabels: {app: clawbox-stage0, role: tool}}
      ports: [{protocol: TCP, port: 8080}]
    - to:
        - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: kube-system}}
          podSelector: {matchLabels: {k8s-app: kube-dns}}
      ports: [{protocol: UDP, port: 53}, {protocol: TCP, port: 53}]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: tool-ingress
spec:
  podSelector: {matchLabels: {app: clawbox-stage0, role: tool}}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector: {matchLabels: {app: clawbox-stage0, role: runtime}}
      ports: [{protocol: TCP, port: 8080}]
EOF

if ! kubectl -n "${NAMESPACE}" wait --for=condition=Ready pod/tool pod/runtime pod/attacker --timeout=240s; then
  kubectl -n "${NAMESPACE}" get pods -o wide >&2 || true
  kubectl -n "${NAMESPACE}" describe pods >&2 || true
  kubectl -n "${NAMESPACE}" get events --sort-by=.lastTimestamp >&2 || true
  exit 1
fi

runtime_class="$(kubectl -n "${NAMESPACE}" get pod runtime -o jsonpath='{.spec.runtimeClassName}')"
tool_class="$(kubectl -n "${NAMESPACE}" get pod tool -o jsonpath='{.spec.runtimeClassName}')"
[[ "${runtime_class}" == "${RUNTIME_CLASS}" && "${tool_class}" == "${RUNTIME_CLASS}" ]]

runtime_arch="$(kubectl -n "${NAMESPACE}" exec runtime -- uname -m)"
tool_arch="$(kubectl -n "${NAMESPACE}" exec tool -- uname -m)"
[[ "${runtime_arch}" =~ ^(aarch64|arm64)$ && "${tool_arch}" =~ ^(aarch64|arm64)$ ]] || {
  echo "foreign-architecture image detected: runtime=${runtime_arch}, tool=${tool_arch}" >&2
  exit 1
}

tool_reply="$(kubectl -n "${NAMESPACE}" exec runtime -- wget -T 10 -qO- http://tool:8080/)"
[[ "${tool_reply}" =~ ^(aarch64|arm64)$ ]]

if kubectl -n "${NAMESPACE}" exec attacker -- wget -T 4 -qO- http://tool:8080/ >/dev/null 2>&1; then
  echo "NetworkPolicy is not enforced: attacker reached the Tool service" >&2
  exit 1
fi

runtime_boot="$(kubectl -n "${NAMESPACE}" exec runtime -- cat /proc/sys/kernel/random/boot_id)"
tool_boot="$(kubectl -n "${NAMESPACE}" exec tool -- cat /proc/sys/kernel/random/boot_id)"
[[ -n "${runtime_boot}" && -n "${tool_boot}" && "${runtime_boot}" != "${tool_boot}" ]] || {
  echo "Runtime and Tool do not expose distinct guest boot IDs; microVM isolation is unproven" >&2
  exit 1
}

echo "PASS runtimeClass=${RUNTIME_CLASS} runtime_arch=${runtime_arch} tool_arch=${tool_arch}"
echo "PASS Runtime -> Tool networking and attacker -> Tool isolation"
echo "PASS distinct guest boot IDs runtime=${runtime_boot} tool=${tool_boot}"
if [[ "${KEEP}" == true ]]; then
  echo "KEEP namespace=${NAMESPACE}; delete with: kubectl delete namespace ${NAMESPACE}"
else
  echo "PASS cleanup scheduled for namespace=${NAMESPACE}"
fi
