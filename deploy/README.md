# Kubernetes manifests

These manifests support the native ARM64 Kata/Firecracker deployment. Replace
all example image references and Secret placeholders before applying them.

Base execution services:

```bash
kubectl apply -f deploy/runtimeclass-firecracker.yaml
kubectl apply -f deploy/runtimeclass-firecracker-ebpf.yaml
kubectl apply -f deploy/sandboxtask-crd.yaml
kubectl apply -f deploy/control-plane-rbac.yaml
kubectl apply -f /path/to/clawbox-control-plane-secret.yaml
kubectl apply -f /path/to/clawbox-llm-secret.yaml
kubectl apply -f deploy/trace-ingester.yaml
kubectl apply -f deploy/tune-kb.yaml
python3 scripts/collect-node-capacity.py --configmap | kubectl apply -f -
kubectl apply -f deploy/cell-controller.yaml
```

Before labeling a node ready, install the shim file-limit wrapper and run the
real host/VM gates:

```bash
sudo bash scripts/install-shim-nofile-wrapper.sh
bash scripts/audit-kata-firecracker-arm64.sh --root /opt/kata
bash scripts/arm64-kata-smoke.sh --runtime-class kata-fc-arm64
bash scripts/run-toolbridge-ebpf-integration.sh \
  --output .artifacts/toolbridge-ebpf-integration.log
```

`arm64-kata-smoke.sh` prompts for sudo only on an interactive terminal. Run
`sudo -v` first in automation. Native eBPF requires the
`kata-fc-arm64-ebpf` RuntimeClass, guest `SYS_ADMIN`, cgroup v2, BTF/kernel
headers, and tracefs; Tool Bridge mounts and verifies tracefs before BCC
starts. A successful gate reports `INTEGRATION_RC=0`, distinct concurrent
cgroup IDs, valid native artifacts, and zero telemetry loss.

The controller creates task-scoped SSH and upload credentials. The Tool
projection excludes the client private key and upload token; the Runtime
projection excludes the Tool host private key.

The managed API requires PostgreSQL and an Alembic migration before startup:

```bash
DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST/clawbox' alembic upgrade head
kubectl apply -f deploy/managed-rbac.yaml
kubectl apply -f /path/to/clawbox-managed-secret.yaml
kubectl apply -f deploy/managed-control-plane.yaml
```

Every `toolImage` in the template Secret must use the immutable digest printed
by `scripts/rebuild-swe-rebench-tool-overlay.sh`. Updating a registry tag does
not update an existing digest pin.

`cell.yaml` is rendered by `cell.sh`; operators should never create the child
Pod, Job, Secret, Service, or NetworkPolicy resources manually.

`containerd-firecracker.toml` is installed by the host setup scripts after the
thin-pool and Kata paths are validated. It is not a standalone Kubernetes
manifest.

To clear historical runtime failures without touching live workloads:

```bash
python3 scripts/cleanup-failed-pods.py          # report only
python3 scripts/cleanup-failed-pods.py \
  --namespace clawbox-system --apply            # Failed phase only
```
