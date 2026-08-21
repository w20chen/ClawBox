# Kubernetes deployment reference

The root [README](../README.md) is the canonical operator procedure. This
file explains the manifests and their required ordering. ClawBox has one
supported production architecture: native ARM64 Kubernetes with
Kata/Firecracker.

## Never apply development image tags as a release

Source manifests contain local development image names. Build and push the
three ARM64 platform images, then render immutable manifests:

```bash
python3 scripts/render-kubernetes-images.py \
  --control-image 'REGISTRY/control-plane-arm64@sha256:DIGEST' \
  --runtime-image 'REGISTRY/runtime-arm64@sha256:DIGEST' \
  --tool-bridge-image 'REGISTRY/tool-bridge-arm64@sha256:DIGEST'
```

Apply `.artifacts/rendered-deploy/*.yaml`; do not deploy a mutable `:dev` or
`:latest` tag and assume it represents the current source.

## Resources and ownership

| File | Creates | Prerequisite |
| --- | --- | --- |
| `runtimeclass-firecracker.yaml` | base Kata RuntimeClass | audited host artifacts |
| `runtimeclass-firecracker-ebpf.yaml` | Tool-VM eBPF RuntimeClass | eBPF guest configuration |
| `sandboxtask-crd.yaml` | served/storage `v1alpha1` CRD | API extensions available |
| `control-plane-rbac.yaml` | namespaces and Cell Controller RBAC | cluster-admin apply |
| `managed-rbac.yaml` | Managed API/Dispatcher RBAC | cluster-admin apply |
| rendered `trace-ingester.yaml` | trace Deployment/Service | control-plane Secret |
| rendered `tune-kb.yaml` | KB Deployment/Service | control-plane Secret |
| rendered `cell-controller.yaml` | Cell Controller | RuntimeClasses, capacity ConfigMap |
| rendered `managed-control-plane.yaml` | Managed API/Dispatcher | migrated PostgreSQL, managed Secret |

The Cell Controller owns task child Pods, Jobs, Secrets, Services, and
NetworkPolicies. Operators should create or delete the parent `SandboxTask`,
not its children.

## Secrets

| Name | Namespace | Purpose |
| --- | --- | --- |
| `clawbox-control-plane` | `clawbox-system` | database URL, API token, telemetry HMAC secret |
| `clawbox-managed` | `clawbox-system` | managed database URL/token and template policy |
| `clawbox-llm` | `clawbox-benchmarks` | task-scoped LLM configuration source |

Copy the matching `*.example.yaml` files to `/tmp`, replace every placeholder,
validate with `kubectl apply --dry-run=client`, and never commit real values.
The template policy must use immutable Tool and Runtime image digests.

## Required apply order

```bash
kubectl apply -f deploy/runtimeclass-firecracker.yaml
kubectl apply -f deploy/runtimeclass-firecracker-ebpf.yaml
kubectl apply -f deploy/sandboxtask-crd.yaml
kubectl apply -f deploy/control-plane-rbac.yaml
kubectl apply -f deploy/managed-rbac.yaml
kubectl apply -f /tmp/clawbox-control-plane.yaml
kubectl apply -f /tmp/clawbox-managed.yaml
kubectl apply -f /tmp/clawbox-llm.yaml
python3 scripts/collect-node-capacity.py --configmap | kubectl apply -f -
kubectl apply -f .artifacts/rendered-deploy/trace-ingester.yaml
kubectl apply -f .artifacts/rendered-deploy/tune-kb.yaml
kubectl apply -f .artifacts/rendered-deploy/cell-controller.yaml
kubectl apply -f .artifacts/rendered-deploy/managed-control-plane.yaml
```

Run `alembic upgrade head` against the exact Managed API database before the
matching API image starts. The Dispatcher currently targets `v1alpha1`; the
separate `v1alpha2` and conversion-webhook files are not part of the canonical
deployment.

## Persistence

- Trace ingestion uses `DATABASE_URL` from `clawbox-control-plane` and also
  mounts the single-node host path `/var/lib/clawbox/ingester`.
- The default tuning KB uses SQLite at `/var/lib/clawbox/tune-kb`.
- Managed run state uses PostgreSQL from `clawbox-managed`.

Back up these stores before an upgrade. The supplied host paths are accepted
for the validated single-node deployment; a multi-node storage design is not
provided.

## Host and runtime acceptance

Run privileged host checks only after refreshing sudo:

```bash
sudo -v
sudo bash scripts/bootstrap-openeuler-arm64.sh status
sudo bash deploy/check-host.sh --runtime-class kata-fc-arm64
bash scripts/arm64-kata-smoke.sh --runtime-class kata-fc-arm64
bash scripts/run-toolbridge-ebpf-integration.sh \
  --image 'REGISTRY/task@sha256:DIGEST' \
  --output .artifacts/toolbridge-ebpf-integration.log
```

Native eBPF requires `kata-fc-arm64-ebpf`, guest `SYS_ADMIN`, cgroup v2,
BTF/kernel headers, and tracefs. A successful integration reports
`INTEGRATION_RC=0`, distinct concurrent cgroup IDs, valid native artifacts,
and zero telemetry loss.

## Upgrade rule

Do not rerun destructive host bootstrap on an initialized machine. Create a
clean detached release worktree, build new digest-pinned images, migrate the
database, apply rendered manifests, wait for rollout, and rerun acceptance.

## Cleanup

Inspect first, then remove only Failed Pods in an explicit namespace:

```bash
python3 scripts/cleanup-failed-pods.py
python3 scripts/cleanup-failed-pods.py --namespace clawbox-system --apply
```

Cleaning unrelated namespaces requires separate operator review. The cleanup
script never selects Running, Pending, or Succeeded Pods.
