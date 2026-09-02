# ClawBox

ClawBox runs coding agent workloads in isolated virtual machines on a
dedicated ARM64 server. Every task uses two Kata/Firecracker VMs:

- the **Runtime VM** runs OpenClaw and ClawTune;
- the **Tool VM** contains the repository and executes commands;
- a Kubernetes controller creates, monitors, collects, and removes both VMs.

The separation keeps model credentials away from repository processes while
allowing the agent to run real commands over SSH. Resource measurements are
collected inside the Tool VM, where the command processes and their Linux
cgroups are visible.

The supported production target is one dedicated openEuler ARM64 host, such as
a Kunpeng server, with KVM, cgroup v2, Kubernetes, Kata Containers, and
Firecracker. There is no x86, QEMU, runc, multi-node, or alternate-VMM
fallback.

## Choose a workflow

| Goal | Workload | Model responses | Execution environment | Entry point |
| --- | --- | --- | --- | --- |
| Run real coding tasks | SWE-ReBench | Real OpenAI-compatible API | Kubernetes-managed Kata/Firecracker | `scripts/run-swe-rebench.sh` |
| Run controlled systems experiments | Recorded agent traces | Recorded responses, real API, or both | Directly managed Firecracker | `python3 -m clawbox.replay.cli study` or `suite` |

Both paths run the real OpenClaw loop, execute commands in a separate VM over
SSH, collect ClawTune telemetry, validate the final repository state, and emit
a versioned result record. Kubernetes is the long-running production control
plane. Direct Firecracker provides deterministic CPU, NUMA, memory, balloon,
and checkpoint controls for experiments.

Common commands:

| Task | Command |
| --- | --- |
| Check an installed host without changing it | `scripts/clawbox doctor` |
| Start an installed host after a reboot | `scripts/clawbox up` |
| Submit one asynchronous task | `scripts/clawbox submit ...` |
| Run a production batch | `bash scripts/run-swe-rebench.sh ...` |
| Download traces and reports | `scripts/clawbox traces TASK_ID` |
| Inspect a recorded trace | `python3 -m clawbox.replay.cli inspect TRACE.jsonl` |
| Run one experiment matrix | `python3 -m clawbox.replay.cli study STUDY.json` |
| Run a concurrency sweep | `python3 -m clawbox.replay.cli suite SUITE.json` |

Commands below run from the repository root on the ARM64 host unless stated
otherwise. Production images must use immutable `IMAGE@sha256:...` references.

## Architecture

The Runtime VM receives model configuration and task-scoped upload credentials,
but not the repository. The Tool VM receives the repository and SSH
service, but never the model API key or central artifact credentials.

Tool Bridge creates a cgroup for each command and records CPU, memory, and
guest-kernel eBPF events. ClawTune records model calls and tool activity. At
completion, records are joined by execution identity, signed, and uploaded.
Only correctly paired, complete, loss-free observations may update the tuning
database.

In production, one `SandboxTask` represents one two-VM sandbox. The
controller reserves capacity for both VMs, creates credentials and network
policy, waits for the agent, collects results, and removes child resources.
The `SandboxTask` remains as an audit record.

Every formal result includes the resolved workload, backend, policy, resource
settings, validation command, completion status, failure category, provenance,
metrics, and artifact references. Detailed Kubernetes JSON, experiment
summaries, VM logs, and JSONL telemetry remain the low-level evidence.

## Baselines and experiment paths

*Baseline* has two uses:

1. a **Kubernetes resource baseline** chooses how a production sandbox is
   sized;
2. a **research policy** changes one memory-management decision in a
   direct-Firecracker experiment.

A baseline never silently changes workload, model provider, image,
concurrency, or timeout.

### Kubernetes resource baselines

| Name | Behavior | Intended use |
| --- | --- | --- |
| `fixed-resident` | Uses a fixed profile and keeps both VMs resident | Production default and sizing control |
| `p90-static` | Sizes workspace CPU and memory from one specified, immutable ClawTune generation | Reproducible prediction study |
| `p90-elastic` | Reads the latest valid generation once while queued, then freezes the decision | Operational prediction study |

Fixed profiles:

| Profile | Runtime VM | Tool VM |
| --- | --- | --- |
| `small` | 1 vCPU, 2 GiB, plus ClawTune | 2 vCPU, 4 GiB |
| `medium` | 2 vCPU, 4 GiB, plus ClawTune | 4 vCPU, 8 GiB |
| `large` | 4 vCPU, 8 GiB, plus ClawTune | 8 vCPU, 16 GiB |

P90 sizing changes only workspace CPU and memory. The Runtime VM remains on the
fixed profile. Predictions receive safety headroom, a minimum viable
allocation, and a maximum equal to the selected profile. Kubernetes VM
checkpointing is not implemented.

### Direct-Firecracker research policies

Research configurations keep workspace capacity fixed at 4096 MiB. They change
host-side admission and reclamation, not RAM visible to the guest.

Memory admission:

| Policy | Meaning |
| --- | --- |
| `static_lifetime` | Reserve declared workspace capacity for the whole session |
| `full_reservation` | Charge calibrated full incremental growth for each active tool call |
| `static` | Charge one fixed incremental allowance for every tool call |
| `p90` | Charge a frozen per-tool P90 from held-out telemetry |
| `oracle` | Charge held-out measured growth; offline upper bound only |

Idle-memory reclamation:

| Policy | Meaning |
| --- | --- |
| `resident` | Keep both VMs running |
| `balloon` | Return guest-cooperative free pages while keeping the VM alive |
| `checkpoint` | Save VM state, stop Firecracker, and restore before use |
| `hybrid` | Balloon first and checkpoint later |

Decision and restore:

| Policy | Meaning |
| --- | --- |
| `eager` | Reclaim as soon as eligible |
| `fixed_delay` | Wait for a fixed idle interval |
| `wait_aware_pressure` | Consider predicted model wait and memory pressure |
| `reactive` | Restore only when the VM is required |
| `proactive` | Restore before expected response delivery |

The supplied configurations isolate one question:

| File | Question | Compared policies |
| --- | --- | --- |
| `deploy/replay-suite.example.json` | How should tool memory be admitted? | Five admission policies, resident VMs |
| `deploy/replay-temporal.example.json` | How should idle memory be reclaimed? | Resident, balloon, checkpoint, hybrid |
| `deploy/replay-decision.example.json` | When should hybrid reclaim and restore run? | Eager, fixed delay, pressure-aware; reactive/proactive |
| `deploy/study.example.json` | Does one workload run end to end? | Smaller single-workload admission matrix |

Production supports SWE-ReBench, OpenClaw, a real model API, Kubernetes, and
SSH. Research supports recorded traces, OpenClaw, replay or a real API, direct
Firecracker, and SSH. Local execution and the older replay engine are
development mechanisms, not comparable baselines. Unsupported combinations
fail validation before a runner starts.

## New host: one-time installation

Do not repeat this section after bootstrap. The devmapper `apply` command
erases the two named disks. Daily operation starts at
[Existing host: start and run tasks](#existing-host-start-and-run-tasks).

### 1. Requirements

- Dedicated openEuler ARM64 host with `/dev/kvm` and cgroup v2.
- Python 3.12+, Git, Docker, and Docker Buildx.
- Two unused whole disks for devmapper data and metadata.
- Administrator-owned ClawBox and ClawTune checkouts.
- An image registry reachable by Docker and Kubernetes.
- OpenAI-compatible endpoint, model, API key, and approved egress network.
- SWE-ReBench data and the pinned SWE-bench harness when building task images.

Never use the system disk, a mounted filesystem, or a partition such as
`/dev/sdb1`. Use dedicated whole devices such as `/dev/sdb`.

### 2. Check out and test

```bash
cd /home/USER
git clone https://github.com/w20chen/ClawTune.git
git clone https://github.com/w20chen/ClawBox.git
git -C ClawTune checkout --detach \
  76eab6fa5c6333f4e80901c030f10cab0e4ce605
cd ClawBox

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev,postgres]'
python3 -m pytest -q
```

Publishing refuses dirty checkouts and checks the ClawTune revision. If
installation fails through a stale localhost SOCKS proxy:

```bash
ss -lnt | grep ':1080 ' || true
unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy
python3 -m pip install -e '.[dev,postgres]'
```

Do not unset a working proxy required by the host.

### 3. Inspect and bootstrap the host

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

After independently confirming both disks may be erased:

```bash
sudo bash scripts/bootstrap-openeuler-arm64.sh apply \
  --devmapper-data-device "$DATA_DISK" \
  --devmapper-meta-device "$META_DISK" \
  --confirm-erase "$DATA_DISK,$META_DISK"

bash scripts/install-ebpf-kata-runtime.sh apply
```

Bootstrap installs pinned Kubernetes, containerd, Kata, Firecracker, Calico,
devmapper, runtime classes, and `deploy/containerd-clawbox.service`. Verify:

```bash
systemctl cat containerd.service | grep 'DM_DISABLE_UDEV=1'
systemctl show containerd.service -p Environment | grep 'DM_DISABLE_UDEV=1'
sudo bash scripts/bootstrap-openeuler-arm64.sh status
bash deploy/check-host.sh \
  --runtime-class kata-fc-arm64 --require-ready-label
bash scripts/arm64-kata-smoke.sh --runtime-class kata-fc-arm64
```

Both `DM_DISABLE_UDEV` checks must match; this prevents stale udev waits
during concurrent VM creation.

### 4. Build and publish platform images

For a registry on the same node:

```bash
docker run -d --restart unless-stopped --name clawbox-registry \
  -p 127.0.0.1:5000:5000 \
  -v clawbox-registry:/var/lib/registry registry:2
```

Skip that command for an existing registry. Build natively:

```bash
export REGISTRY=127.0.0.1:5000/clawbox
export CLAWTUNE_ROOT="$PWD/../ClawTune"
export TAG="$(git rev-parse --short=12 HEAD)"
export PUSH=1
bash scripts/build-kubernetes-images.sh
```

Immutable image references are written to
`.artifacts/platform-images.env` for `install` and `up`.

### 5. Build native ARM64 task images

Prepare:

- `/data/swe-rebench.parquet`: dataset;
- `../ClawTune/swe_rebench/tasks.json`: selected tasks;
- `/src/SWE-bench-fork`: harness checkout;
- `/data/swe-rebench-arm64-map.json`: output mapping.

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

Every selected task requires `platform: linux/arm64` and an immutable
`arm64_image`; no runtime translation is attempted.

### 6. Configure the model service and install

Resolve the provider immediately before installation. Use a provider-approved
narrow CIDR or exact public `/32`; `0.0.0.0/0` is rejected.

```bash
getent ahostsv4 api.example.com | awk '{print $1}' | sort -u
```

Choose one mapped task image for the Managed API's fixed default template:

```bash
read -rsp 'Model API key: ' CLAWBOX_LLM_API_KEY
echo
export CLAWBOX_LLM_API_KEY
export CLAWBOX_LLM_BASE_URL='https://PROVIDER_BASE_URL/v1'
export CLAWBOX_LLM_MODEL='PROVIDER_MODEL_ID'
export CLAWBOX_OPENCLAW_MODEL_REF='vllm/PROVIDER_MODEL_ID'
export CLAWBOX_TOOL_IMAGE='127.0.0.1:5000/clawbox/TASK@sha256:DIGEST'
read -rp 'Approved provider CIDR: ' CLAWBOX_LLM_EGRESS_CIDR
export CLAWBOX_LLM_EGRESS_CIDR

scripts/clawbox install
scripts/clawbox doctor
unset CLAWBOX_LLM_API_KEY
```

`install` stores Secrets, initializes persistent SQLite, applies five
services, runs migrations, and waits for readiness. Set
`CLAWBOX_DATABASE_URL=postgresql+psycopg://...` beforehand for PostgreSQL.

### 7. First concurrency smoke

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
  --llm-egress-host api.example.com \
  --parallelism 2 \
  --timeout-seconds 120 \
  --command-timeout-seconds 120 \
  --run-id "$RUN_ID"
```

The 120 seconds cover the agent, not VM startup and result collection.
Repeated task copies test infrastructure concurrency, not benchmark quality.

## Existing host: start and run tasks

Never repeat the disk bootstrap.

### Start and verify

```bash
cd /home/USER/ClawBox
scripts/clawbox up
scripts/clawbox doctor
```

`up` starts containerd, kubelet, and Docker, reapplies immutable image
configuration, runs migrations, and waits for five deployments. It does not
partition disks.

```bash
kubectl get nodes
kubectl describe node | grep -A2 -E \
  'DiskPressure|MemoryPressure|PIDPressure'
kubectl -n clawbox-benchmarks get events --sort-by=.lastTimestamp \
  | grep -E 'FailedCreatePodSandbox|devmapper' | tail -20 || true
```

### Run a production batch

```bash
export RUN_ID="swe-$(date -u +%Y%m%d%H%M%S)"
bash scripts/run-swe-rebench.sh \
  --tasks /data/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-host api.example.com \
  --profile small \
  --baseline fixed-resident \
  --parallelism 8 \
  --timeout-seconds 1800 \
  --command-timeout-seconds 300 \
  --run-id "$RUN_ID" \
  --output ".artifacts/${RUN_ID}.json"
```

`--parallelism` controls active tasks. VM creation is staggered to reduce
devmapper pressure; a task may wait until both VMs fit.
`--llm-egress-host` resolves current public IPv4 addresses, rejects unsafe
results, and freezes exact `/32` egress rules for the batch. Run the launcher
again for every batch so DNS changes are captured.

For a static prediction generation:

```bash
bash scripts/run-swe-rebench.sh \
  --tasks /data/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-host api.example.com \
  --profile small \
  --baseline p90-static \
  --kb-generation 7 \
  --parallelism 8 \
  --timeout-seconds 1800 \
  --command-timeout-seconds 300 \
  --run-id "p90-static-$(date -u +%Y%m%d%H%M%S)"
```

For latest-at-admission sizing, use `--baseline p90-elastic` and omit
`--kb-generation`. Both modes persist the exact prediction and final
resource decision.

### Run in the background, monitor, or cancel

```bash
nohup bash scripts/run-swe-rebench.sh \
  --tasks /data/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-host api.example.com \
  --profile small --parallelism 8 \
  --timeout-seconds 1800 --command-timeout-seconds 300 \
  --run-id "$RUN_ID" --output ".artifacts/${RUN_ID}.json" \
  >".artifacts/${RUN_ID}.stdout" \
  2>".artifacts/${RUN_ID}.log" </dev/null &
echo $! >".artifacts/${RUN_ID}.pid"
```

```bash
kubectl -n clawbox-benchmarks get sandboxtasks \
  -l "clawbox.openai.com/run=${RUN_ID}" \
  -o custom-columns='NAME:.metadata.name,INSTANCE:.metadata.annotations.clawbox\.openai\.com/original-instance-id,PHASE:.status.phase,OUTCOME:.status.outcome,REASON:.status.reason'
```

Stopping the launcher does not cancel submitted tasks. Cancel a batch:

```bash
while read -r task; do
  kubectl -n clawbox-benchmarks patch "$task" \
    --type=merge -p '{"spec":{"desiredState":"Cancelled"}}'
done < <(kubectl -n clawbox-benchmarks get sandboxtasks \
  -l "clawbox.openai.com/run=${RUN_ID}" -o name)
```

### Submit one asynchronous task

This route uses the fixed workspace image in the installed template. Use the
batch launcher when tasks require different images.

```bash
kubectl -n clawbox-system get secret clawbox-managed \
  -o jsonpath='{.data.templates}' | base64 -d
echo

scripts/clawbox submit \
  --input-ref INSTANCE_ID \
  --problem-file /path/to/problem-statement.txt \
  --deadline-seconds 1800
```

```bash
scripts/clawbox status RUN_ID
scripts/clawbox attempts RUN_ID
scripts/clawbox events RUN_ID
scripts/clawbox watch RUN_ID --timeout 2700
scripts/clawbox cancel RUN_ID
scripts/clawbox retry RUN_ID
```

Ctrl+C stops only the watcher. Use `--idempotency-key` only to repeat the
same uncertain HTTP submission.

### Download task artifacts

```bash
TASK_ID='swe-run-id-instance-hash'
scripts/clawbox traces "$TASK_ID"
```

Output defaults to `.artifacts/${TASK_ID}.traces/` and includes model/tool
JSONL, guest-kernel events, cgroup records, validation output, and reports.

## Validate workflow specifications

This planner never starts a VM:

```bash
python3 -m clawbox.experiments.cli list-baselines
python3 -m clawbox.experiments.cli validate experiment.json
python3 -m clawbox.experiments.cli resolve experiment.json
python3 -m clawbox.experiments.cli matrix experiment.json
```

A workflow explicitly selects workload, agent, inference source, sandbox,
workspace transport, resource policy, concurrency, timeout, validation, and
output. `resolve` requires one workflow; `matrix` expands selected
inference sources and baselines. Execution still uses the production launcher
or the research commands below.

## Direct Firecracker experiments

Each session still uses separate agent and Tool VMs with OpenClaw,
ClawTune, and SSH. The runner is single-host and fail-stop; rerun failed arms
from fresh disks rather than treating it as a highly available service.

### 1. Build reusable VM disks

```bash
python3 scripts/build-oci-firecracker-rootfs.py \
  --image 'REGISTRY/runtime-arm64:TAG' \
  --output /data/openclaw-runtime.ext4 --size-mib 6144 \
  --inject-file \
    scripts/experiment-runtime-init.sh:/usr/local/bin/experiment-runtime-init

python3 scripts/build-oci-firecracker-rootfs.py \
  --image 'REGISTRY/workload-arm64@sha256:DIGEST' \
  --output /data/tool-workspace.ext4 --size-mib 16384 \
  --inject-file \
    scripts/experiment-tool-init.sh:/usr/local/bin/experiment-tool-init
```

The runner clones fresh disks for each group. Never reuse an executed copy as
a source. Copy-on-write filesystem support greatly reduces preparation time.

### 2. Prepare workloads and predictions

Each workload needs a prompt, a recorded ClawTune model trace, an independent
task ID, and validation/correctness commands.

```bash
python3 -m clawbox.replay.cli inspect \
  /data/workloads/short-fix.model.jsonl
```

Traces must include assistant responses, tool-call arguments, and model
latency. New traces also contain request payloads; replay fails if the live
OpenClaw request diverges. The validation command should print the final
repository state, for example:

```bash
cd /testbed && git diff --binary --no-ext-diff HEAD
```

Train P90 inputs from recordings independent of evaluation traces:

```bash
python3 scripts/train-p90-from-runs.py \
  /data/train-recording/session-0000 \
  /data/train-recording/session-0001 \
  --repository OWNER/REPO \
  --observed-repo-fingerprint TRACE_REPO_FIELD \
  --training-set-id recording-train \
  --evaluation-set-id recording-eval \
  --evaluation-trace \
    short-fix=/data/eval/short-fix.model.jsonl \
  --evaluation-trace \
    test-debug=/data/eval/test-debug.model.jsonl \
  --output /data/workloads/p90-independent-eval.json
```

Formal suites should set `require_held_out_predictions: true`. Oracle inputs
use held-out measured incremental memory and are offline upper bounds. For an
initial VM smoke, edit `deploy/study.example.json` to use only
`["full_reservation"]`; this avoids requiring P90 and oracle files.

### 3. Create the memory safety hierarchy

Adjust these example limits to the selected NUMA node:

```bash
echo +memory | sudo tee /sys/fs/cgroup/cgroup.subtree_control
sudo mkdir -p /sys/fs/cgroup/clawbox-vm-pool
echo $((212992 * 1024 * 1024)) | sudo tee \
  /sys/fs/cgroup/clawbox-vm-pool/memory.max
echo +memory | sudo tee \
  /sys/fs/cgroup/clawbox-vm-pool/cgroup.subtree_control
sudo mkdir -p \
  /sys/fs/cgroup/clawbox-vm-pool/runtime \
  /sys/fs/cgroup/clawbox-vm-pool/tool
echo $((131072 * 1024 * 1024)) | sudo tee \
  /sys/fs/cgroup/clawbox-vm-pool/tool/memory.max
```

For both parent and workspace pools:

```text
low watermark < high watermark < hard limit
```

Admission uses cgroup `memory.current`; Firecracker RSS is diagnostic.
`oom_kill` growth is a policy failure. The runner remains outside the VM
cgroup and is covered by `numa_host_reserve_mib`.

### 4. Calibrate checkpoint overhead

Temporal and decision examples intentionally contain zero transient
reservations and must be calibrated on the target kernel, Firecracker, and
filesystem before formal use:

```bash
python3 scripts/calibrate-replay-memory.py \
  /data/pilot-*/results/summary.json \
  --quantile 0.99 \
  --output /data/calibration/replay-memory-p99.json
```

Copy measured values into
`reclamation.checkpoint_transient_parent_mib` and
`reclamation.checkpoint_transient_tool_mib`. Never use guessed zero values.

### 5. Create VM networks

```bash
sudo bash scripts/direct-firecracker-network.sh up \
  --sessions 40 --network 172.30.0.0/16
```

One isolated `/29` is allocated per session. Keep networks up for the whole
study. `--reuse-existing` validates an existing setup before reuse.

### 6. Configure, validate, and run

```bash
cp deploy/study.example.json /data/study.json
cp deploy/replay-suite.example.json /data/spatial.json
cp deploy/replay-temporal.example.json /data/temporal.json
cp deploy/replay-decision.example.json /data/decision.json
\${EDITOR:-vi} /data/study.json
```

Important fields:

| Field | Purpose |
| --- | --- |
| `output` | New output directory; studies do not overwrite existing output |
| `source` / `workloads[]` | VM disks, prompts, traces, repository IDs, prediction files |
| `independent_unit` | Independent task ID for statistics |
| `sessions` / `concurrency_levels` | Offered concurrency |
| `repetitions`, `seed` | Repeated randomized arm order |
| `inference_backends` | `replay`, `api`, or both |
| `paper_experiment` | The one policy dimension and its arms |
| `tool_pool_memory`, `vm_pool_memory` | Hard limits, watermarks, and headroom |
| `resources` | Guest memory, NUMA node, and CPU start |
| `cpu_placement` | `round_robin` offered load or `exclusive` CPU isolation |
| `validation_command`, `correctness_command` | Final-state and correctness checks |
| `resume` | Reuse only complete children with identical input/config hashes |
| `replay.time_scale` | Recorded latency multiplier; use `1.0` for measurements |

For a real model API, add:

```json
{
  "inference_backends": ["replay", "api"],
  "api": {
    "base_url": "https://PROVIDER_BASE_URL/v1",
    "model": "PROVIDER_MODEL_ID",
    "key_env": "OPENAI_API_KEY"
  }
}
```

```bash
read -rsp 'Model API key: ' OPENAI_API_KEY
echo
export OPENAI_API_KEY
python3 -m clawbox.replay.cli study /data/study.json
```

Validate a suite without writing experiment output:

```bash
numactl --cpunodebind=0 --membind=0 \
  python3 -m clawbox.replay.cli suite \
  /data/spatial.json --validate-only
```

Preflight checks files and digests, held-out provenance, NUMA CPU/memory,
node-local free memory and CPU load, snapshot disk space, parent NUMA binding,
running Firecracker processes, and network capacity.

```bash
numactl --cpunodebind=0 --membind=0 \
  python3 -m clawbox.replay.cli suite /data/spatial.json
```

`round_robin` reuses NUMA-local CPU pairs and lets memory admission control
concurrency. `exclusive` requires two distinct CPUs per session. With
`resume: true`, only complete children with the same immutable suite identity
are reused; partial or changed output is rejected.

### 7. Inspect, validate, report, and clean up

```bash
python3 scripts/summarize-replay-suite.py /data/SUITE_OUTPUT

python3 scripts/validate-replay-gates.py \
  /data/ARM/results/summary.json \
  --mode formal \
  --output /data/ARM/results/validity-gates.json
```

Formal gates require final-state equality, no host OOM or swap violation,
correct NUMA placement, prediction evidence, checkpoint I/O/transient evidence
when applicable, and consistent model/tool counts.

Outputs include `study-summary.json`, `result-envelopes.json`,
`suite-summary.json`, `measurements.csv`, VM logs, gateway records, memory
samples, and validation/correctness output.

```bash
python3 scripts/report-replay-paper.py \
  --main /data/MAIN_SUITE \
  --density /data/DENSITY_SUITE \
  --json-out /data/paper-report.json \
  --markdown-out /data/paper-report.md
```

Use independent task IDs—not model calls—as statistical samples. Do not draw
conclusions from one repetition.

After every Firecracker process exits:

```bash
sudo bash scripts/direct-firecracker-network.sh down \
  --sessions 40 --network 172.30.0.0/16
unset OPENAI_API_KEY
```

## Tuning data and offline analysis

Accepted observations are scoped by tenant and repository. Raw manifests are
signed, immutable, and idempotent. Clause-level and tool-resource models are
published as one atomic generation. Later tasks may request an exact
generation or the latest valid generation.

```bash
python3 -m clawbox.tuning \
  /data/run-a /data/run-b /data/run-c \
  --output-dir /data/tuning-analysis
```

This joins ClawTune spans and Tool Bridge records by execution ID, exports
command-disjoint and stratified splits, and compares a fixed profile, a global
estimator, and the repository-aware model.

## Updating an installed host

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

Never copy source into running containers. Rebuild images and deploy immutable
digests.

For an older host missing `DM_DISABLE_UDEV=1`, wait until no task VM runs:

```bash
kubectl -n clawbox-benchmarks get pods,jobs
sudo install -m 0644 deploy/containerd-clawbox.service \
  /etc/systemd/system/containerd.service
sudo systemctl daemon-reload
sudo systemctl restart containerd
sudo systemctl restart kubelet
systemctl show containerd.service -p Environment \
  | grep 'DM_DISABLE_UDEV=1'
scripts/clawbox up
scripts/clawbox doctor
```

Do not restart containerd while task VMs are running.

## Troubleshooting

### Task remains queued

```bash
kubectl -n clawbox-benchmarks get sandboxtask TASK_NAME -o yaml
kubectl -n clawbox-system logs \
  deployment/clawbox-cell-controller --tail=300
```

- `PendingAdmission`: waiting for reconciliation.
- `InsufficientCellCapacity`: both VMs do not fit.
- `DiskPressure=True`: free host disk space.
- P90 tasks also require a valid prediction service, generation, and evidence.

### Sandbox or devmapper creation fails

```bash
kubectl -n clawbox-benchmarks get events --sort-by=.lastTimestamp \
  | grep -E 'FailedCreatePodSandbox|devmapper' | tail -100
sudo bash scripts/setup-devmapper-openeuler-arm64.sh status
systemctl show containerd.service -p Environment
df -h /
sudo du -sh \
  /var/crash /var/lib/containerd /var/lib/kubelet 2>/dev/null
```

Do not run global Docker/containerd prune commands. Remove only exact files
after preserving useful diagnostics.

### Failed Pods accumulated

```bash
python3 scripts/cleanup-failed-pods.py
python3 scripts/cleanup-failed-pods.py \
  --namespace clawbox-system --apply
```

Never clean unrelated namespaces, Pods, Kata sandboxes, or devmapper snapshots.

### Direct-Firecracker preflight fails

Typical causes are changed input digests, insufficient NUMA memory, missing
parent NUMA binding, excessive CPU load, another Firecracker process, low
snapshot disk space, uncalibrated checkpoint reservations, or partial output.
Correct the cause and rerun `--validate-only`; do not bypass formal gates.

## Development and verification

```bash
python3 -m pytest -q
python3 -m compileall -q clawbox scripts

cd toolbridge
go test -race ./...
```

For guest-kernel, eBPF, or production task-image changes, also run on ARM64:

```bash
bash scripts/arm64-kata-smoke.sh --runtime-class kata-fc-arm64

bash scripts/run-toolbridge-ebpf-integration.sh \
  --image 'REGISTRY/TOOL@sha256:DIGEST' \
  --namespace clawbox-ebpf-acceptance \
  --output .artifacts/toolbridge-ebpf-integration.log
```

The exact Kubernetes manifest order, persistence layout, and manual deployment
procedure are in [deploy/README.md](deploy/README.md).
