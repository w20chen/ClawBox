# ClawBox

![ClawBox architecture overview](clawbox-overview.png)

ClawBox runs coding-agent tasks on a native ARM64 Kubernetes node. ARM64 is the
CPU architecture used by servers such as Kunpeng. Kubernetes is the software
that starts, monitors, and stops ClawBox services and task processes. A coding
task consists of one repository state and one problem statement. ClawBox runs
each task in two isolated Linux virtual machines:

- the Runtime VM runs the agent;
- the Tool VM contains the task repository and executes tools;
- Kubernetes stores the request as a `SandboxTask` object;
- the Cell Controller starts, monitors, and cleans up both VMs together.

The supported production target is a single ARM64 node. There is no supported
x86, QEMU, runc, or multi-node fallback. `docker-compose.yml` and
`scripts/linux-deploy.sh` are development checks, not production deployment
paths.

## Terms used in this README

These project-specific terms are used throughout the commands below:

- **Task image:** a container image, not a picture. It contains the exact
  repository state and software dependencies for one coding task. Different
  SWE-ReBench tasks normally require different task images.
- **Platform image:** the ClawBox software itself, such as the Runtime and Cell
  Controller. Platform images are shared by tasks.
- **Tool Bridge:** the service inside the Tool VM that receives command
  requests from the agent and runs them against the task repository.
- **ClawTune:** the sibling project that supplies the selected SWE-ReBench task
  list and records execution data used by the tuning system.
- **Managed API:** the service used by `scripts/clawbox submit`. It records a
  run ID and provides status, cancellation, and retry operations.
- **Template:** saved server-side settings for `scripts/clawbox submit`. A
  template fixes the task image, Runtime image, resource size, and allowed LLM
  connection settings.
- **`swe-rebench-arm64`:** the name of the default template. The name does not
  mean that it contains every SWE-ReBench task. In the current implementation,
  it contains one fixed task image selected during installation.
- **ARM64 mapping:** a JSON file that maps each original SWE-ReBench image name
  to the exact ARM64 task image built for it. The batch runner uses this file
  to choose the correct image for every task.

An image written as `name@sha256:...` is pinned to one exact build. ClawBox
requires this form so a later registry update cannot silently change a run.

## Choose the right command

| Goal | Command |
| --- | --- |
| Check an installed host | `scripts/clawbox doctor` |
| Start an installed host and reapply its saved configuration | `scripts/clawbox up` |
| Submit one task that matches the template's fixed task image | `scripts/clawbox submit ...` |
| Run SWE-ReBench tasks and select the correct image for each one | `scripts/run-swe-rebench.sh ...` |
| Install a new, already-bootstrapped host | `scripts/clawbox install` |

For normal SWE-ReBench work, use `scripts/run-swe-rebench.sh`, even if the
first test contains only one task. It checks the ARM64 mapping and selects the
correct repository image. Use `scripts/clawbox submit` only when the person who
installed ClawBox has recorded which task image is stored in the template.

## Use an installed host

Run these commands from the ClawBox checkout on the Kubernetes host:

```bash
scripts/clawbox up
scripts/clawbox doctor
```

`up` starts the required host services and makes the running ClawBox services
match the saved installation configuration. It never partitions disks or runs
the destructive host bootstrap. `doctor` changes nothing; it reports whether
the node, Firecracker runtime, ClawBox services, and platform images are ready.

### Submit one task through the Managed API (fixed-image path)

During installation, the person installing ClawBox sets
`CLAWBOX_TOOL_IMAGE` to one task image. ClawBox stores that image and the other
allowed settings under the template name `swe-rebench-arm64`. Every
`scripts/clawbox submit` command using this template runs with that same task
image.

The command does not look up an image from `INSTANCE_ID`. Therefore, before
using this path, confirm that the task image chosen during installation was
built for this instance. If that information is unavailable, use the
SWE-ReBench runner in the next section; it performs the image lookup itself.

To display the saved template configuration:

```bash
kubectl -n clawbox-system get secret clawbox-managed \
  -o jsonpath='{.data.templates}' | base64 -d
echo
```

In the printed JSON, `swe-rebench-arm64` > `1` > `toolImage` is the exact task
image every submission will use. Find that value in the ARM64 mapping and
confirm that its original image belongs to `INSTANCE_ID`. If you cannot make
that match, do not submit through this path.

Prepare a text file containing the task's actual `problem_statement`, then
submit it:

```bash
scripts/clawbox submit \
  --input-ref INSTANCE_ID \
  --problem-file /path/to/problem-statement.txt
```

- `INSTANCE_ID` is the task's `instance_id` from the SWE-ReBench task JSON.
- `--problem-file` points to a plain-text file containing the issue the agent
  must solve.
- `--input-ref` is recorded for identification only. It does not change the
  task image.

Submission is asynchronous by default. The command returns a `runId` while the
task continues in the cluster. Check it later:

```bash
scripts/clawbox status RUN_ID
scripts/clawbox attempts RUN_ID
scripts/clawbox events RUN_ID
scripts/clawbox watch RUN_ID --timeout 2700
```

Add `--watch` to `submit` only when the submitting shell should wait until the
run finishes:

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
`scripts/clawbox retry RUN_ID` creates another attempt for a finished run.

Run phases have the following meaning:

| Phase | Meaning |
| --- | --- |
| `Queued` | Accepted but waiting for the controller or free host resources |
| `Running` | One or both task VMs are active |
| `Finalizing` | The agent stopped and its result is being collected |
| `Succeeded` | The agent and result collection completed |
| `Failed` | The platform or agent failed |
| `TimedOut` | The configured deadline expired |
| `Cancelled` | Cancellation was completed and the state will not change |

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

`PROVIDER_CIDR` is the IP address range that task VMs may contact for the LLM
API. Use `/32` for one IPv4 address, for example `203.0.113.10/32`. This value
must describe the real provider endpoint; ask the provider or network operator
when its addresses are not known.

Run a small batch in the current shell first. The command waits for both tasks
to finish:

```bash
bash scripts/run-swe-rebench.sh \
  --tasks ../ClawTune/swe_rebench/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-cidr PROVIDER_CIDR \
  --parallelism 2 \
  --sample 2
```

`--parallelism N` allows at most N tasks to be active through this launcher at
once. A task can still remain `Queued` when the host lacks enough CPU, memory,
or storage to start both of its VMs. `--sample N` selects the first N tasks;
omit it to run the whole file.

For an asynchronous batch, keep a stable run ID and put the launcher in the
background. `nohup` keeps the launcher running after the SSH session closes.
`RUN_ID` labels all tasks in the batch, while the `.pid` file records only the
local launcher process; they are different identifiers.

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

The results JSON is complete only after every selected task finishes. Monitor
the batch independently of the launcher process:

```bash
kubectl -n clawbox-benchmarks get sandboxtasks \
  -l "clawbox.openai.com/run=${RUN_ID}" \
  -o custom-columns='NAME:.metadata.name,INSTANCE:.metadata.annotations.clawbox\.openai\.com/original-instance-id,PHASE:.status.phase,OUTCOME:.status.outcome,REASON:.status.reason'
```

Add `-w` to keep watching. The short-lived Kubernetes resources that ran the
two VMs disappear after result collection; that cleanup is expected. The
`SandboxTask` record remains and keeps the final phase and outcome.

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

ClawBox's result service stores the final answer, patch, and bounded logs after
a task finishes. First obtain the `SandboxTask` name from the monitoring
command and use it as `TASK_ID`. The following commands read ClawBox's internal
result-access token, temporarily forward local port 8084 to the result service,
download the files, close the connection, and remove the token from the shell:

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

Installation has four distinct stages: prepare the ARM64 host, build
exact-version images, configure values specific to this installation, and
start ClawBox. Do not run fresh-host bootstrap commands on an existing
installation.

In this project, **bootstrap** means the one-time preparation of a new Linux
host: configure Kubernetes, containerd, Kata/Firecracker, and the disk pool
used by task VMs. It is separate from installing or starting ClawBox itself.

### Requirements

- dedicated native ARM64 Linux host with hardware virtualization;
- Kubernetes 1.35 and containerd 2.3.4, the container runtime used by
  Kubernetes;
- Kata Containers 3.31.0 and Firecracker 1.12.1;
- cgroup v2 (Linux resource accounting), Docker with Buildx for building
  images, and Python 3.12+;
- a container registry, which is a server that stores images for Docker and
  Kubernetes to download;
- ClawTune checked out beside ClawBox;
- a SWE-bench harness checkout at the exact Git commit required by the image
  builder;
- LLM API key, base URL, model names, and exact provider egress CIDR;
- two unused complete disks, not existing partitions, only for fresh-host
  devmapper bootstrap.

The bootstrap uses one disk for task data and the other for devmapper's block
allocation metadata. It initializes both as a new task-storage pool, which
erases their previous contents. Verify their absolute paths before applying
it; neither disk may contain the operating system or user data.

### 1. Check out and test

Replace `USER` with the Linux account that will own the two checkouts:

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

Do not build release images while either Git checkout contains uncommitted
changes. Record the exact ClawBox and ClawTune commits used for deployment.

### 2. Prepare or verify the host

On a fresh dedicated host, inspect the read-only plan first:

Replace `DATA_DISK` with the complete disk used for task data and `META_DISK`
with the complete disk used for devmapper allocation metadata. Neither path
may refer to the system disk or an existing partition.

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

### 3. Build exact-version images

Build on the ARM64 host. Start a local registry only when an external registry
is not already available:

```bash
docker run -d --restart unless-stopped --name clawbox-registry \
  -p 127.0.0.1:5000:5000 \
  -v clawbox-registry:/var/lib/registry registry:2
```

Build and push the three shared platform images: the control plane, Runtime
VM software, and Tool Bridge:

```bash
export REGISTRY=127.0.0.1:5000/clawbox
export CLAWTUNE_ROOT="${CLAWTUNE_ROOT:-$PWD/../ClawTune}"
export TAG="$(git rev-parse --short=12 HEAD)"
export PUSH=1

bash scripts/build-kubernetes-images.sh
```

The build writes the exact image references to
`.artifacts/platform-images.env`. `scripts/clawbox install` reads this file
automatically. Do not deploy `:dev` or `:latest` tags.

Build the selected SWE-ReBench task images and their ARM64 mapping. Before the
command, provide:

- `/data/swe-rebench.parquet`: the full SWE-ReBench task dataset;
- `../ClawTune/swe_rebench/tasks.json`: the tasks selected for this project;
- `/src/SWE-bench-fork`: the SWE-bench harness checkout;
- `$REGISTRY`: the destination configured above;
- `/data/swe-rebench-arm64-map.json`: the mapping file to create or update;
- `.artifacts/tool-bridge-arm64/tool-bridge`: the Tool Bridge binary produced
  by the platform build.

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

Prepare these values before running the command:

- `CLAWBOX_LLM_API_KEY`: credential accepted by the LLM provider;
- `CLAWBOX_LLM_BASE_URL`: base URL of its OpenAI-compatible API;
- `CLAWBOX_LLM_MODEL`: model name expected by that provider;
- `CLAWBOX_OPENCLAW_MODEL_REF`: the adapter and model reference used by
  OpenClaw, for example `vllm/provider-model`;
- `CLAWBOX_TOOL_IMAGE`: one exact ARM64 task image from the mapping; this
  becomes the fixed image in the default Managed API template;
- `CLAWBOX_LLM_EGRESS_CIDR`: the real provider IP range explained in the batch
  section above.

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
PostgreSQL is required. `install` stores the credentials, initializes the
database, writes Kubernetes configuration using the exact image versions,
starts all five ClawBox services, and waits until they are ready.

Manual Kubernetes deployment, credential-file format, component order, and
database storage are documented in [deploy/README.md](deploy/README.md). Do
not run that manual path after a successful `scripts/clawbox install`.

## Troubleshooting

### A task stays Queued

Inspect the parent task first:

```bash
kubectl -n clawbox-benchmarks get sandboxtask TASK_NAME -o yaml
```

- `PendingAdmission` means the controller has not started the two task VMs yet.
- `InsufficientCellCapacity` means the host cannot currently provide enough
  CPU, memory, or storage for both task VMs.

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

`kubectl top` works only when the optional Kubernetes Metrics API is installed.
If it is unavailable, Linux cgroup v2 still exposes the controller's current
and peak memory directly:

```bash
kubectl -n clawbox-system exec deployment/clawbox-cell-controller \
  -c controller -- cat /sys/fs/cgroup/memory.current
kubectl -n clawbox-system exec deployment/clawbox-cell-controller \
  -c controller -- cat /sys/fs/cgroup/memory.peak
```

### Old Failed Pods

Kubernetes groups related resources in a namespace. Preview first, then delete
only Failed Pods in an explicitly named ClawBox namespace:

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
