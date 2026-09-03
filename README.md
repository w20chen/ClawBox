# ClawBox

ClawBox runs reproducible coding-agent experiments on a dedicated ARM64
Kubernetes node. CubeSandbox is the only sandbox runtime.

## Architecture

```text
clawbox experiment
  -> ClawBox API -> Run / Attempt / transactional Outbox
  -> one SandboxTask -> thin controller -> one ExperimentWorker Job
  -> one trusted Runtime VM (OpenClaw + native ClawTune + model access)
  -> authenticated cube_shell bridge -> trusted Worker
  -> one in-process PolicyCoordinator per arm -> official CubeSandbox Python SDK
  -> one untrusted Tool VM (/workspace + command/resource telemetry)
  -> ClawTune v6 spans + exact-ID Cube execution records -> offline KB/p90
```

The trusted Worker owns replay orchestration, policy decisions, validation,
and result collection. Each logical Agent owns exactly two CubeSandbox ARM64
MicroVMs: a Runtime VM for OpenClaw, reasoning state, native ClawTune, and model
access; and a Tool VM for `/workspace` plus every shell, repository, file,
compilation, generated-program, and test operation. Model credentials are
injected only into the Runtime VM and never into the Tool VM. Kubernetes places
the Worker; the Worker constrains both Cube sandboxes to that same node.

OpenClaw has only the required `cube_shell` tool in experiment sessions; its
host-local file, shell, process, browser, and patch tools are denied. The Worker
bridge is authenticated with a per-session random token. The Runtime VM gets a
narrow CubeSandbox egress allow rule for only the Worker Pod IP; the Tool VM
gets no bridge or model credential.

ClawTune observes this trusted boundary. Each completed `cube_shell` call emits
a Runtime-VM native ClawTune span and a matching Worker bridge record, Tool-VM
cgroup snapshot, and eBPF clause artifact under the same execution ID. The
Tool VM starts the guest collector; every command enters a dedicated cgroup
before its execution gate opens. Missing kernel support or collector failures
are recorded explicitly and never replaced with fabricated resource values.
Runtime-side ClawTune predictors consume the cold-start or control-plane KB and
proxy model calls, but never execute commands or own sandbox lifecycle policy.

There is no selectable sandbox backend, SSH tool transport, ClawBox node agent,
pool manager, custom scheduler, Kata execution path, or direct Firecracker
execution path in the supported interface.

## Public interface

```bash
clawbox experiment validate examples/experiments/vertical-slice.yaml
clawbox experiment plan examples/experiments/vertical-slice.yaml
clawbox experiment run examples/experiments/vertical-slice.yaml --project PROJECT_ID
clawbox experiment status RUN_ID
clawbox experiment cancel RUN_ID
clawbox experiment collect RUN_ID
```

`run`, `status`, `cancel`, and `collect` use the managed API. Retry is an API
Run/Attempt operation; Kubernetes Jobs use `backoffLimit: 0` and
`restartPolicy: Never`.

## Kunpeng installation

Requirements are an ARM64 Kubernetes node with native `/dev/kvm`, cgroup v2,
and at least 210 GiB free under `/data` when the installer creates its default
200 GiB reflink XFS loopback allocation. Inspect before changing the host:

```bash
bash scripts/install-cubesandbox-kunpeng920.sh check
```

Install the pinned CubeSandbox release and its Kubernetes components:

```bash
export CUBE_MYSQL_PASSWORD='...'
export CUBE_MYSQL_ROOT_PASSWORD='...'
export CUBE_REDIS_PASSWORD='...'
export CUBE_USE_CN_MIRROR=1        # optional
bash scripts/install-cubesandbox-kunpeng920.sh install
```

The installer pins CubeSandbox `v0.7.0` at commit
`d0081641c59822e4e5653b7462e914410b81910a`, configures 1.0 CPU/memory
overcommit ratios and full paused-resource release, and installs cluster DNS for
`*.cube.local`. It does not patch or replace the host kernel.

Register the known ARM64 task template and run the real lifecycle smoke test:

```bash
python scripts/register-cube-template.py \
  --image cube-sandbox-cn.tencentcloudcr.com/cube-sandbox/sandbox-code@sha256:e1cb43e12ba70b8453b45f0c063306faab8a6974aa3fd76982dc4d019d07c60d \
  --alias clawbox-task-arm64-4g --memory-mib 4096

CUBE_API_URL=http://127.0.0.1:30030 \
python scripts/smoke-cubesandbox-kunpeng920.py \
  --template tpl-c7212cdc724844639aa65486
```

Deploy the CRD and controller after publishing immutable ARM64 Worker and
control-plane images to the node registry:

```bash
kubectl apply -f deploy/sandboxtask-crd.yaml
kubectl apply -f deploy/cell-rbac.yaml
kubectl apply -f deploy/cell-controller.yaml
kubectl apply -f deploy/sandboxtask-vertical-slice.yaml
kubectl -n clawbox-benchmarks get sandboxtask,job,pod
```

Deployment examples contain machine-specific node names, template IDs, and
immutable image digests; resolve and update them when targeting another node.

## Experiment specification and results

Schema v2 has no runtime or transport selector. It expands workload case ×
repetition × concurrency × explicit policy tuple, randomizes arms with the
recorded seed, and executes arms sequentially. See
`examples/experiments/vertical-slice.yaml` and `smoke-matrix.yaml`.

Each arm is persisted atomically before its completion marker. A run emits
per-arm JSON, JSONL events, ClawTune-compatible `traces/*.jsonl` and
`tool-bridge.jsonl`, `summary.json`, `summary.csv`, and `summary.md`.
Results include the resolved policy, correctness, latency, lifecycle, physical
node-memory/storage measurements, SandboxTask identity, and pinned Cube/template
provenance. Failed or partial arms are rerun rather than reused.

Build an offline ClawTune dataset and evaluation from one or more completed run
directories with:

```bash
python -m clawbox.tuning /data/clawbox-results/RUN_ID \
  --output-dir /data/clawbox-results/clawtune-analysis
```

The optional `clawbox-tune-server` serves the validated tenant/repository KB.
Experiment specifications can consume an immutable exported p90 JSON through
the `tool_p90` admission policy; it is measurement-derived policy input, not a
second runtime control plane.

## Verification

```bash
python -m pip install -e '.[dev,postgres]'
python -m pytest -q
```

The synthetic Cube client is used only by unit tests. Runtime smoke and matrix
claims require execution against the real Kunpeng CubeSandbox deployment.
