# ClawBox

![ClawBox architecture overview](clawbox-overview.png)

ClawBox runs coding-agent tasks on one native ARM64 Kubernetes node. Each
task is a `SandboxTask` backed by two isolated Kata/Firecracker VMs:

- the Runtime VM runs OpenClaw and calls the LLM;
- the Tool VM contains the repository and executes tools;
- the Cell Controller admits, starts, observes, and cleans up both VMs.

The supported production target is a dedicated ARM64 host such as Kunpeng.
There is no supported x86, QEMU, runc, or multi-node fallback.

This README has two paths. Use **New host: one-time installation** exactly
once for a new machine. For every later reboot or task run, skip it and use
**Existing host: start and run tasks**.

## Command map

| Situation | Command |
| --- | --- |
| Read-only health check | `scripts/clawbox doctor` |
| Start/reconcile an installed host | `scripts/clawbox up` |
| Submit one task using the installed fixed-image template | `scripts/clawbox submit ...` |
| Run mapped SWE-ReBench tasks, including concurrent batches | `scripts/run-swe-rebench.sh ...` |
| Install ClawBox on an already bootstrapped new host | `scripts/clawbox install ...` |

All commands below run from the ClawBox checkout on the ARM64 host unless a
section says otherwise. Platform and task images must use immutable
`IMAGE@sha256:...` references.

## New host: one-time installation

Do not repeat this section after the host has been bootstrapped. In
particular, the devmapper `apply` command erases the two disks named on its
command line.

### 1. Requirements

- dedicated openEuler ARM64 host with `/dev/kvm` and cgroup v2;
- Python 3.12+, Git, Docker with Buildx, and outbound access for installation;
- two unused whole disks: one devmapper data disk and one metadata disk;
- ClawBox and ClawTune checkouts owned by the administrator account;
- an image registry reachable by both Docker and Kubernetes;
- an LLM API key, OpenAI-compatible base URL, model name, and provider egress
  CIDR;
- the SWE-ReBench dataset and the pinned SWE-bench harness when building task
  images.

Never use the system disk, a mounted filesystem, or a partition such as
`/dev/sdb1` for devmapper. Use whole, dedicated devices such as `/dev/sdb`.

### 2. Check out and test the release

Replace `/home/USER` with the administrator's home directory:

```bash
cd /home/USER
git clone https://github.com/w20chen/ClawTune.git
git clone https://github.com/w20chen/ClawBox.git
git -C ClawTune checkout --detach e91e60bc1e5f3209fbcf6091013fde96f217e2a7
cd ClawBox

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev,postgres]'
python3 -m pytest -q
```

Release image builds refuse dirty ClawBox or ClawTune checkouts. Commit or
remove local changes before publishing images.

### 3. Inspect and bootstrap the host

Set the exact whole-disk paths, inspect them, and run the read-only plan:

```bash
export DATA_DISK=/dev/DATA_DISK
export META_DISK=/dev/META_DISK

lsblk -o NAME,PATH,SIZE,TYPE,FSTYPE,MOUNTPOINTS
findmnt -rn -S "$DATA_DISK" || true
findmnt -rn -S "$META_DISK" || true

bash scripts/bootstrap-openeuler-arm64.sh plan \
  --devmapper-data-device "$DATA_DISK" \
  --devmapper-meta-device "$META_DISK"
```

Only after independently confirming that both devices are unused and may be
erased, apply the plan:

```bash
sudo bash scripts/bootstrap-openeuler-arm64.sh apply \
  --devmapper-data-device "$DATA_DISK" \
  --devmapper-meta-device "$META_DISK" \
  --confirm-erase "$DATA_DISK,$META_DISK"

bash scripts/install-ebpf-kata-runtime.sh apply
```

The bootstrap installs pinned Kubernetes, containerd, Kata, Firecracker,
Calico, devmapper, the RuntimeClass, and the containerd systemd unit. It also
runs the static host gate and a live Firecracker smoke test before applying
the ready label.

Verify the persistent devmapper setting and host health:

```bash
systemctl cat containerd.service | grep 'DM_DISABLE_UDEV=1'
systemctl show containerd.service -p Environment | grep 'DM_DISABLE_UDEV=1'
sudo bash scripts/bootstrap-openeuler-arm64.sh status
bash deploy/check-host.sh --runtime-class kata-fc-arm64 --require-ready-label
bash scripts/arm64-kata-smoke.sh --runtime-class kata-fc-arm64
```

Both `DM_DISABLE_UDEV` checks must print a match. This prevents libdevmapper
from waiting on stale udev cookies during concurrent VM creation.

### 4. Build and publish platform images

For a registry on the same single node, start it once:

```bash
docker run -d --restart unless-stopped --name clawbox-registry \
  -p 127.0.0.1:5000:5000 \
  -v clawbox-registry:/var/lib/registry registry:2
```

Skip that command when using an existing external registry. Then build on the
native ARM64 host:

```bash
export REGISTRY=127.0.0.1:5000/clawbox
export CLAWTUNE_ROOT="$PWD/../ClawTune"
export TAG="$(git rev-parse --short=12 HEAD)"
export PUSH=1

bash scripts/build-kubernetes-images.sh
```

The build publishes the control-plane, Runtime, and Tool Bridge images and
writes their immutable references to `.artifacts/platform-images.env`.

### 5. Build ARM64 task images and mapping

The batch launcher does not translate x86 images at runtime. Every input task
must have a native ARM64 image recorded in the mapping.

Prepare the following paths:

- `/data/swe-rebench.parquet`: SWE-ReBench dataset;
- `../ClawTune/swe_rebench/tasks.json`: selected task list;
- `/src/SWE-bench-fork`: SWE-bench harness checkout;
- `/data/swe-rebench-arm64-map.json`: mapping output.

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

Every supported mapping entry must contain `platform: linux/arm64` and an
immutable `arm64_image`.

### 6. Configure the LLM and install ClawBox

Resolve the LLM hostname immediately before installation. Do not copy an old
provider IP from a previous run: provider addresses can change. Use one
provider-approved CIDR, or a `/32` for the exact IPv4 address selected by your
network configuration. ClawBox rejects `0.0.0.0/0`.

```bash
getent ahostsv4 api.deepseek.com | awk '{print $1}' | sort -u
```

If this prints several addresses, do not arbitrarily copy the first one: use
the provider/network operator's narrow stable CIDR. The CIDR passed to a batch
must cover the hostname stored in the `clawbox-llm` Secret.

As of this README update, DeepSeek's official OpenAI-compatible base URL is
`https://api.deepseek.com` and its current model IDs include
`deepseek-v4-flash` and `deepseek-v4-pro`. Recheck the
[official DeepSeek API documentation](https://api-docs.deepseek.com/) before
a new installation.

Choose one task image from the mapping for the Managed API's default
single-task template, then install:

```bash
read -rsp 'DeepSeek API key: ' CLAWBOX_LLM_API_KEY
echo
export CLAWBOX_LLM_API_KEY
export CLAWBOX_LLM_BASE_URL='https://api.deepseek.com'
export CLAWBOX_LLM_MODEL='deepseek-v4-flash'
export CLAWBOX_OPENCLAW_MODEL_REF='vllm/deepseek-v4-flash'
export CLAWBOX_TOOL_IMAGE='127.0.0.1:5000/clawbox/TASK@sha256:DIGEST'
export CLAWBOX_LLM_EGRESS_CIDR='PROVIDER_IPV4/32'

scripts/clawbox install
scripts/clawbox doctor
unset CLAWBOX_LLM_API_KEY
```

`install` stores credentials in Kubernetes Secrets, initializes persistent
SQLite by default, applies all five ClawBox deployments, and waits for them to
be ready. Set `CLAWBOX_DATABASE_URL=postgresql+psycopg://...` before install
when PostgreSQL is required.

### 7. First concurrency smoke test

Start with two tasks and a 120-second agent budget:

```bash
RUN_ID="smoke-$(date -u +%Y%m%d%H%M%S)"

bash scripts/run-swe-rebench.sh \
  --tasks ../ClawTune/swe_rebench/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-cidr "$CLAWBOX_LLM_EGRESS_CIDR" \
  --parallelism 2 \
  --sample 2 \
  --timeout-seconds 120 \
  --command-timeout-seconds 120 \
  --run-id "$RUN_ID"
```

The agent budget is exactly 120 seconds. VM startup and bounded result
collection happen outside that agent budget, so total wall-clock time is
longer. Increase concurrency only after `scripts/clawbox doctor` still passes
and the node has no `DiskPressure`.

## Existing host: start and run tasks

Use this section after every reboot and for normal daily operation. Never
repeat the disk bootstrap.

### 1. Start and verify

```bash
cd /home/USER/ClawBox
scripts/clawbox up
scripts/clawbox doctor
```

`up` starts containerd, kubelet, and Docker when needed, reapplies the saved
immutable image configuration, runs database migrations, and waits for all
five deployments. It does not partition or erase disks.

Before a large batch, also check node pressure and recent sandbox failures:

```bash
kubectl get nodes
kubectl describe node | grep -A2 -E 'DiskPressure|MemoryPressure|PIDPressure'
kubectl -n clawbox-benchmarks get events --sort-by=.lastTimestamp \
  | grep -E 'FailedCreatePodSandbox|devmapper' | tail -20 || true
```

### 2. Run an 8-concurrency, 2-minute batch

The task JSON must contain `instance_id`, `image`, and `problem_statement` for
each task. The `image` value is looked up in the ARM64 mapping.

```bash
export PROVIDER_CIDR='CURRENT_PROVIDER_IPV4/32'
export RUN_ID="swe-$(date -u +%Y%m%d%H%M%S)"

bash scripts/run-swe-rebench.sh \
  --tasks ../ClawTune/swe_rebench/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-cidr "$PROVIDER_CIDR" \
  --profile small \
  --parallelism 8 \
  --sample 8 \
  --timeout-seconds 120 \
  --command-timeout-seconds 120 \
  --run-id "$RUN_ID"
```

`--parallelism 8` allows eight Cells to be active. The controller deliberately
staggers new VM materialization across reconciliation cycles; this reduces
devmapper pressure without reducing steady-state concurrency. A task may stay
`Queued` until capacity for both of its VMs is available.

The foreground command waits for the batch. To survive SSH disconnects:

```bash
mkdir -p .artifacts

nohup bash scripts/run-swe-rebench.sh \
  --tasks ../ClawTune/swe_rebench/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-cidr "$PROVIDER_CIDR" \
  --profile small \
  --parallelism 8 \
  --sample 8 \
  --timeout-seconds 120 \
  --command-timeout-seconds 120 \
  --run-id "$RUN_ID" \
  >".artifacts/${RUN_ID}.results.json" \
  2>".artifacts/${RUN_ID}.log" </dev/null &

echo $! >".artifacts/${RUN_ID}.pid"
echo "$RUN_ID"
```

Monitor independently of the launcher process:

```bash
kubectl -n clawbox-benchmarks get sandboxtasks \
  -l "clawbox.openai.com/run=${RUN_ID}" \
  -o custom-columns='NAME:.metadata.name,INSTANCE:.metadata.annotations.clawbox\.openai\.com/original-instance-id,PHASE:.status.phase,OUTCOME:.status.outcome,REASON:.status.reason'
```

Add `-w` to watch. Tool and Runtime Pods are deleted after collection;
`SandboxTask` remains as the audit record.

Cancel all active tasks in the batch:

```bash
while read -r task; do
  kubectl -n clawbox-benchmarks patch "$task" \
    --type=merge -p '{"spec":{"desiredState":"Cancelled"}}'
done < <(kubectl -n clawbox-benchmarks get sandboxtasks \
  -l "clawbox.openai.com/run=${RUN_ID}" -o name)
```

Stopping the local launcher does not cancel tasks already created in the
cluster.

### 3. Submit one asynchronous Managed API task

This path always uses the fixed task image stored in template
`swe-rebench-arm64`. Use it only when that image matches the requested
repository. Use the mapped batch runner above when the task image varies by
instance.

Inspect the saved template:

```bash
kubectl -n clawbox-system get secret clawbox-managed \
  -o jsonpath='{.data.templates}' | base64 -d
echo
```

Submit and keep the returned `runId`:

```bash
scripts/clawbox submit \
  --input-ref INSTANCE_ID \
  --problem-file /path/to/problem-statement.txt \
  --deadline-seconds 120
```

Submission is asynchronous. Manage it with:

```bash
scripts/clawbox status RUN_ID
scripts/clawbox attempts RUN_ID
scripts/clawbox events RUN_ID
scripts/clawbox watch RUN_ID --timeout 900
scripts/clawbox cancel RUN_ID
```

Ctrl+C stops only the local watcher. `retry RUN_ID` creates a new attempt for
a finished run. Use `--idempotency-key KEY` only when retrying the same HTTP
submission after an uncertain client/network failure.

### 4. Retrieve task results

Set `TASK_ID` to the `SandboxTask` name:

```bash
mkdir -p .artifacts
TASK_ID='swe-run-id-instance-hash'
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

curl -fsS -H "Authorization: Bearer ${TOKEN}" \
  "http://127.0.0.1:8084/v1/archive/${TASK_ID}/result" \
  -o ".artifacts/${TASK_ID}.result.json"
curl -fsS -H "Authorization: Bearer ${TOKEN}" \
  "http://127.0.0.1:8084/v1/archive/${TASK_ID}/traces" \
  -o ".artifacts/${TASK_ID}.traces.json"

kill "$PORT_FORWARD_PID"
unset TOKEN
```

## Updating an installed host

### Deploy new ClawBox code

Build and publish from clean, pinned ClawBox and ClawTune revisions, then let
`up` consume `.artifacts/platform-images.env`:

```bash
source .venv/bin/activate
python3 -m pytest -q

export REGISTRY=127.0.0.1:5000/clawbox
export CLAWTUNE_ROOT="$PWD/../ClawTune"
export TAG="$(git rev-parse --short=12 HEAD)"
export PUSH=1
bash scripts/build-kubernetes-images.sh

scripts/clawbox up
scripts/clawbox doctor
```

Never copy Python source into a running container. Rebuild both the control
plane and Runtime when their code changes; immutable digests prevent stale
tags and bytecode from surviving an update.

### Repair an older containerd unit once

New bootstrap runs already install this configuration. Use these commands
only on an older installed host where the following check prints nothing:

```bash
systemctl cat containerd.service | grep 'DM_DISABLE_UDEV=1'
```

First wait until no ClawBox task Pods or Jobs are active. Then install the
repository-owned unit and restart containerd during a maintenance window:

```bash
kubectl -n clawbox-benchmarks get pods,jobs
sudo install -m 0644 deploy/containerd-clawbox.service \
  /etc/systemd/system/containerd.service
sudo systemctl daemon-reload
sudo systemctl restart containerd
sudo systemctl restart kubelet

systemctl show containerd.service -p Environment | grep 'DM_DISABLE_UDEV=1'
scripts/clawbox up
scripts/clawbox doctor
```

Do not restart containerd while task VMs are running.

## Troubleshooting

### Task stays Queued

```bash
kubectl -n clawbox-benchmarks get sandboxtask TASK_NAME -o yaml
kubectl -n clawbox-system logs deployment/clawbox-cell-controller --tail=300
```

- `PendingAdmission`: waiting for a controller cycle;
- `InsufficientCellCapacity`: insufficient CPU, memory, devmapper storage, or
  Pod capacity for both VMs;
- `DiskPressure=True`: free host disk space before submitting more work.

### Sandbox or devmapper creation fails

Stop submitting new tasks and inspect before deleting anything:

```bash
kubectl -n clawbox-benchmarks get events --sort-by=.lastTimestamp \
  | grep -E 'FailedCreatePodSandbox|devmapper' | tail -100
sudo bash scripts/setup-devmapper-openeuler-arm64.sh status
systemctl show containerd.service -p Environment
df -h /
sudo du -sh /var/crash /var/lib/containerd /var/lib/kubelet 2>/dev/null
```

Do not run global Docker/containerd prune commands. Old crash dumps under
`/var/crash` can be very large; remove only exact files after preserving any
diagnostic data that is still needed.

### Failed Pods accumulated

Preview, then delete only Failed Pods in the named ClawBox namespace:

```bash
python3 scripts/cleanup-failed-pods.py
python3 scripts/cleanup-failed-pods.py \
  --namespace clawbox-system --apply
```

Do not clean unrelated namespaces without identifying their owner and cause.

## Lifecycle and development references

The current controller has explicit capacity ownership and workload
materialization boundaries so a future suspend/resume implementation can
checkpoint and release VMs without reusing ambiguous terminal states. See
[ADR 004](docs/adr/004-cell-suspend-resume-boundary.md).

Development checks:

```bash
python3 -m pytest -q
cd toolbridge && go test -race ./...
```

- Manual manifest deployment: [deploy/README.md](deploy/README.md)
- Execution and telemetry invariants: [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)
- Capacity scale gate: `scripts/scale-swe-rebench.sh`
- Managed API load test: `scripts/load-test.sh`
