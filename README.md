# ClawBox

ClawBox runs coding-agent tasks in two isolated ARM64 Firecracker microVMs:
one Runtime VM for the agent and one Tool VM for the repository and command
execution. It provides tenant-scoped submission, immutable task images,
bounded resource profiles, durable result/trace upload, native command
telemetry, and shadow resource prediction.

## What is supported

- Native ARM64 Kubernetes nodes with Kata Containers and Firecracker.
- One Runtime VM and one Tool VM per task, admitted as one capacity unit.
- Immutable `image@sha256:...` task images; unsupported architectures fail
  closed.
- Tenant-scoped, idempotent run submission through an HTTP API.
- Direct `SandboxTask` submission for cluster debugging and benchmark runs.
- Task-specific SSH keys, isolated NetworkPolicies, and separate Runtime/Tool
  credentials.
- SWE-ReBench ARM64 image building and parallel benchmark submission.
- Per-execution cgroup-v2 CPU, memory, disk, timeout, exit, and process-tree
  measurements inside the Tool VM.
- Native ClawTune eBPF clause telemetry inside the Tool guest kernel,
  including pipelines and concurrent commands.
- Signed, immutable, tenant/repository-scoped native telemetry ingestion.
- Atomic `ClauseResourceKB` and `RuntimeToolResourceKB` generations.
- Shadow predictions that identify their KB generation and evidence run, then
  join predicted values to real Tool-VM measurements.

Resource predictions do not control task sizing. `FixedProfileSizer` remains
authoritative while prediction quality is evaluated.

## Architecture

```text
Managed API ──> Dispatcher ──> SandboxTask ──> Cell Controller
                                           ├── Tool Firecracker VM
                                           │   └── task image + Tool Bridge
                                           └── Runtime Firecracker VM
                                               └── OpenClaw + ClawTune

Tool VM telemetry ──> signed native artifacts ──> Tuning API ──> atomic KB
                                                               └── next run
```

The eBPF collector runs inside the Tool VM. Moving it to the Kubernetes host
or Runtime VM would lose command-level process attribution.

## Requirements

The production path requires:

- an ARM64 Linux host with hardware virtualization;
- Kubernetes and containerd;
- two dedicated block devices for the devmapper thin pool;
- Kata Containers 3.31.0 and Firecracker 1.12.1;
- Docker with Buildx on the native ARM64 builder;
- a sibling ClawTune checkout at `../ClawTune`;
- a registry reachable by containerd;
- Python 3.12+ for the client and build tools.

Do not initialize disks until the bootstrap plan shows the intended device
names. The apply command erases both supplied devices.

## Install the client and run tests

```bash
git clone https://github.com/w20chen/ClawBox.git
git clone https://github.com/w20chen/ClawTune.git
cd ClawBox

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev,postgres]'
python3 -m pytest -q
```

ClawTune must stay beside ClawBox because the image builds use it as a Docker
build context.

## Local Docker check

This checks the API, scheduler, allocator, Docker execution path, telemetry
join, KB update, and lease release. It does not test Firecracker.

```bash
bash scripts/linux-deploy.sh all
bash scripts/linux-deploy.sh status
```

Useful follow-up commands:

```bash
bash scripts/linux-deploy.sh logs
bash scripts/linux-deploy.sh down    # preserves the database volume
```

## Prepare an ARM64 Firecracker host

Run the read-only plan first:

```bash
bash scripts/bootstrap-openeuler-arm64.sh plan \
  --devmapper-data-device /dev/DATA_DISK \
  --devmapper-meta-device /dev/META_DISK
```

Apply only after checking the canonical paths printed by the plan:

```bash
sudo bash scripts/bootstrap-openeuler-arm64.sh apply \
  --devmapper-data-device /dev/DATA_DISK \
  --devmapper-meta-device /dev/META_DISK \
  --confirm-erase /dev/DATA_DISK,/dev/META_DISK
```

Install the isolated Tool-VM eBPF runtime and run both host gates:

```bash
bash scripts/install-ebpf-kata-runtime.sh apply
sudo bash scripts/install-shim-nofile-wrapper.sh
bash deploy/check-host.sh --runtime-class kata-fc-arm64
bash scripts/arm64-kata-smoke.sh --runtime-class kata-fc-arm64
```

The shim wrapper must report a soft `nofile` limit of at least `8192`
(`524288` on the validated host). The smoke test uses cached passwordless sudo
when available and otherwise prompts on an interactive terminal. For a
non-interactive session, run `sudo -v` first so the narrowly scoped `ctr`
preflight can access containerd.

The node should be labeled ready only after both gates pass:

```bash
kubectl label node "$(hostname)" \
  clawbox.openai.com/firecracker-ready=true --overwrite
```

## Build and push the platform images

Build on the native ARM64 host. Set mirrors only when the default registries
are not reachable.

```bash
export REGISTRY=127.0.0.1:5000/clawbox
export CLAWTUNE_ROOT="$PWD/../ClawTune"
export PUSH=1

# Optional regional mirrors:
# export GOPROXY=https://goproxy.cn,direct
# export NPM_REGISTRY=https://registry.npmmirror.com
# export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

bash scripts/build-kubernetes-images.sh
```

This builds the control-plane, Runtime, Tool Bridge, and static ARM64 Tool
Bridge binary. Pin released images by digest in your deployment manifests.

### Build SWE-ReBench task images

Install the image-factory dependency and provide a pinned task selection,
dataset, SWE-bench checkout, and registry:

```bash
python3 -m pip install -e '.[images]'

python3 scripts/build-swe-rebench-arm64.py \
  --dataset /data/swe-rebench.parquet \
  --selection ../ClawTune/swe_rebench/tasks.json \
  --swebench-root /src/SWE-bench-fork \
  --registry registry.example.com/clawbox \
  --mapping /data/swe-rebench-arm64-map.json \
  --tool-bridge-binary .artifacts/tool-bridge-arm64/tool-bridge \
  --push --fail-fast
```

The mapping records supported images as native ARM64 immutable digests.
Submission rejects missing or mutable mappings.

To add the production eBPF collector to an existing task image:

```bash
export BASE_IMAGE='registry.example.com/task@sha256:BASE_DIGEST'
export REGISTRY='registry.example.com/clawbox'
export TAG='tool-telemetry-20260821'
export CLAWTUNE_ROOT="$PWD/../ClawTune"
bash scripts/rebuild-swe-rebench-tool-overlay.sh
```

The command prints the final immutable `TOOL_IMAGE=...@sha256:...` value.
Put that exact digest in the SWE-ReBench mapping or managed template Secret;
do not leave a previous Tool Bridge digest pinned after rebuilding telemetry.

## Deploy the cluster services

Copy the example Secrets, replace every placeholder, and apply them. Do not
commit the resulting files.

```bash
cp deploy/control-plane-secret.example.yaml /tmp/clawbox-control-plane.yaml
cp deploy/swe-rebench-secret.example.yaml /tmp/clawbox-llm.yaml
${EDITOR:-vi} /tmp/clawbox-control-plane.yaml
${EDITOR:-vi} /tmp/clawbox-llm.yaml

kubectl apply -f deploy/runtimeclass-firecracker.yaml
kubectl apply -f deploy/runtimeclass-firecracker-ebpf.yaml
kubectl apply -f deploy/sandboxtask-crd.yaml
kubectl apply -f deploy/control-plane-rbac.yaml
kubectl apply -f /tmp/clawbox-control-plane.yaml
kubectl apply -f /tmp/clawbox-llm.yaml
kubectl apply -f deploy/trace-ingester.yaml
kubectl apply -f deploy/tune-kb.yaml
python3 scripts/collect-node-capacity.py --configmap | kubectl apply -f -
kubectl apply -f deploy/cell-controller.yaml
kubectl -n clawbox-system rollout status deployment/clawbox-cell-controller
```

Replace the example image references in the manifests with images from your
registry before applying them.

### Deploy the tenant-scoped API

The API and dispatcher require PostgreSQL. Run migrations first, then apply
the API Secret and workloads:

```bash
export DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST/clawbox'
alembic upgrade head

cp deploy/managed-secret.example.yaml /tmp/clawbox-managed.yaml
${EDITOR:-vi} /tmp/clawbox-managed.yaml
kubectl apply -f deploy/managed-rbac.yaml
kubectl apply -f /tmp/clawbox-managed.yaml
kubectl apply -f deploy/managed-control-plane.yaml
kubectl -n clawbox-system rollout status deployment/clawbox-managed-api
kubectl -n clawbox-system rollout status deployment/clawbox-managed-dispatcher
```

For local access:

```bash
kubectl -n clawbox-system port-forward service/clawbox-managed-api 8085:8085
```

The supplied dispatcher targets the currently served `v1alpha1` CRD. Do not
switch it to `v1alpha2` until the controller and conversion webhook support
that version end to end.

## Submit and manage a run

Set the API connection once. Prefer the environment variable so the token is
not stored in shell history:

```bash
export CLAWBOX_API_URL=http://127.0.0.1:8085
export CLAWBOX_TOKEN='replace-with-service-token'
export CLAWBOX_TENANT='team-a'
```

Submit one task and watch it:

```bash
scripts/clawbox submit \
  --project demo \
  --template swe-rebench-arm64 \
  --template-revision 1 \
  --input-ref 15five__scim2-filter-parser-13 \
  --problem-file ./problem.txt \
  --deadline-seconds 1800 \
  --idempotency-key "demo-$(date +%s)" \
  --watch
```

Inspect or operate on the returned run ID:

```bash
scripts/clawbox status RUN_ID
scripts/clawbox attempts RUN_ID
scripts/clawbox events RUN_ID
scripts/clawbox watch RUN_ID --timeout 2700
scripts/clawbox cancel RUN_ID
scripts/clawbox retry RUN_ID
```

The API records cancel intent immediately, the dispatcher deletes the bound
`SandboxTask`, and the controller removes task-owned child resources. A
cancelled run remains terminal and cannot later be overwritten as succeeded.

### Direct cluster submission

For cluster debugging without the API, create one `SandboxTask` directly:

```bash
bash deploy/cell.sh deploy \
  --task demo-001 \
  --tool-image 'registry.example.com/task@sha256:DIGEST' \
  --problem-file ./problem.txt \
  --llm-egress-cidr 203.0.113.0/24 \
  --profile small \
  --timeout-seconds 1800

kubectl -n clawbox-benchmarks get sandboxtask demo-001 -w
```

Delete only the named task when finished:

```bash
bash deploy/cell.sh delete --task demo-001
```

## Simulate many users

The load driver creates unique tenant scopes and idempotency keys, submits
requests concurrently, reports intake throughput, and can watch all dispatched
runs. Ten tenants with two runs each:

```bash
export CLAWBOX_TOKEN='replace-with-service-token'

scripts/load-test.sh \
  --api-url http://127.0.0.1:8085 \
  --tenant-count 10 \
  --tenant-prefix team \
  --runs-per-tenant 2 \
  --submit-workers 20 \
  --arrival-rate 5 \
  --template swe-rebench-arm64 \
  --input-ref load-test-repository \
  --problem-file ./problem.txt \
  --deadline-seconds 900 \
  --watch --watch-timeout 1800 \
  --output-json .artifacts/load-test.json
```

Use `--tenants tenant-a tenant-b tenant-c` instead of `--tenant-count` when
you need explicit tenant IDs. Add `--require-success` in CI to fail on a watch
timeout or terminal task failure. Omit `--watch` to measure API submission and
idempotency throughput without waiting for VM execution.

## Run a benchmark set

Submit an ARM64-mapped SWE-ReBench selection directly to Kubernetes:

```bash
bash scripts/run-swe-rebench.sh \
  --tasks ../ClawTune/swe_rebench/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-cidr 203.0.113.0/24 \
  --parallelism 8 \
  --timeout-seconds 1800
```

Run a measured scale ladder by setting the desired steps:

```bash
CLAWBOX_SCALE_STEPS='1 2 4 8' \
bash scripts/scale-swe-rebench.sh \
  --tasks ../ClawTune/swe_rebench/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-cidr 203.0.113.0/24
```

The scale script stops at the first task or devmapper pressure failure.

## Telemetry and knowledge-base checks

Clause telemetry and cgroup artifacts are collected automatically for task
commands. A native sample enters the KB only when identity pairing, signature,
schema, quality, loss, cleanup, and resource checks all pass.

Query the current generation:

```bash
export CLAWBOX_SERVICE_TOKEN='replace-with-control-plane-token'
kubectl -n clawbox-system port-forward service/clawbox-tune-kb 8086:8086
python3 scripts/kb-live-status.py \
  --endpoint http://127.0.0.1:8086 \
  --tenant team-a \
  --repo github.com/example/project
```

Run the strict production Tool-VM telemetry gate after rebuilding a task
image:

```bash
bash scripts/run-toolbridge-ebpf-integration.sh \
  --image 'registry.example.com/task@sha256:DIGEST' \
  --namespace clawbox-ebpf-acceptance \
  --output .artifacts/tool-ebpf-acceptance.log
```

Success requires real native clause telemetry, non-zero CPU/RSS, zero event
loss, successful cleanup, distinct concurrent cgroups, no cross-attribution,
and native artifact validation. Command success alone is not telemetry
success.

For the local development registry, the default `:dev` image must be rebuilt
from the current Tool Bridge source. The bridge mounts and verifies tracefs
before starting BCC because the minimal Kata guest does not mount tracefs by
default. The standalone kernel gate is also available:

```bash
kubectl create namespace clawbox-benchmarks --dry-run=client -o yaml | kubectl apply -f -
kubectl delete pod -n clawbox-benchmarks clawbox-ebpf-cgroup-smoke --ignore-not-found
kubectl apply -f deploy/ebpf-cgroup-smoke.yaml
kubectl wait -n clawbox-benchmarks --for=jsonpath='{.status.phase}'=Succeeded \
  pod/clawbox-ebpf-cgroup-smoke --timeout=360s
kubectl logs -n clawbox-benchmarks clawbox-ebpf-cgroup-smoke
```

## Operational cleanup

Kubernetes retains terminated pods until its pod-GC threshold is reached. A
large failure burst can therefore leave thousands of `Failed` objects even
after the underlying runtime is healthy. Inspect first, then delete only pods
whose API phase is already terminal:

```bash
python3 scripts/cleanup-failed-pods.py
python3 scripts/cleanup-failed-pods.py --namespace clawbox-system --apply
```

The cleanup script enumerates exact pod names, batches deletion by namespace,
and never selects Running, Pending, or Succeeded pods. `--apply` requires an
explicit namespace unless the operator deliberately passes `--all-namespaces`.
Remove dedicated probe namespaces separately after preserving any evidence
you need.

## Development checks

```bash
python3 -m pytest -q
python3 -m py_compile $(find clawbox scripts -name '*.py')

cd toolbridge
go test -race ./...
```

Host, Firecracker, and eBPF changes also require the real ARM64 smoke and
strict integration commands shown above.

Maintainers and code agents should read [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)
before changing the execution, telemetry, identity, or storage contracts.
