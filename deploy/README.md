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

`cell.yaml` is rendered by `cell.sh`; operators should never create the child
Pod, Job, Secret, Service, or NetworkPolicy resources manually.

`containerd-firecracker.toml` is installed by the host setup scripts after the
thin-pool and Kata paths are validated. It is not a standalone Kubernetes
manifest.
