# ClawBox

![ClawBox architecture overview](clawbox-overview.png)

ClawBox runs coding-agent tasks on a native ARM64 Kubernetes node. Each task is
one `SandboxTask` containing two isolated Kata/Firecracker virtual machines:

- the Runtime VM runs the agent;
- the Tool VM contains the task repository and executes tools;
- the Cell Controller admits, starts, monitors, and cleans up both VMs as one
  unit.

The supported production target is a single ARM64 node. There is no supported
x86, QEMU, runc, or multi-node fallback. `docker-compose.yml` and
`scripts/linux-deploy.sh` are development checks, not production deployment
paths.

## Choose the right command

| Goal | Command |
| --- | --- |
| Check an installed host | `scripts/clawbox doctor` |
| Start or reconcile an installed host | `scripts/clawbox up` |
| Submit one task using the configured template image | `scripts/clawbox submit ...` |
| Run several SWE-ReBench tasks with their own images | `scripts/run-swe-rebench.sh ...` |
| Install a new, already-bootstrapped host | `scripts/clawbox install` |

For normal SWE-ReBench batches, use `scripts/run-swe-rebench.sh`. The Managed
API template used by `scripts/clawbox submit` contains one fixed Tool image;
`--input-ref` is only an identifier and does not select an image.

## Use an installed host

Run these commands from the ClawBox checkout on the Kubernetes host:

```bash
scripts/clawbox up
scripts/clawbox doctor
```

`up` starts host services if necessary and reconciles the five ClawBox
Deployments. It never partitions disks or runs the destructive host bootstrap.
`doctor` is read-only and reports whether the node, RuntimeClasses,
Deployments, and immutable platform images are ready.

### Submit one task

Use this path only when the configured `swe-rebench-arm64` template image is
the image for the task being submitted.

```bash
scripts/clawbox submit \
  --input-ref INSTANCE_ID \
  --problem-file /path/to/problem-statement.txt
```

Submission is asynchronous by default. The command returns a `runId` while the
task continues in the cluster. Check it later:

```bash
scripts/clawbox status RUN_ID
scripts/clawbox attempts RUN_ID
scripts/clawbox events RUN_ID
scripts/clawbox watch RUN_ID --timeout 2700
```

Add `--watch` to `submit` only when the submitting shell should wait for a
terminal state:

```bash
scripts/clawbox submit \
  --input-ref INSTANCE_ID \
  --problem-file /path/to/problem-statement.txt \
  --watch
```

Pressing Ctrl+C stops only the local watcher. It does not cancel the cluster
task. Cancel explicitly with:

```bash
scripts/clawbox cancel RUN_ID
```

Use `--idempotency-key KEY` only when retrying the same request after a client
or network failure. Reusing a key prevents an accidental duplicate run.
`scripts/clawbox retry RUN_ID` creates another attempt for a terminal run.

Run phases have the following meaning:

| Phase | Meaning |
| --- | --- |
| `Queued` | Accepted but waiting for controller admission or capacity |
| `Running` | One or both task VMs are active |
| `Finalizing` | The agent stopped and artifacts are being collected |
| `Succeeded` | The agent and artifact collection completed |
| `Failed` | The platform or agent failed |
| `TimedOut` | The configured deadline expired |
| `Cancelled` | Cancellation was committed and is terminal |

### Run SWE-ReBench tasks concurrently

This is the normal path for several SWE-ReBench instances because it resolves
each task's original image through the ARM64 image mapping before submission.

The task file is JSON. It may be a list or contain a `tasks`/`instances` list.
Every entry must include:

```json
{
  "instance_id": "owner__repo-123",
  "image": "original/swe-rebench-image",
  "problem_statement": "The issue to fix",
  "base_commit": "optional commit",
  "hint_text": "optional hint"
}
```

`../ClawTune/swe_rebench/tasks.json` already has this shape. The mapping file
is produced while building the ARM64 task images; its keys are the original
`image` values from this task file.

Run a small foreground batch first:

```bash
bash scripts/run-swe-rebench.sh \
  --tasks ../ClawTune/swe_rebench/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-cidr PROVIDER_CIDR \
  --parallelism 2 \
  --sample 2
```

`--parallelism N` allows at most N tasks to be active through this launcher at
once. The Cell Controller can still leave a task in `Queued` when the complete
two-VM resource budget is unavailable. `--sample N` selects the first N tasks;
omit it to run the whole file.

For an asynchronous batch, keep a stable run ID and put the launcher in the
background:

```bash
mkdir -p .artifacts
RUN_ID="swe-$(date -u +%Y%m%d%H%M%S)"

nohup bash scripts/run-swe-rebench.sh \
  --tasks ../ClawTune/swe_rebench/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-cidr PROVIDER_CIDR \
  --parallelism 4 \
  --run-id "$RUN_ID" \
  >".artifacts/${RUN_ID}.results.json" \
  2>".artifacts/${RUN_ID}.log" </dev/null &

echo $! >".artifacts/${RUN_ID}.pid"
echo "$RUN_ID"
```

The results JSON is complete only after every selected task reaches a terminal
state. Monitor the batch independently of the launcher process:

```bash
kubectl -n clawbox-benchmarks get sandboxtasks \
  -l "clawbox.openai.com/run=${RUN_ID}" \
  -o custom-columns='NAME:.metadata.name,INSTANCE:.metadata.annotations.clawbox\.openai\.com/original-instance-id,PHASE:.status.phase,OUTCOME:.status.outcome,REASON:.status.reason'
```

Add `-w` to keep watching. Child Pods and Jobs disappear after terminal
artifact collection; that cleanup is expected. The parent `SandboxTask`
retains the final phase and outcome.

To cancel every active task in this batch:

```bash
while read -r task; do
  kubectl -n clawbox-benchmarks patch "$task" \
    --type=merge -p '{"spec":{"desiredState":"Cancelled"}}'
done < <(kubectl -n clawbox-benchmarks get sandboxtasks \
  -l "clawbox.openai.com/run=${RUN_ID}" -o name)
```

Stopping the launcher with Ctrl+C or `kill` does not cancel already-created
`SandboxTask` objects.

### Retrieve a result

The ingester stores the final answer, patch, and bounded logs after a task
finishes. First obtain the `SandboxTask` name from the monitoring command and
use it as `TASK_ID`:

```bash
TASK_ID='sandboxtask-name-from-the-NAME-column'
TOKEN="$(kubectl -n clawbox-system get secret clawbox-control-plane \
  -o jsonpath='{.data.service-token}' | base64 -d)"

kubectl -n clawbox-system port-forward service/clawbox-ingester 8084:8084 \
  >.artifacts/ingester-port-forward.log 2>&1 &
PORT_FORWARD_PID=$!
for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8084/healthz >/dev/null && break
  sleep 1
done
curl -fsS http://127.0.0.1:8084/healthz >/dev/null

curl -fsS \
  -H "Authorization: Bearer ${TOKEN}" \
  "http://127.0.0.1:8084/v1/archive/${TASK_ID}/result" \
  -o ".artifacts/${TASK_ID}.result.json"
curl -fsS \
  -H "Authorization: Bearer ${TOKEN}" \
  "http://127.0.0.1:8084/v1/archive/${TASK_ID}/traces" \
  -o ".artifacts/${TASK_ID}.traces.json"

kill "$PORT_FORWARD_PID"
unset TOKEN
```

The result JSON contains `status`, `final_answer`, `patch`, selected logs, and
task metadata. The traces JSON lists the archived trace files and their sizes.

## Install a new host

Installation has four distinct stages: prepare the ARM64 host, build immutable
images, configure site-specific values, and deploy. Do not run fresh-host
bootstrap commands on an existing installation.

### Requirements

- dedicated native ARM64 Linux host with hardware virtualization;
- Kubernetes 1.35 and containerd 2.3.4;
- Kata Containers 3.31.0 and Firecracker 1.12.1;
- cgroup v2, Docker with Buildx, and Python 3.12+;
- registry reachable by Docker and containerd;
- ClawTune checked out beside ClawBox;
- SWE-bench harness checkout pinned to the revision required by the image
  builder;
- LLM API key, base URL, model names, and exact provider egress CIDR;
- two unused whole block devices only for fresh-host devmapper bootstrap.

The bootstrap erases the two devices passed to it. Verify their absolute paths
and contents before applying it.

### 1. Check out and test

```bash
cd /home/USER
git clone https://github.com/w20chen/ClawTune.git
git clone https://github.com/w20chen/ClawBox.git
git -C ClawTune checkout --detach e91e60bc1e5f3209fbcf6091013fde96f217e2a7
cd ClawBox

python3 -c 'import sys; assert sys.version_info >= (3, 12), sys.version'
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev,postgres]'
python3 -m pytest -q
```

Do not build release images from a dirty working tree. Record the ClawBox and
ClawTune revisions used for the deployment.

### 2. Prepare or verify the host

On a fresh dedicated host, inspect the read-only plan first:

```bash
bash scripts/bootstrap-openeuler-arm64.sh plan \
  --devmapper-data-device /dev/DATA_DISK \
  --devmapper-meta-device /dev/META_DISK
```

Only after confirming both devices are unused whole disks:

```bash
sudo bash scripts/bootstrap-openeuler-arm64.sh apply \
  --devmapper-data-device /dev/DATA_DISK \
  --devmapper-meta-device /dev/META_DISK \
  --confirm-erase /dev/DATA_DISK,/dev/META_DISK

bash scripts/install-ebpf-kata-runtime.sh apply
```

On an already-provisioned host, do not run `apply`. Verify it instead:

```bash
sudo -v
sudo bash scripts/bootstrap-openeuler-arm64.sh status
bash deploy/check-host.sh --runtime-class kata-fc-arm64
bash scripts/arm64-kata-smoke.sh --runtime-class kata-fc-arm64
```

After the checks pass:

```bash
kubectl label node "$(hostname)" \
  clawbox.openai.com/firecracker-ready=true --overwrite
```

### 3. Build immutable images

Build on the ARM64 host. Start a local registry only when an external registry
is not already available:

```bash
docker run -d --restart unless-stopped --name clawbox-registry \
  -p 127.0.0.1:5000:5000 \
  -v clawbox-registry:/var/lib/registry registry:2
```

Build and push the three platform images:

```bash
export REGISTRY=127.0.0.1:5000/clawbox
export CLAWTUNE_ROOT="${CLAWTUNE_ROOT:-$PWD/../ClawTune}"
export TAG="$(git rev-parse --short=12 HEAD)"
export PUSH=1

bash scripts/build-kubernetes-images.sh
```

The build writes immutable image references to
`.artifacts/platform-images.env`. `scripts/clawbox install` reads this file
automatically. Do not deploy `:dev` or `:latest` tags.

Build the selected SWE-ReBench task images and their ARM64 mapping:

```bash
git -C /src/SWE-bench-fork checkout --detach \
  980d0cca8aa4e73f1d9f894e906370bef8c4de8a
python3 -m pip install -e '.[images]'
python3 scripts/build-swe-rebench-arm64.py \
  --dataset /data/swe-rebench.parquet \
  --selection ../ClawTune/swe_rebench/tasks.json \
  --swebench-root /src/SWE-bench-fork \
  --registry "$REGISTRY" \
  --mapping /data/swe-rebench-arm64-map.json \
  --tool-bridge-binary .artifacts/tool-bridge-arm64/tool-bridge \
  --push --fail-fast
```

Every supported mapping entry must contain a native
`linux/arm64` `image@sha256:...` reference. There is no architecture fallback.

### 4. Configure and deploy

Choose one ARM64 task image from the mapping for the Managed API's default
single-task template. Batch execution still uses the complete mapping file.

```bash
export CLAWBOX_LLM_API_KEY='PROVIDER_KEY'
export CLAWBOX_LLM_BASE_URL='https://provider.example/v1'
export CLAWBOX_LLM_MODEL='provider-model'
export CLAWBOX_OPENCLAW_MODEL_REF='vllm/provider-model'
export CLAWBOX_TOOL_IMAGE='REGISTRY/task@sha256:DIGEST'
export CLAWBOX_LLM_EGRESS_CIDR='PROVIDER_CIDR'

scripts/clawbox install
scripts/clawbox doctor
```

Single-node installs use persistent SQLite by default. Set
`CLAWBOX_DATABASE_URL=postgresql+psycopg://...` before `install` when
PostgreSQL is required. `install` creates Secrets, runs migrations, renders
digest-pinned manifests, deploys all five services, and waits for readiness.

The manual manifest path, Secret schema, apply order, and persistence details
are documented in [deploy/README.md](deploy/README.md). Do not run the manual
path after a successful `scripts/clawbox install`.

## Troubleshooting

### A task stays Queued

Inspect the parent task first:

```bash
kubectl -n clawbox-benchmarks get sandboxtask TASK_NAME -o yaml
```

- `PendingAdmission` means the controller has not admitted it yet.
- `InsufficientCellCapacity` means the complete two-VM budget is unavailable.

Then check the controller:

```bash
kubectl -n clawbox-system get pods \
  -l app.kubernetes.io/component=cell-controller
kubectl -n clawbox-system logs deployment/clawbox-cell-controller --tail=300
```

If it is restarting, inspect the last termination reason:

```bash
kubectl -n clawbox-system describe pod \
  -l app.kubernetes.io/component=cell-controller
kubectl -n clawbox-system logs deployment/clawbox-cell-controller \
  --previous --tail=300
```

Do not assume that increasing a memory limit fixes the cause. `OOMKilled`
shows that the previous limit was exceeded; controller logs and pod inventory
are still required to find the retained data or unbounded list operation.

### Metrics API is unavailable

`kubectl top` requires a cluster Metrics API. On cgroup v2, inspect the
controller directly:

```bash
kubectl -n clawbox-system exec deployment/clawbox-cell-controller \
  -c controller -- cat /sys/fs/cgroup/memory.current
kubectl -n clawbox-system exec deployment/clawbox-cell-controller \
  -c controller -- cat /sys/fs/cgroup/memory.peak
```

### Old Failed Pods

Preview first, then delete only Failed Pods in an explicit ClawBox namespace:

```bash
python3 scripts/cleanup-failed-pods.py
python3 scripts/cleanup-failed-pods.py \
  --namespace clawbox-system --apply
```

Do not clean unrelated namespaces without investigating their owner and
failure reason.

## Development and operator references

```bash
python3 -m pytest -q
python3 -m py_compile $(find clawbox scripts -name '*.py')
cd toolbridge && go test -race ./...
```

- Kubernetes manifests and manual deployment: [deploy/README.md](deploy/README.md)
- Execution and telemetry invariants: [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)
- Capacity scale gate: `scripts/scale-swe-rebench.sh`
- Managed API load test: `scripts/load-test.sh`
