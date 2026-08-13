# Concurrent Kubernetes + Kata/Firecracker SWE-Rebench MVP

## Local quick start

From a machine with no current cluster, Ubuntu users can bootstrap KVM/libvirt, a kvm2 Minikube,
Kata, and the first task in one command:

```bash
bash scripts/local-kata-swe.sh \
  --bootstrap-minikube \
  --api-key-file ~/llm-api-key.txt \
  --base-url https://api.example.com/v1 \
  --model provider-model
```

The host must already expose `/dev/kvm`; the script does not change BIOS or nested virtualization
settings.

For a local kind, minikube, k3d, k3s, or containerd-backed cluster, use the automated path:

```bash
cd ~/ClawBox
bash scripts/local-kata-swe.sh \
  --api-key-file ~/llm-api-key.txt \
  --base-url https://api.example.com/v1 \
  --model provider-model \
  --install-kata
```

It prepares the bundle when missing, builds and imports the image, applies manifests, creates the
Secret, boots a `kata-fc` smoke microVM, and runs one task. On Minikube it also creates the
persistent containerd devmapper thin pool required by Firecracker and installs a systemd unit that
reattaches the same pool after a VM restart. The backing files are sparse (100 GB data, 2 GB
metadata), but real writes still consume Minikube's host disk. Repeat runs reuse the pool, bundle,
image and Secret. Use `--rebuild` after ClawTune changes. Useful commands:

```bash
bash scripts/local-kata-swe.sh --sample 8 --parallelism 4 --cpu 4 --memory 8Gi
bash scripts/local-kata-swe.sh status
bash scripts/local-kata-swe.sh logs
bash scripts/local-kata-swe.sh smoke
```

## Decision: reuse ClawTune at a versioned bundle boundary

ClawBox does not import the complete ClawTune application at runtime and does not fork its
SWE-Rebench implementation. ClawTune remains the source of truth for the generated
`swe_rebench/.runtime/bundle`: OpenClaw, its plugin, sidecar, setup and task entrypoint. ClawBox packages
that generated bundle into an immutable image and injects it into each task Pod with an init
container. The SWE-Rebench task image remains the main container image.

This split gives every task (and therefore every benchmark tenant) one isolated OpenClaw runtime,
while ClawBox owns Kubernetes concurrency and lifecycle. Predictions, KB sharing, custom
scheduling and eBPF are intentionally disabled on this first path.

## Runtime topology

```text
clawbox-swe-rebench --parallelism N
  -> N Kubernetes Jobs (bounded client-side)
       -> runtimeClassName: kata-fc
       -> init: copy ClawTune bundle image to /claw
       -> main: SWE-Rebench task image + /claw/entrypoint.sh
       -> one local OpenClaw + sidecar per task/tenant
       -> /traces/<task-id> on an optional RWX PVC
```

Kata Containers is the Kubernetes runtime integration and the `kata-fc` handler selects its
Firecracker hypervisor configuration. ClawBox does not start Firecracker directly.

## Build

On the Linux build host, generate the ClawTune bundle and build images:

```bash
cd ../ClawTune
python3 -m swe_rebench.runner prepare

cd ../ClawBox
export REGISTRY=registry.example.com/clawbox
export TAG=dev
PUSH=1 bash scripts/build-kubernetes-images.sh
```

`registry.example.com/clawbox` is only a placeholder. Replace it with a registry reachable by
every Kubernetes node, for example `ghcr.io/<owner>/clawbox`. For a registry-free local cluster,
set a local image name, use `PUSH=0`, then import all images into the cluster nodes explicitly.

The build uses the sibling `ClawTune` checkout as a named Docker build context. No ClawTune file
is copied into this repository.

## Cluster prerequisites

The nodes must have containerd, Kata Containers, Firecracker/KVM and a configured `kata-fc`
handler. Verify before deployment:

```bash
bash deploy/check-host.sh
kubectl apply -f deploy/runtimeclass.yaml
kubectl apply -f deploy/control-plane-rbac.yaml
kubectl apply -f deploy/benchmark-networkpolicy.yaml
```

Firecracker requires a block-device rootfs. The Kata Deploy chart therefore maps `kata-fc` to the
containerd `devmapper` snapshotter, which the operator must preconfigure. The automated Minikube
path does this before Helm and refuses to continue unless `ctr plugins ls` reports devmapper as
`ok`. For non-Minikube clusters, provision a production thin pool on every eligible node; the
loopback pool used here is intended for local development only.

Create the LLM secret without putting its key in shell history or Git. The example documents the
required keys but must not be applied unchanged:

```bash
kubectl -n clawbox-benchmarks create secret generic clawbox-llm \
  --from-file=llm-api-key=/secure/path/api-key \
  --from-literal=llm-upstream-base-url=https://api.example.com/v1 \
  --from-literal=llm-model=provider-model \
  --from-literal=openclaw-model-ref=vllm/provider-model
```

For trace retention, provision an RWX PVC named `clawbox-traces`. Without `--trace-pvc`, traces
are ephemeral and disappear when the Job is deleted.

## Run concurrent tasks

The task file uses the same common fields as ClawTune: `instance_id`, `image`, and
`problem_statement` (with accepted aliases `task_id`, `docker_image`, and `problem`).

```bash
python3 -m pip install -e .
bash scripts/run-swe-rebench.sh \
  --tasks ../ClawTune/swe_rebench/tasks.json \
  --sample 8 \
  --parallelism 4 \
  --bundle-image registry.example.com/clawbox/clawtune-swe-bundle:dev \
  --trace-pvc clawbox-traces
```

The wrapper runs from the source checkout and gives a direct dependency error. It also checks that
the namespace, all four Secret keys, and an optional trace PVC exist before creating any Job. After installation,
the equivalent console command is `clawbox-swe-rebench`. `/data/swe-rebench/tasks.json` in earlier
examples was a placeholder; use the actual ClawTune dataset path or another existing task file.

Inspect a task:

```bash
kubectl -n clawbox-benchmarks get jobs,pods -l app.kubernetes.io/name=clawbox-swe-rebench
kubectl -n clawbox-benchmarks logs job/<job-name> -c openclaw-runtime
```

## Controller backend

Set `CONTROLLER_BACKEND=kubernetes` on the existing Controller service to create sticky Tool
Agent Pods instead of Docker containers. It creates one namespace per tenant, uses `kata-fc`,
sets equal CPU/memory requests and limits, waits for readiness, and stores the Pod/Service identity
in the existing database. Apply `deploy/control-plane-rbac.yaml` and run the Controller with the
`clawbox-controller` ServiceAccount when it runs inside the cluster.

## Current verification boundary

Unit tests validate the Pod/Job contract, secret references, RuntimeClass, bundle injection and
Guaranteed QoS shape. A real end-to-end pass requires a Linux KVM host and a functioning
`kata-fc` RuntimeClass; Windows development machines cannot validate Firecracker boot.
