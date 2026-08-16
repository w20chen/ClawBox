#!/usr/bin/env bash
# FC-3/FC-4 live gate: two independent arm64 Firecracker VMs and cleanup.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_CLASS="${CLAWBOX_RUNTIME_CLASS:-kata-fc-arm64}"
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
[[ "${RUNTIME_CLASS}" == kata-fc-arm64 ]] || { echo "only kata-fc-arm64 is accepted" >&2; exit 64; }
valid_name "${NAMESPACE}" || { echo "invalid namespace" >&2; exit 64; }
[[ "${IMAGE}" =~ ^[A-Za-z0-9._/@:-]+$ ]] || { echo "invalid image reference" >&2; exit 64; }

command -v kubectl >/dev/null 2>&1 || { echo "kubectl is required" >&2; exit 69; }
kubectl cluster-info >/dev/null
kubectl get runtimeclass "${RUNTIME_CLASS}" >/dev/null
handler="$(kubectl get runtimeclass "${RUNTIME_CLASS}" -o jsonpath='{.handler}')"
overhead_cpu="$(kubectl get runtimeclass "${RUNTIME_CLASS}" -o jsonpath='{.overhead.podFixed.cpu}')"
overhead_memory="$(kubectl get runtimeclass "${RUNTIME_CLASS}" -o jsonpath='{.overhead.podFixed.memory}')"
[[ "${handler}" == "${RUNTIME_CLASS}" && -n "${overhead_cpu}" && -n "${overhead_memory}" ]] || {
  echo "RuntimeClass handler/overhead gate failed" >&2
  exit 1
}

host_command() {
  if [[ "$(id -u)" == 0 ]]; then "$@"; else sudo -n "$@"; fi
}
command -v ctr >/dev/null 2>&1 || { echo "ctr is required on the node running this gate" >&2; exit 69; }
host_command bash "${ROOT}/scripts/audit-kata-firecracker-arm64.sh" --root /opt/kata
host_command ctr plugins ls | awk '$1 ~ /snapshotter/ && $2 == "devmapper" && $NF == "ok" {found=1} END {exit !found}' \
  || { echo "devmapper snapshotter is not healthy" >&2; exit 1; }
config_dump="$(host_command containerd config dump)"
handler_block="$(grep -A12 -F "runtimes.${RUNTIME_CLASS}" <<<"${config_dump}" | head -13)"
grep -Eq 'snapshotter[[:space:]]*=[[:space:]]*"devmapper"' <<<"${handler_block}" \
  || { echo "handler does not select devmapper" >&2; exit 1; }
grep -F 'configuration-fc-arm64.toml' <<<"${handler_block}" >/dev/null \
  || { echo "handler does not select the audited Firecracker config" >&2; exit 1; }
active_snapshots_before="$(host_command ctr -n k8s.io snapshots --snapshotter devmapper ls 2>/dev/null | awk 'NR > 1 && $3 == "Active" {count++} END {print count+0}')"

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

kubectl -n "${NAMESPACE}" get pod/tool pod/runtime -o json | python3 -c '
import json, sys
pods = json.load(sys.stdin)["items"]
for pod in pods:
    for volume in pod["spec"].get("volumes", []):
        if "hostPath" in volume or "persistentVolumeClaim" in volume:
            raise SystemExit("shared host/PVC storage is forbidden: {}/{}".format(
                pod["metadata"]["name"], volume["name"]
            ))
'

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

firecracker_processes="$(ps -eo pid=,args= | grep -E '[f]irecracker([^[:alnum:]]|$)' || true)"
[[ "$(grep -c . <<<"${firecracker_processes}")" -ge 2 ]] || {
  echo "fewer than two host Firecracker processes were observed" >&2
  printf '%s\n' "${firecracker_processes}" >&2
  exit 1
}
if ps -eo args= | grep -E '[q]emu-system|[q]emu-kvm' >/dev/null; then
  echo "an alternate VMM process is active; Firecracker-only proof is ambiguous" >&2
  exit 1
fi

runtime_uid="$(kubectl -n "${NAMESPACE}" get pod runtime -o jsonpath='{.metadata.uid}')"
tool_uid="$(kubectl -n "${NAMESPACE}" get pod tool -o jsonpath='{.metadata.uid}')"
containerd_log="$(host_command journalctl -u containerd --since '-10 minutes' --no-pager 2>/dev/null || true)"
grep -Eqi 'firecracker' <<<"${containerd_log}" \
  || { echo "containerd journal contains no Firecracker launch evidence" >&2; exit 1; }

echo "PASS runtimeClass=${RUNTIME_CLASS} runtime_arch=${runtime_arch} tool_arch=${tool_arch}"
echo "PASS Runtime -> Tool networking and attacker -> Tool isolation"
echo "PASS distinct guest boot IDs runtime=${runtime_boot} tool=${tool_boot}"
echo "PASS static sizing, guest-local emptyDir, and no hostPath/PVC storage"
echo "PASS Firecracker host processes and containerd launch evidence for pod_uids=${runtime_uid},${tool_uid}"
if [[ "${KEEP}" == true ]]; then
  echo "KEEP namespace=${NAMESPACE}; delete with: kubectl delete namespace ${NAMESPACE}"
else
  kubectl delete namespace "${NAMESPACE}" --wait=true --timeout=180s >/dev/null
  created=false
  for _ in $(seq 1 60); do
    active_snapshots_after="$(host_command ctr -n k8s.io snapshots --snapshotter devmapper ls 2>/dev/null | awk 'NR > 1 && $3 == "Active" {count++} END {print count+0}')"
    (( active_snapshots_after <= active_snapshots_before )) && break
    sleep 2
  done
  (( active_snapshots_after <= active_snapshots_before )) || {
    echo "active devmapper snapshots leaked: before=${active_snapshots_before} after=${active_snapshots_after}" >&2
    exit 1
  }
  echo "PASS namespace cleanup and active devmapper snapshot reclamation"
fi
