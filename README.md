# ClawBox: Firecracker-first ARM64 benchmark runtime

ClawBox runs a dual-VM Cell per `SandboxTask` on Kunpeng/openEuler ARM64 hosts. The Tool Pod hosts the immutable SWE-ReBench task image and a static SSH Tool Bridge; the Runtime Pod hosts OpenClaw and runs ClawTune as a native Kubernetes sidecar. Both Pods are pinned to `kata-fc-arm64`, with no fallback path to other architectures or VMMs.

```text
SandboxTask
  ├─ Tool Pod (Kata + Firecracker) ── SSH/2222 ──┐
  └─ Runtime Job/Pod (Kata + Firecracker)        │
       ├─ runtime: OpenClaw ─────────────────────┘
       └─ native sidecar: ClawTune observe-only
              └─ task token → central trace/result ingester
```

This repository contains the full software implementation, fail-closed host gates, and automated tests. Real `/dev/kvm`, two dedicated disks, and Firecracker microVMs can only be validated on a target Kunpeng host; a deployment should not be marked production-passing unless the target-machine commands have been run.

## Fixed boundaries

- Kata Containers `3.31.0`, Firecracker `1.12.1`, Linux `arm64` guest/host.
- containerd 2.x `devmapper` snapshotter; loop devices are forbidden in production.
- The RuntimeClass name and handler are both `kata-fc-arm64`, and per-VM Pod overhead is declared.
- Every task must map to a `linux/arm64` registry digest; `unsupported-arm64` blocks submission.
- The Tool Pod only receives a task-specific SSH public key/host key, never LLM or upload credentials.
- Runtime and Tool share no hostPath, PVC, or RWX; results and traces are uploaded via a short-lived, task-scoped token.
- ClawTune is only enabled with `observe-only` and `hook-only`; cgroup, affinity, and NUMA control are disabled.

## 1. Host and Firecracker

Start with a read-only plan on the target machine; `apply` installs Kubernetes/containerd/Kata and only initializes the two dedicated disks when given a confirmation string that exactly matches the canonical device names:

```bash
bash scripts/bootstrap-openeuler-arm64.sh plan \
  --devmapper-data-device /dev/DATA_DISK \
  --devmapper-meta-device /dev/META_DISK

sudo bash scripts/bootstrap-openeuler-arm64.sh apply \
  --devmapper-data-device /dev/DATA_DISK \
  --devmapper-meta-device /dev/META_DISK \
  --confirm-erase /dev/DATA_DISK,/dev/META_DISK
```

If you prefer to run the steps separately, use:

```bash
bash scripts/audit-kata-firecracker-arm64.sh --root /opt/kata
bash scripts/build-kata-firecracker-arm64.sh plan
sudo bash scripts/setup-devmapper-openeuler-arm64.sh status
bash deploy/check-host.sh --runtime-class kata-fc-arm64
bash scripts/arm64-kata-smoke.sh --runtime-class kata-fc-arm64
```

Only after the last two checks pass should a node be labeled `clawbox.openai.com/firecracker-ready=true`. See [docs/OPENEUER_ARM64.md](docs/OPENEUER_ARM64.md) for detailed steps and rollback boundaries.

## 2. ARM64 images

All builds must run on a native ARM64 Docker daemon; detection of a foreign-architecture binfmt handler fails the build outright.

```bash
REGISTRY=registry.example.com/clawbox PUSH=1 \
  CLAWTUNE_ROOT=/src/ClawTune \
  bash scripts/build-kubernetes-images.sh
```

The 128 SWE-ReBench images are built from the install recipes in the full dataset and a pinned SWE-bench fork. First pin the fork to the factory default commit:

```bash
git clone https://github.com/SWE-rebench/SWE-bench-fork.git /src/SWE-bench-fork
git -C /src/SWE-bench-fork checkout 980d0cca8aa4e73f1d9f894e906370bef8c4de8a
```

`--selection` refers to the 128 tasks selected by ClawTune. You can use a full local export that includes `install_config`:

```bash
python3 scripts/build-swe-rebench-arm64.py \
  --dataset /data/swe-rebench-full.parquet \
  --selection /src/ClawTune/swe_rebench/tasks.json \
  --swebench-root /src/SWE-bench-fork \
  --registry registry.example.com/clawbox \
  --mapping /data/swe-rebench-arm64-map.json \
  --tool-bridge-binary .artifacts/tool-bridge-arm64/tool-bridge \
  --push --fail-fast
```

Alternatively, you can have the factory read the official dataset at a pinned revision:

```bash
python3 scripts/build-swe-rebench-arm64.py \
  --dataset-id nebius/SWE-rebench \
  --dataset-revision 4ece23ba02fe8b68858e430134adddfd64d6f0f4 \
  --selection /src/ClawTune/swe_rebench/tasks.json \
  --swebench-root /src/SWE-bench-fork \
  --registry registry.example.com/clawbox \
  --mapping /data/swe-rebench-arm64-map.json \
  --tool-bridge-binary .artifacts/tool-bridge-arm64/tool-bridge \
  --push --fail-fast
```

The factory validates `/testbed`, the shell, git, patches, dependency/test commands, and the ARM64 Tool Bridge before writing a mapping that records `platform`, `recipe_revision`, and the registry digest. Any entry that cannot be built natively is recorded as `unsupported-arm64`, and the launcher does not allow fallback.

Before reading Parquet or remote datasets from Hugging Face, install the optional image-factory dependencies: `python3 -m pip install -e '.[images]'`.

## 3. Control plane and tasks

Replace the example image names with real digests/tags and create the `clawbox-control-plane` and `clawbox-llm` Secrets, then deploy:

```bash
kubectl apply -f deploy/sandboxtask-crd.yaml
kubectl apply -f deploy/control-plane-rbac.yaml
kubectl apply -f deploy/trace-ingester.yaml
python3 scripts/collect-node-capacity.py --configmap | kubectl apply -f -
kubectl apply -f deploy/cell-controller.yaml
```

A template for `clawbox-control-plane` is in `deploy/control-plane-secret.example.yaml`; the keys for `clawbox-llm` are in `deploy/swe-rebench-secret.example.yaml`. The placeholder values in both templates must be replaced, and a production ingester should use persistent PostgreSQL.

Submit a single template:

```bash
TASK_ID=demo TOOL_IMAGE='registry.example.com/task@sha256:…' \
LLM_EGRESS_CIDR=203.0.113.10/32 bash deploy/cell.sh render | kubectl apply -f -
kubectl get sandboxtasks -n clawbox-benchmarks -w
```

Run the dataset:

```bash
bash scripts/run-swe-rebench.sh \
  --tasks /src/ClawTune/swe_rebench/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-cidr 203.0.113.10/32 \
  --parallelism 8
```

The controller drives each task strictly through `Queued → Admitted → ToolStarting → ToolReady → RuntimeRunning → Collecting → Succeeded/Failed/TimedOut → Cleaned`. If the full dual-VM budget is not available, the task stays `Queued` and never creates a half-built Cell.

## 4. Capacity and incremental load testing

The `small`, `medium`, and `large` profiles account for the Runtime, Tool, ClawTune sidecar, both RuntimeClass overheads, and a 10% safety margin. Node capacity is derived from the allocatable resources of ready ARM64 nodes, minus non-Cell Pod requests, and is bounded by the devmapper baseline budget.

```bash
python3 scripts/collect-node-capacity.py
bash scripts/scale-swe-rebench.sh \
  --tasks /src/ClawTune/swe_rebench/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-cidr 203.0.113.10/32
```

Load testing ramps in fixed steps of `1, 2, 4, 8, 16, 32` and stops on the first task failure or a thin-pool pressure gate failure. See [docs/IMPLEMENTATION_MAPPING.md](docs/IMPLEMENTATION_MAPPING.md) for an index of architecture and implementation files.

## Local verification

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest
python3 -m py_compile $(find clawbox scripts -name '*.py')
```

Local tests are not a substitute for target-machine acceptance; production-passing evidence must include the host gates, a dual-Pod smoke test, Firecracker host processes/journal, and devmapper cleanup results.
