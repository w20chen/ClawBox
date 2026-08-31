# ClawBox

ClawBox runs coding agent tasks on one native ARM64 Kubernetes node. Each task is represented by a custom Kubernetes object `SandboxTask` and backed by two isolated Kata/Firecracker virtual machines:

- the **Runtime VM** runs OpenClaw and ClawTune;
- the **Tool VM** contains the repository and executes commands;
- the Cell controller creates, monitors, collects, and cleans up both VMs.

The supported production target is a dedicated ARM64 host (e.g., Kunpeng).
There is no supported x86, QEMU, runc, or multi-node fallback.

## ClawBox & ClawTune

ClawBox owns task isolation and lifecycle: the Cell controller creates, monitors, collects, and cleans up the Runtime and Tool VMs. OpenClaw controls the agent loop inside the Runtime VM and sends commands directly to Tool Bridge over SSH.

[ClawTune](https://github.com/w20chen/ClawTune) records model, tool, and clause activity in the Runtime VM. Tool Bridge executes commands in the Tool VM and collects guest-side resource telemetry. At task completion, the Runtime VM pairs these artifacts by execution identity, signs them, and uploads them for validation and storage.

Later tasks from the same tenant and repository may load the resulting knowledge base generation. `fixed-resident` remains the production default. The opt-in research baselines `p90-static` and `p90-elastic` enforce bounded Tool-VM CPU and memory predictions; Runtime resources remain fixed.

## Execution architecture

A workflow explicitly selects independent axes: workload source, agent driver,
inference backend, sandbox backend, tool transport, admission policy,
residency policy, and result collection. A baseline resolves only admission
and residency policy; it never changes workload, provider, backend, image,
timeout, or concurrency.

### Canonical axes

The schema names all currently relevant values, including values retained for
historical code or future work. A name appearing here does **not** mean every
combination containing it is executable; the centralized capability validator
classifies the complete workflow.

| Axis | Values | Current boundary |
| --- | --- | --- |
| Workload source | `swe_rebench`, `recorded_trace`, `synthetic` | All three parse into `WorkloadCase`; `synthetic` has no comparable production/paper runner. |
| Agent driver | `openclaw`, `replay_engine` | OpenClaw is the comparable production/paper driver; ReplayEngine is historical mechanism testing. |
| Inference backend | `api`, `replay` | Both are implemented for the paper path; production uses API. |
| Sandbox backend | `kubernetes`, `direct_firecracker`, `local` | Kubernetes is production, direct Firecracker is research/historical, local is testing/historical. |
| Tool transport | `ssh`, `vsock`, `local`, `kubectl` | Production and paper use SSH; the others belong to ReplayEngine/testing paths. |
| Admission policy | `fixed_profile`, `fixed_explicit`, `p90_static`, `p90_elastic` | All are active. P90 policies require authoritative native ClawTune evidence and are research-only. |
| Residency policy | `resident`, `llm_wait_checkpoint`, `pressure_checkpoint` | Resident and direct-Firecracker LLM-wait checkpoint are active; pressure checkpoint is not implemented. |
| Result collection | `ResultEnvelope` plus runner artifacts | Production and paper retain their detailed outputs and add the same outer envelope. |

### Implemented workflow combinations

These are the combinations that are represented or recognized by the
canonical model today:

| Classification | Workload / driver | Inference | Sandbox / transport | Admission / residency | Execution status |
| --- | --- | --- | --- | --- | --- |
| Production | `swe_rebench` / `openclaw` | `api` | `kubernetes` / `ssh` | `fixed_profile` / `resident` | Implemented and accepted. |
| Research | `swe_rebench` / `openclaw` | `api` | `kubernetes` / `ssh` | `p90_static` or `p90_elastic` / `resident` | Implemented opt-in Cell sizing baselines. |
| Research | `recorded_trace` / `openclaw` | `replay` or `api` | `direct_firecracker` / `ssh` | `fixed_explicit` / `resident` | Implemented paper arm. |
| Research | `recorded_trace` / `openclaw` | `replay` or `api` | `direct_firecracker` / `ssh` | `fixed_explicit` / `llm_wait_checkpoint` | Implemented paper arm. |
| Research | `recorded_trace` / `openclaw` | `replay` or `api` | `direct_firecracker` / `ssh` | `p90_static` / `resident` or `llm_wait_checkpoint` | Implemented predictive-memory paper arms. |
| Historical | `recorded_trace` / `replay_engine` | existing replay/API providers | `direct_firecracker` or `local` / explicitly selected transport | ReplayEngine's existing policy | Retained for mechanism testing; not comparable with a Cell. |
| Local-only | `synthetic` or targeted smoke input | as required by the helper | `local` or direct Firecracker | helper-specific | Targeted tests only, not a benchmark baseline. |

For one inference backend, the sizing/residency factorial has four comparable arms:

```text
fixed      + resident
fixed      + llm_wait_checkpoint
p90_static + resident
p90_static + llm_wait_checkpoint
```

Paper studies should add `fixed_control_tool_memory_mib` when the prediction
hits a safety floor. This creates an untrained fixed-size control at that same
memory limit under both residency policies, separating the value of the sizing
decision from the value of merely choosing a smaller VM. Its compact output
label is `fixed2` (the configured MiB value remains authoritative); compact arm
labels keep Firecracker API socket paths within the Unix-domain limit.

The following remain explicitly **not implemented**: `pressure_checkpoint`,
Kubernetes checkpoint residency, and local Firecracker checkpoint residency.
The validator rejects them before a runner starts.

### Baseline registry

A baseline is immutable policy data, not a runner. It controls only the final
two policy columns below:

| Baseline | Admission policy | Residency policy | Status |
| --- | --- | --- | --- |
| `fixed-resident` | `fixed_profile` | `resident` | Implemented for the production Cell. |
| `fixed-explicit-resident` | `fixed_explicit` | `resident` | Implemented for direct-Firecracker research. |
| `fixed-llm-wait-checkpoint` | `fixed_explicit` | `llm_wait_checkpoint` | Implemented for direct-Firecracker research. |
| `p90-static` | `p90_static` | `resident` | Implemented research Cell baseline; requires an exact KB generation. |
| `p90-static-llm-wait-checkpoint` | `p90_static` | `llm_wait_checkpoint` | Implemented direct-Firecracker research baseline. |
| `p90-elastic` | `p90_elastic` | `resident` | Implemented research Cell baseline; resolves latest once at admission, then freezes it. |
| `p90-elastic-pressure-checkpoint` | `p90_elastic` | `pressure_checkpoint` | Not implemented. |

The accepted production workflow is:

```text
swe_rebench + openclaw + api + kubernetes + ssh
+ fixed_profile + resident
```

The active paper workflow is:

```text
recorded_trace + openclaw + (replay | api) + direct_firecracker + ssh
+ fixed_explicit + (resident | llm_wait_checkpoint)
```

Fixed sizing remains the production default; P90 enforcement is explicit and
research-classified. Kubernetes VM checkpointing and `pressure_checkpoint`
remain rejected during validation. The historical ReplayEngine
and Tool-only scheduler/allocator/controller paths remain available but are
not comparable production Cells.

See [Execution architecture](docs/execution-architecture.md) for the complete
runner inventory, canonical vocabulary, baseline registry, capability table,
and authoritative/legacy persistence boundary.

### Complete canonical examples

This production specification resolves to one workflow. The existing
SWE-ReBench launcher obtains the case-specific Tool image from the ARM64
mapping and the Runtime image/provider configuration from the installed Cell
configuration.

```json
{
  "schema_version": 1,
  "workload": {
    "source": "swe_rebench",
    "input": "/data/tasks.json",
    "repetitions": 1
  },
  "agent": {"driver": "openclaw"},
  "inference": {"backends": ["api"]},
  "sandbox": {
    "backend": "kubernetes",
    "tool_transport": "ssh"
  },
  "scheduling": {"baselines": ["fixed-resident"]},
  "execution": {
    "concurrency": 8,
    "timeout_seconds": 1800,
    "command_timeout_seconds": 300
  },
  "resources": {"profile": "small"},
  "validation": {
    "command": "cd /testbed && git diff --binary --no-ext-diff HEAD"
  },
  "output": {"directory": "/data/production-run"}
}
```

This paper specification resolves to the four arms listed above. Rootfs and
prompt paths are backend materialization fields, not workload fields.

```json
{
  "schema_version": 1,
  "workload": {
    "source": "recorded_trace",
    "input": "/data/workloads/model-trace.jsonl",
    "repetitions": 3
  },
  "agent": {"driver": "openclaw"},
  "inference": {
    "backends": ["replay", "api"],
    "configuration": {
      "replay": {"time_scale": 1.0},
      "api": {
        "base_url": "https://PROVIDER_BASE_URL/v1",
        "model": "PROVIDER_MODEL_ID",
        "key_env": "OPENAI_API_KEY"
      }
    }
  },
  "sandbox": {
    "backend": "direct_firecracker",
    "tool_transport": "ssh",
    "materialization": {
      "runtime_rootfs": "/data/openclaw-runtime.ext4",
      "tool_rootfs": "/data/tool-workspace.ext4",
      "prompt": "/data/workloads/prompt.txt",
      "network_prefix": "172.30",
      "exposed_model": "experiment-model"
    }
  },
  "scheduling": {
    "baselines": [
      "fixed-explicit-resident",
      "fixed-llm-wait-checkpoint"
    ]
  },
  "execution": {
    "concurrency": 8,
    "timeout_seconds": 900,
    "command_timeout_seconds": 300
  },
  "resources": {
    "runtime_memory_mib": 2048,
    "tool_memory_mib": 4096,
    "cpu_first": 0,
    "numa_node": 0
  },
  "validation": {
    "command": "cd /testbed && git diff --binary --no-ext-diff HEAD"
  },
  "output": {"directory": "/data/paper-study-001"}
}
```

The canonical CLI is a read-only planner: it parses, resolves, validates, and
prints workflows but never starts Kubernetes or Firecracker. Execution remains
in the existing runners. The production launcher constructs the production
workflow adapter automatically; the paper runner currently translates the
backward-compatible `deploy/study.example.json` format into the canonical
model. There is intentionally no generic runner for arbitrary axis
combinations.

## Start here

All commands below run from the repository root on the ARM64 host. The project
has two independent workflows:

1. **Run real coding tasks:** Kubernetes creates two isolated virtual machines
   per task, OpenClaw drives the task, and ClawTune records the execution.
2. **Run the paper experiment:** previously recorded actions are executed in
   directly managed Firecracker VMs. The experiment compares recorded timing
   with a real model API, and compares keeping VMs in memory with saving idle
   VMs to disk.

The paper workflow is ready for experimental use. With either replay or API
inference, OpenClaw and ClawTune run inside the Runtime VM, while commands run
in the Tool VM over SSH. Only the source of model responses changes. This exact path has passed short resident,
checkpoint/restore, and real-API runs on Kunpeng.

Use these entry points instead of assembling lower-level commands:

| Goal | Command |
| --- | --- |
| Check an installed host without changing it | `scripts/clawbox doctor` |
| Start services after a reboot | `scripts/clawbox up` |
| Submit one real task | `scripts/clawbox submit ...` |
| Run a real task batch | `bash scripts/run-swe-rebench.sh ...` |
| Download a task's traces and reports | `scripts/clawbox traces TASK_ID` |
| Run one paper matrix | `python3 -m clawbox.replay.cli study STUDY.json` |
| Run multi-trace NUMA/concurrency sweep | `python3 -m clawbox.replay.cli suite SUITE.json` |

The main documentation flow is:

- [Install a new host](#new-host-one-time-installation)
- [Run real tasks on an installed host](#existing-host-start-and-run-tasks)
- [Run the paper experiment](#paper-experiment-save-idle-virtual-machines)
- [Update an installation](#updating-an-installed-host)
- [Troubleshoot](#troubleshooting)

Container images used for real tasks must be immutable references of the form
`IMAGE@sha256:...`.

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
git -C ClawTune checkout --detach 76eab6fa5c6333f4e80901c030f10cab0e4ce605
cd ClawBox

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev,postgres]'
python3 -m pytest -q
```

If pip reports `Missing dependencies for SOCKS support` or connection refused
to `127.0.0.1:1080`, the shell has a SOCKS proxy configured but its tunnel is
not running. Either start the intended tunnel or disable that stale proxy for
the current shell before retrying:

```bash
ss -lnt | grep ':1080 ' || true
unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy
python3 -m pip install -e '.[dev,postgres]'
```

If the proxy exports come from `~/.bashrc`, remove/comment them when the proxy
is no longer used; otherwise every new shell will fail in the same way. Do not
unset a working proxy on a host that requires it for outbound access.

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

Obtain the base URL and model name from the provider's current documentation.
Do not copy model names from an old installation: providers can rename or
retire them.

Choose one task image from the mapping for the Managed API's default
single-task template, then install:

```bash
read -rsp 'DeepSeek API key: ' CLAWBOX_LLM_API_KEY
echo
export CLAWBOX_LLM_API_KEY
export CLAWBOX_LLM_BASE_URL='https://PROVIDER_BASE_URL'
export CLAWBOX_LLM_MODEL='PROVIDER_MODEL_ID'
export CLAWBOX_OPENCLAW_MODEL_REF='vllm/PROVIDER_MODEL_ID'
export CLAWBOX_TOOL_IMAGE='127.0.0.1:5000/clawbox/TASK@sha256:DIGEST'
read -rp 'Approved provider CIDR (for example 203.0.113.10/32): ' \
  CLAWBOX_LLM_EGRESS_CIDR
export CLAWBOX_LLM_EGRESS_CIDR

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
mkdir -p .artifacts
.venv/bin/python scripts/make-concurrency-smoke-tasks.py \
  --tasks ../ClawTune/swe_rebench/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --count 2 \
  --output .artifacts/concurrency-smoke-2.json

RUN_ID="smoke-$(date -u +%Y%m%d%H%M%S)"

bash scripts/run-swe-rebench.sh \
  --tasks .artifacts/concurrency-smoke-2.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-host api.deepseek.com \
  --parallelism 2 \
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

For an infrastructure concurrency smoke, generate eight uniquely named copies
of the first task present in the ARM64 mapping. This validates concurrent VM
creation and LLM calls; repeated tasks are not valid benchmark scores.

```bash
mkdir -p .artifacts
.venv/bin/python scripts/make-concurrency-smoke-tasks.py \
  --tasks ../ClawTune/swe_rebench/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --count 8 \
  --output .artifacts/concurrency-smoke-8.json

export RUN_ID="swe-$(date -u +%Y%m%d%H%M%S)"

bash scripts/run-swe-rebench.sh \
  --tasks .artifacts/concurrency-smoke-8.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-host api.deepseek.com \
  --profile small \
  --parallelism 8 \
  --timeout-seconds 120 \
  --command-timeout-seconds 120 \
  --run-id "$RUN_ID"
```

`--parallelism 8` allows eight tasks to be active. The controller deliberately
staggers new VM materialization across reconciliation cycles; this reduces
devmapper pressure without reducing steady-state concurrency. A task may stay
`Queued` until capacity for both of its VMs is available.

`--llm-egress-host` resolves every current public IPv4 address immediately
before submission. The launcher rejects private, loopback, malformed, empty,
or excessive results, records the canonical `/32` egress address snapshot in
every `SandboxTask`, and limits Runtime egress to those addresses on port 443.
This removes per-run CIDR input while remaining fail-closed. Re-run the
launcher for each batch so provider DNS changes are captured; never replace this with
`0.0.0.0/0`.

The runner clears inherited HTTP(S)/SOCKS proxy variables only in its own
process before connecting to the local Kubernetes API. This prevents a stale
login-shell proxy from breaking submission and does not modify shell startup
files or Runtime network policy.

For real SWE-ReBench evaluation, first build mappings for every selected task,
then pass the original task file and optionally `--sample N`. The launcher
fails closed when any selected task lacks an ARM64 mapping.

The foreground command waits for the batch. To survive SSH disconnects:

```bash
mkdir -p .artifacts

nohup bash scripts/run-swe-rebench.sh \
  --tasks .artifacts/concurrency-smoke-8.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-host api.deepseek.com \
  --profile small \
  --parallelism 8 \
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

### 3. Submit one asynchronous task through the web API

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

Set `TASK_ID` to the Kubernetes task-object name, then export all archived
trace files with one host command. The command obtains the cluster credential,
creates a temporary local connection, downloads and checks the files, and
closes the connection automatically:

```bash
TASK_ID='swe-run-id-instance-hash'
scripts/clawbox traces "$TASK_ID"
```

Files are written below `.artifacts/${TASK_ID}.traces/`. This includes JSONL,
Linux kernel event telemetry, control-group resource records, and generated
reports when the task uploaded them. Pass a second argument to choose another
output directory. The command creates and closes its temporary local
connection automatically.

Each batch item retains its existing SandboxTask result fields and now also
contains `resolved_workflow` plus a versioned `result_envelope`. The envelope
records the actual batch concurrency, case-specific Tool image, stable status,
failure category, provenance, artifacts, and backend details.

## Plan an experiment

The canonical experiment CLI is read-only. It validates and expands JSON
configuration but never starts a sandbox:

```bash
python3 -m clawbox.experiments.cli list-baselines
python3 -m clawbox.experiments.cli validate experiment.json
python3 -m clawbox.experiments.cli resolve experiment.json
python3 -m clawbox.experiments.cli matrix experiment.json
```

`resolve` accepts one implemented workflow; `matrix` expands explicitly
selected inference backends and scheduling baselines. Unsupported combinations
fail before a runner is started.

## Paper experiment: save idle virtual machines

This experiment is separate from the Kubernetes task service, but preserves
the task architecture. Every session has two Firecracker virtual machines: the
Runtime VM runs OpenClaw and ClawTune; the Tool VM owns `/testbed` and executes
commands received over SSH.

OpenClaw always controls the agent loop. `inference_backend=replay` supplies
saved model responses through an OpenAI-compatible endpoint;
`inference_backend=api` forwards the same requests to a real service. Prompts,
tools, SSH, instrumentation, VM images, and agent settings therefore stay the
same.

The experiment varies three independent dimensions:

| Dimension | Baseline | Alternative |
| --- | --- | --- |
| Inference backend | `replay` recorded responses and latency | `api` through a real OpenAI-compatible service |
| Admission/accounting policy | static reservation equal to fixed Tool capacity | `p90_reservation` per-tool immutable ClawTune P90 reservation |
| Residency policy | `resident` keeps both VMs in memory | `llm_wait_checkpoint` checkpoints idle VMs and restores them before the next command |

During an outstanding model request, `llm_wait_checkpoint` first checkpoints
the Runtime VM and then its Tool dependency, exits both Firecracker processes,
restores Tool before Runtime, and consumes the response only afterward. This
dependency order prevents a resumed model response from dispatching work while
Tool is unavailable. The model gateway identifies requests by content and
stores responses, so retrying an interrupted HTTP connection cannot duplicate
a real API call.

This is a single-host, NUMA-controlled, fail-stop research executor. It reads a
frozen manifest, leases fixed Runtime/Tool CPU and memory pairs, and fails the
entire experimental arm on an unexpected runner or VM failure. It is not a
long-running scheduler or a production control plane: it does not claim agent
restart recovery, high-availability reconciliation, cross-node migration, or
Kubernetes checkpointing. Those properties are outside the registered paper
mechanism and are not prerequisites for a valid arm; rerun a failed arm from
fresh disks instead of recovering it in place.

Tool VM capacity is fixed for an arm; the runner does not resize a persistent
Firecracker VM between commands. `p90_reservation` instead gates each concrete
tool invocation against a shared FIFO accounting budget. It resolves an
incremental P90 by the frozen KB hierarchy (exact/prefix command, program,
tool, then global fallback). Before releasing a tool call, admission requires
`live Tool-Firecracker RSS + outstanding incremental commitments + the new
incremental P90 + safety headroom <= budget`. A 100 ms host sampler feeds RSS
growth back into the gate and blocks later admissions after an overrun. At
tool completion the gate remeasures RSS before dropping only the future-growth
commitment; it never assumes that guest-free pages left Firecracker RSS.
Static controls use their full 4 GiB or 2 GiB Tool capacity as the incremental
commitment. Actual per-command cgroup peaks are measured separately.
Checkpointing is the main long-idle reclamation mechanism, and every eviction
records that both Firecracker process RSS values reached zero plus the observed
cgroup and NUMA-local memory change. An optional resident-only virtio-balloon
baseline reclaims guest-cooperative free pages at tool completion while keeping
the VM alive; it reports host RSS separately from guest balloon statistics.

### What this experiment does not do

- It does not suspend a Kubernetes Pod. The experimental virtual machines are
  managed directly through the Firecracker API.
- Replay inference does not run a model, but it runs the real OpenClaw loop, tool
  calls, SSH backend, and ClawTune instrumentation.
- API mode forwards model traffic through a host gateway so request identity
  survives save/restore. GPU placement and provider-side cache management are
  outside this project.
- Saving memory can briefly increase peak host memory while files are written.
  Paper results must report peak as well as average memory.

### 1. Prepare the two reusable VM disks

Build the platform images first as described above. Export the Runtime image
containing OpenClaw and ClawTune, and the workload image containing the
repository and SSH tool service. Replace the example image references with the
exact images built for the experiment.

```bash
python3 scripts/build-oci-firecracker-rootfs.py \
  --image REGISTRY/runtime-arm64:TAG \
  --output /data/openclaw-runtime.ext4 --size-mib 6144 \
  --inject-file scripts/experiment-runtime-init.sh:/usr/local/bin/experiment-runtime-init

python3 scripts/build-oci-firecracker-rootfs.py \
  --image REGISTRY/workload-arm64:TAG \
  --output /data/tool-workspace.ext4 --size-mib 16384 \
  --inject-file scripts/experiment-tool-init.sh:/usr/local/bin/experiment-tool-init
```

Every experiment group receives fresh copies of these disks. Never reuse a
copy that has already executed a task.

### 2. Prepare the workload

Create a plain-text prompt and a ClawTune JSONL trace. The trace must contain a
complete model response for every turn, including tool-call arguments, plus
the measured duration. Inspection is read-only:

```bash
python3 -m clawbox.replay.cli inspect /data/workloads/model-trace.jsonl
```

Replay is faithful at the OpenClaw API boundary, not merely a sleep schedule:
OpenClaw receives recorded assistant text and tool calls, executes tools over
SSH, sends tool results into the next request, and waits for recorded latency.
Newly recorded traces also retain the model request payload. Replay compares
each live OpenClaw request with the recorded request and fails closed on
divergence. Legacy response-only traces remain usable, but their manifests
must disclose that request equality could not be checked; final-state equality
is still enforced.

### 3. Create the private VM networks

Create one isolated bridge and two TAP devices per concurrent session. Keep
them up for all experiment groups and remove them only after every VM exits.

```bash
sudo bash scripts/direct-firecracker-network.sh up --sessions 8 --prefix 172.30
```

### 4. Configure and run the comparison

Copy the example, then edit its paths, session count, repetition count, and
model-service settings. The output path must not already exist.

```bash
cp deploy/study.example.json /data/study.json
${EDITOR:-vi} /data/study.json
```

The configuration names correspond to these general experiment concepts:

| Configuration field | Meaning |
| --- | --- |
| `source` | the two reusable VM disks, prompt, and recorded model trace |
| `sessions`, `repetitions`, `seed` | concurrent workload size, repeat count, and randomized group order |
| `inference_backends` | recorded timing, real model API, or both |
| `sizing_policies` | `fixed`, `p90_reservation`, or both (`p90_static` is a legacy config alias) |
| `memory_policies` | legacy input spelling for `resident` and `llm_wait_checkpoint`; `snapshot` remains an alias |
| `resources` | VM memory size, CPU numbering, and NUMA placement |
| `fixed_control_tool_memory_mib` | optional untrained fixed-capacity/static-reservation control below the conservative fixed size |
| `tool_reservation_budget_mib` | NUMA-local Tool admission budget applied to live RSS plus outstanding incremental demand |
| `tool_admission_safety_headroom_mib` | unallocatable safety margin retained below the Tool admission budget |
| `idle_tool_vm_rss_mib` | measured idle Tool-VM RSS anchor used for capacity and oracle analysis, not subtracted at tool completion |
| `tool_balloon_reclamation`, `tool_balloon_idle_floor_mib` | optional resident predictive baseline that deflates before tool execution and inflates back to the measured idle floor after tool completion; fixed VM capacity is unchanged |
| `resident_memory_budget_mib` | optional configured admission budget; excess sessions queue until a VM pair is resident or checkpointed |
| `workloads[].independent_unit` | independent task ID used for inference; repeated trajectories share one ID |
| `numa_host_reserve_mib`, `max_numa_cpu_busy_fraction`, `require_no_firecracker` | clean-host admission bounds for timing runs |
| `require_parent_numa_binding` | reject a suite whose parent/helpers were not launched on the selected NUMA node |
| `snapshot_disk_reserve_mib` | free space retained beyond two full alternating snapshot generations |
| `require_held_out_predictions` | require the prediction artifact to name and hash held-out evaluation traces; the research report must state whether separation is by task or only by recording set |
| `resume` | reuse only completed child studies in a long suite |
| `replay.time_scale` | recorded latency multiplier; use `1.0` for measurements |
| `validation_command` | command whose output represents the final task state |
| `api` | OpenAI-compatible endpoint, model, and credential variable |

The `p90_reservation` configuration block supplies the frozen KB
artifact used by `p90_reservation`. It must contain heterogeneous per-tool
reservations and an explicit fixed VM capacity class. Export its base evidence from
the authoritative Tune-KB service, keep the returned generation and digests
with the paper artifacts, and never substitute a newer generation mid-study:

```bash
kubectl -n clawbox-system port-forward service/clawbox-tune-kb 18082:8082
# In another shell, set CLAWBOX_KB_TOKEN from the installed secret without
# writing it to the study configuration.
clawbox-p90-export --endpoint http://127.0.0.1:18082 \
  --tenant TENANT --repository OWNER/REPO --generation GENERATION \
  --output /data/workloads/p90-admission-prediction.json
```

Static sizing pins the exact generation. The Kubernetes `p90-elastic` baseline
is different: it resolves latest exactly once at admission and persists the
full decision in `SandboxTask.status.sizingDecision`.

For offline paper evidence, train from recovered, independently measured
ClawTune span/bridge/cgroup artifacts. Keep training recordings disjoint from
the replay recordings. This command embeds evidence counts, join diagnostics,
source digests, recording-set identities, and evaluation-trace digests in the
prediction file:

```bash
python3 scripts/train-p90-from-runs.py \
  /data/train-recording/session-0000 \
  /data/train-recording/session-0001 \
  /data/train-recording/session-0002 \
  --repository OWNER/REPO \
  --observed-repo-fingerprint TRACE_REPO_FIELD \
  --training-set-id recording-train \
  --evaluation-set-id recording-eval \
  --evaluation-trace short-fix=/data/eval/short-fix.model.jsonl \
  --evaluation-trace test-debug=/data/eval/test-debug.model.jsonl \
  --evaluation-trace multi-file=/data/eval/multi-file.model.jsonl \
  --output /data/workloads/p90-independent-eval.json
```

The trainer rejects unexpected raw trace repository identities and hashes the
span, bridge, and cgroup inputs, including incomplete artifacts, into immutable
provenance. The suite can require the independent recording-set protocol with
`require_held_out_predictions: true`.

For a recorded-only trial, set `inference_backends` to `["replay"]` and no API
key is needed. For a real-API comparison, retain both values and export the key
named by `api.key_env`. Choose exactly one of the following runs.

Recorded timing only:

```bash
python3 -m clawbox.replay.cli study /data/study.json
```

Recorded timing plus the real API:

```bash
read -rsp 'Model API key: ' OPENAI_API_KEY; echo
export OPENAI_API_KEY
python3 -m clawbox.replay.cli study /data/study.json
unset OPENAI_API_KEY
```

The runner randomizes group order, creates fresh VM disks for every group, and
writes intermediate results. The final `study-summary.json` contains:

- completed sessions and failures;
- wall time, completed agent runs/min (the compatibility field is
  `throughput_tasks_per_minute`), and model steps/min (one step is one
  completed model request); this is not a correctness score;
- average and peak Firecracker process memory;
- P95 Firecracker RSS, RSS-time integral, resident-VM peak, NUMA-local memory,
  and experiment-cgroup memory deltas;
- paired checkpoint cycles, individual VM save/restore operation counts, and
  summed VM service time (not critical-path overhead);
- means, sample standard deviations, and two-sided 95% Student-t confidence
  intervals across repetitions;
- commit, source-tree hash, trace hash, host identity, and full configuration;
- hashes produced by `validation_command` for cross-group final-state checks.

Each arm also writes `result-envelopes.json`, with one envelope for every
successful or failed session. The resolved workflow records the selected
inference configuration, VM materialization, resources, concurrency,
validation, and `vm_checkpoints` metric; existing study files remain the
authoritative detailed artifacts.

### 5. Multi-trace, full-NUMA throughput sweep

Use `deploy/replay-suite.example.json` for paper throughput results. It crosses
at least two recorded workloads, concurrency levels, fixed/P90 sizing, and
resident/checkpoint policy, while each child study randomizes arm order and
repeats measurements. On the four-node Kunpeng reference host, NUMA 0 contains
CPUs `0-79`; each task has one Runtime vCPU and one Tool vCPU, so 40 tasks is
the exclusive-CPU ceiling. The suite reads `/sys` and fails before starting if
either CPU placement escapes the selected node or configured guest memory
exceeds node-local memory after `numa_host_reserve_mib`.

Before allocating an output directory, `--validate-only` checks every input
file and prediction digest, held-out recording provenance, NUMA CPU/memory
bounds, current node-local free memory, pre-existing Firecracker processes,
and a sampled NUMA-local CPU-busy limit. It performs no experiment writes:

```bash
numactl --cpunodebind=0 --membind=0 \
  python3 -m clawbox.replay.cli suite /data/replay-suite.json --validate-only
```

```bash
cp deploy/replay-suite.example.json /data/replay-suite.json
${EDITOR:-vi} /data/replay-suite.json
sudo bash scripts/direct-firecracker-network.sh up --sessions 40 --prefix 172.30
numactl --cpunodebind=0 --membind=0 \
  python3 -m clawbox.replay.cli suite /data/replay-suite.json
sudo bash scripts/direct-firecracker-network.sh down --sessions 40 --prefix 172.30
```

Set `resume: true` for long experiments. A rerun reuses only child studies
that already contain a complete `study-summary.json`; it fails closed on a
partial child directory or changed generated configuration. Each
workload/concurrency block receives a deterministic derived seed, so arm order
varies between blocks while remaining reproducible. Clean-host CPU and
Firecracker checks repeat before every new block.

Suites stop after the first failed workload/concurrency block by default;
`continue_after_block_failure: true` is an explicit diagnostic-only opt-out.
Keep ordinary Firecracker control calls short, but give synchronous full-image
snapshot create/load calls a separate I/O timeout. The reference configs use
`firecracker_api_timeout_s: 15`,
`firecracker_snapshot_api_timeout_s: 300`, and a 7200-second per-session bound;
the longer snapshot timeout prevents valid concurrent multi-GiB writes from
being misclassified as failed API calls.

Progress inspection is read-only and safe while the suite is running:

```bash
python3 scripts/summarize-replay-suite.py /data/clawbox-paper-suite-001
```

If a previous verified setup already left some session networks in place, use
`--reuse-existing`. The helper validates every existing bridge address and TAP
membership and still fails closed on partial or mismatched state:

```bash
sudo bash scripts/direct-firecracker-network.sh up --sessions 40 \
  --prefix 172.30 --reuse-existing
```

The primary exclusive-CPU sweep is `[1, 8, 20, 40]`, three repetitions,
`time_scale=1.0`,
and at least three traces chosen before viewing results. Prefer distinct tasks:
a short localized fix, a test/debug task, and a multi-file change. Multiple
model trajectories from one task must share the same `independent_unit`; they
are robustness repeats, not independent workloads. Report each trajectory and
concurrency separately, then average trajectories within each independent task
before macro inference; do not pool individual steps. With only one independent
task, the confidence interval is explicitly not estimable. `suite-summary.json` records measured NUMA
topology, clean-host preflight samples, frozen prediction provenance, and links
every child `study-summary.json`. The preparation path uses
XFS copy-on-write clones where supported so fresh 6-16 GiB VM disks do not turn
the sweep into an avoidable storage benchmark.

Preflight hashes both reusable rootfs images plus every prompt and replay
trace, and records the pinned Tool-repository commit. This takes noticeable
time for multi-gigabyte images but prevents silently mixing image builds.

The suite also writes `measurements.csv`, one row per randomized arm
repetition, and `macro_statistics` in `suite-summary.json`. Macro confidence
intervals use one mean per pre-registered independent task as the unit; model
steps and repeated trajectories are never treated as independent replicates.
`paired_contrasts` reports within-repetition sizing, checkpoint, and interaction
effects instead of drawing conclusions from overlapping marginal intervals.

After both registered suites finish, generate paper tables directly from their
validated macro statistics and paired contrasts:

```bash
python3 scripts/report-replay-paper.py \
  --main /data/clawbox-paper-suite-001 \
  --density /data/clawbox-paper-checkpoint-density-001 \
  --json-out /data/clawbox-paper-report.json \
  --markdown-out /data/clawbox-paper-report.md
```

The report command fails closed if either suite is incomplete, any block has a
divergent final state, any session failed its correctness command, or a
registered baseline is missing. Its 95% confidence intervals use independent
task IDs; with repeated trajectories from only one SWE task it explicitly
reports that a cross-task interval is not estimable.
The JSON retains all registered telemetry (correct and completed throughput,
step throughput, configured memory, RSS/RSS-time, NUMA and cgroup deltas,
checkpoint counts and service time, admission and session-latency tails, and
snapshot allocation); the Markdown file renders the primary tables and paired
effects without discarding the machine-readable secondary outcomes.

An optional `correctness_command` records a separate exit code and output per
session without conflating a benchmark-oracle failure with infrastructure
failure. When configured, reports include `correctness_pass_fraction` and
`throughput_correct_tasks_per_minute`; otherwise "tasks/min" means completed
agent sessions/min, not correct SWE tasks/min.

For the separate checkpoint-density experiment, use
`deploy/replay-density.example.json`. It holds a 160 GiB configured resident
admission budget constant, uses a 2+2 GiB Runtime/Tool pair, crosses resident
and LLM-wait checkpoint policy, and requests `[40, 60, 76]` agent runs. At 40,
all pairs fit; at 60/76, the resident arm runs waves while the checkpoint arm
can release FIFO admission slots during model waits. One atomic FIFO lease owns
the Runtime+Tool memory slot and a unique NUMA-local CPU pair; it is released
only after both VMs are evicted and reacquired before either restore. Runtime
is paused and checkpointed before Tool so a model response cannot dispatch work
to an unavailable dependency; restore uses the inverse Tool-then-Runtime order.
This is a direct
configured-memory-budget throughput/density test rather than a projection from
RSS. It uses `cpu_placement=round_robin`; label 60/76 CPU-oversubscribed and
report the scheduling regime. The disk preflight accounts for one generation per
evicted session plus the prior generation mapped by each resident slot. Extend
the network first with `--sessions 76 --reuse-existing`, and launch the suite
itself through `numactl` as shown above.

The registered Kunpeng protocol and current paper artifacts are documented in
[`docs/results/p90-baselines-kunpeng-2026-08-31.md`](docs/results/p90-baselines-kunpeng-2026-08-31.md).
The bounded, directly reproducible c8 result uses the tracked
[`two-hour-direct-c08-suite.json`](docs/results/artifacts/kunpeng-2026-08-31/two-hour-direct-c08-suite.json):

```bash
numactl --cpunodebind=0 --membind=0 \
  python3 -m clawbox.replay.cli suite \
  docs/results/artifacts/kunpeng-2026-08-31/two-hour-direct-c08-suite.json \
  --validate-only

numactl --cpunodebind=0 --membind=0 \
  python3 -m clawbox.replay.cli suite \
  docs/results/artifacts/kunpeng-2026-08-31/two-hour-direct-c08-suite.json
```

The prediction-aware admission sweep uses fixed 2 GiB Tool VM capacity for its
P90 arms and gates each tool-bearing model response with a heterogeneous,
per-invocation reservation. The tracked `[1, 8, 20, 40]` NUMA-0 suite is
[`prediction-aware-sweep-suite.json`](docs/results/artifacts/kunpeng-2026-08-31/prediction-aware-sweep-suite.json).
Its 4 GiB and 2 GiB arms are static controls; neither is a predictive arm.

Its output directory is frozen in the configuration. Remove or rename a prior
output only after preserving its evidence; `resume: true` accepts completed
child studies with the same immutable suite identity and rejects partial or
changed inputs.
The earlier implementation-acceptance smoke result remains in
[`docs/results/p90-baselines-kunpeng-2026-08-30.md`](docs/results/p90-baselines-kunpeng-2026-08-30.md)
and is not pooled with the paper suite.

The example validator prints the Git diff plus hashes of non-ignored untracked
files. It excludes transient ignored caches without missing newly created task
files. Replace it with a command that prints the scientifically relevant final
artifact for your workload. Identical stdout produces an identical recorded
hash; the raw output is retained as `validation-session-NNNN.out`. A non-zero
exit code or different hashes make the study fail.

### 6. Read the result and clean up

```bash
python3 -m json.tool /data/paper-study-001/study-summary.json | less
sudo bash scripts/direct-firecracker-network.sh down --sessions 8 --prefix 172.30
```

Do not draw conclusions from a single repetition. Use the same session count
and VM sizes for every group, retain the generated manifest, and report both
peak and average memory. Each group retains VM serial logs, request metadata,
memory samples, and per-session results below its output directory.

For a quick local trace/tool check that does not start Firecracker:

```bash
python3 -m clawbox.replay.cli run trace.jsonl \
  --backend local --mode resident --sleep-scale 0 \
  --cwd /tmp/disposable-checkout
```

### What has been verified

On Kunpeng, all four single-session combinations passed: replay and API
inference, each with `resident` and `llm_wait_checkpoint` residency. Every run
completed two model turns and one real SSH tool call. Both checkpoint runs
saved and restored both VMs during each model turn (two cycles), retained
ClawTune traces, and produced the same final-state hash as resident runs. These
are functional checks; publishable density and performance claims still
require the configured concurrency and repeated trials.

## Terminology and compatibility

- `vm_checkpoint`: Firecracker execution state;
- `kb_snapshot`: ClawTune knowledge state;
- `egress_address_snapshot`: resolved provider addresses;
- `storage_snapshot`: containerd/devmapper state.

Legacy `memory_policies: [resident, snapshot]` and
`--mode resident|snapshot` remain accepted at runner boundaries. New
configuration and result fields use `inference_backend`, `sandbox_backend`,
`admission_policy`, `residency_policy`, and `agent_driver`.

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

The production Kubernetes controller does not yet suspend running Pods. The
trace-replay experiment above can save and restore directly managed
Firecracker VMs, including an optional second VM representing the tool
environment. Integrating this with containerd and Kata without making
Kubernetes treat the Pod as failed remains future work. See
[ADR 004](docs/adr/004-cell-suspend-resume-boundary.md) for the controller
lifecycle boundary.

Development checks:

```bash
python3 -m pytest -q
cd toolbridge && go test -race ./...
```

- Manual manifest deployment: [deploy/README.md](deploy/README.md)
- Execution and telemetry invariants: [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)
- Capacity scale gate: `scripts/scale-swe-rebench.sh`
- Managed API load test: `scripts/load-test.sh`
