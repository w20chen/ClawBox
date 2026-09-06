# ClawBox

ClawBox is a research harness for high-density coding-agent execution on
CubeSandbox MicroVMs. CubeSandbox is the sole sandbox and multi-node substrate;
ClawBox supplies Agent-aware memory admission and residency policy above it.

The non-negotiable research architecture, ClawTune ownership boundary, replay
rules, and evidence requirements are summarized in
[docs/research-system-contract.md](docs/research-system-contract.md).

## Architecture

Each Agent owns two CubeSandbox VMs:

```text
host ModelGateway <--- OpenAI-compatible HTTP --- Runtime VM (OpenClaw + ClawTune)
                                                   |
                         synchronous policy hook --+--> host PolicyCoordinator
                                                   |
                                                   +--- native SSH ---> Tool VM
                                                                         |
                                                       workspace + cgroup/eBPF
```

The Tool data path is native SSH. Commands, file content, stdout, and stderr do
not pass through PolicyCoordinator. A Runtime-side SSH hook sends only control
metadata and blocks until admission returns `ADMIT`; after SSH finishes it sends
an idempotent completion. `(session_id, execution_id)` joins prediction,
admission, SSH, Tool cgroup/eBPF telemetry, and completion.
ClawTune supplies the ID/envelope for command-bearing `exec` calls. OpenClaw's
lower-level SSH filesystem/backend calls have no command field at the tool-hook
boundary, so the SSH hook admits only recognized OpenClaw backend invocations,
mints their per-SSH IDs, and inserts the same Tool-bridge envelope. Other
unenveloped SSH is rejected. Backend-maintenance executions are labeled
separately and are excluded from Agent Tool-call throughput.

During model waits, lightweight ModelGateway events are queued to policy workers.
For native OpenClaw with `snapshot_pause`, policy snapshots both the Runtime and
Tool VMs when reclamation is selected. Runtime is restored before the pending
model response is released, while Tool may remain swapped until the next SSH
admission. Resident arms keep both VMs running. CubeSandbox performs create,
pause/checkpoint, connect/restore, placement, and destroy; gateway locks never
contain slow lifecycle work. The replay-engine compatibility path retains its
historical Tool-only lifecycle.

ClawBox does not use SandboxTask CRDs, controllers, Jobs/Pods, Services,
NodePorts, Kubernetes scheduling, or direct Firecracker management. Some legacy
modules and deployment files still await deletion after the native SSH live gate;
they are not supported architecture.

## Set up a machine

The complete operator guide is [docs/cubesandbox-setup.md](docs/cubesandbox-setup.md).
It covers both a fresh CubeSandbox one-click deployment and an already-set-up
machine, including the semantic Tool `2222` endpoint gate. The short rule is:
the Runtime must consume CubeSandbox's raw `host: mappedPort` endpoint and
reach it over the deployment's physical/private network. A populated
CubeSandbox HostPort map, a successful host-side TCP probe, or a successful
CubeProxy HTTP command is not sufficient evidence.

The current Kunpeng c1 managed-replay gate is green on the real architecture:
two exact model steps, one logical Agent `exec`, five admitted native SSH
processes including backend setup, 100% PolicyControl/Runtime/bridge/cgroup/eBPF
execution-ID join, successful workspace validation, and zero leaked sandboxes.
The paired `snapshot_pause` variant is also green: Tool and Runtime were both
checkpointed during the model wait, 607,834,112 bytes of whole-host memory were
observed reclaimed across the pair, Runtime PID 153 survived restore, Runtime
was restored before response release, Tool demand-restore advanced its semantic
endpoint from epoch 1 to epoch 2, and the same exact-ID validation passed. A
Runtime-local relay keeps the OpenClaw-to-ClawTune stream inside the VM snapshot
and reconnects only ClawTune's host-facing request. The gateway recorded two
HTTP attempts for one logical replay step and released the cached result 1.1 ms
after restore, without waiting for OpenClaw's 120-second idle watchdog.
This is an infrastructure smoke trajectory, not a representative paper
workload; formal c20/c40/c60 results still require separately captured held-out
trajectories and a frozen ClawTune-derived KB.

### Latest constrained-memory c60 gate

Commit `70f7b31` is live-green on Kunpeng for a same-spec c60 comparison with a
64 GiB ClawBox policy pool. Sixty Agent pairs offer 360 GiB of configured VM
memory (60 x (2 GiB Runtime + 4 GiB Tool)), so the policy scope is deliberately
overcommitted by 5.625x even though the physical host itself has about 2 TiB.
This distinction matters: the claim is overcommit relative to the experiment's
fixed physical-memory budget, not exhaustion of the entire shared machine.

Both `resident` and eager `snapshot_pause` completed 60/60 Agents, produced one
identical final-output hash, joined native Tool telemetry at 1.0 with zero loss,
had zero host OOMs, and cleaned CubeSandbox inventory to zero. With the same
trace, templates, fixed-stagger arrival schedule, replay timing, and budget,
resident mean/peak host-memory deltas were 48.67/55.77 GB; paired snapshot was
40.70/45.21 GB. Snapshot therefore reduced mean memory by 7.97 GB and peak by
10.57 GB. It performed 178 pauses and 178 restores, taking 322.91 s and 36.22 s
of aggregate service time respectively. Resident was faster on this short
smoke (9.38 versus 6.54 correct Agents/min), while its memory budget caused 229
recorded admission guard interventions and 4,541.89 aggregate blocked seconds;
the snapshot arm needed no such intervention and blocked for 1.60 seconds.

The raw result bundles are
`/home/weitianc/clawbox-results-current/final-network-c60-resident-70f7b-r1`
and `/home/weitianc/clawbox-results-current/final-network-c60-70f7b-r3`.
Their summary SHA-256 values are `49877c403a72794eae46536fd9bb4551f6d55add6163eca46f07c082d60742f1`
and `0af45bdc8b8b1eeaa8c3f569ff0a8c270d0e9dface4781c4a4d471f8ff315222`.
These remain scale/correctness evidence, not the formal paper result: the
workload is one tiny repeated trace and admission is static rather than a
separately trained frozen P90 KB.

The conservative admission implementations were also rechecked on the same
real managed architecture at c5. Both `lifetime_full + resident` and
`tool_full + resident` passed 5/5 with identical output hashes, 1.0 exact
native Tool telemetry joins, zero telemetry loss/OOM, and zero sandbox leaks.
That bundle is
`/home/weitianc/clawbox-results-current/final-network-c5-conservative-70f7b-r1`
(summary SHA-256
`943c59c8cfd5fe6c4caaf738a11c9b7007a41ae0ee62bfebb393ed5d20d5d21e`).

The real-provider confirmation is now green with `deepseek-v4-flash` through
`https://api.deepseek.com/v1`. Managed c1 passed 1/1 with three real model
steps and two Agent Tool operations; managed c2 passed 2/2 with five model
steps and three Tool operations. Both had successful workspace validation,
1.0 exact native Tool telemetry joins, zero telemetry loss, zero host OOM, and
zero post-run sandboxes. The c2 bundle is
`/home/weitianc/clawbox-results-current/final-network-deepseek-c2-3fd64-r3`
(summary SHA-256
`c8f94562927f410e2dc6800c12fb7349c64e16809249384953e34a538c4042d9`).
Credentials were read from the existing ClawTune operator file only in the
Worker environment and are absent from specs and result provenance.

The same managed path is green at c4 and c8 for resident and paired snapshot.
The c8 gate completed 8/8 Agents and 16/16 model steps per arm with 100% exact
Agent Tool telemetry joins, zero OOMs, and zero leaks. Snapshot reduced mean
host-memory delta from 6.34 GB to 4.27 GB in this burst smoke.
`openclaw_exec_yield_ms` is explicit because OpenClaw backgrounds a command
after 10 seconds by default; this smoke uses 120000 ms so incidental concurrent
SSH startup does not alter its frozen trajectory. ClawBox writes the value to
both the Runtime environment and OpenClaw's `tools.exec.backgroundMs` setting;
the latter is required for long-lived/restored Runtime processes at c60.
Representative traces must reuse the value recorded during capture.

The identical-trace burst gate is also live-green at c20, c40, and c60 for
resident and eager paired-snapshot policies. Every final arm completed all
offered Agents with 100% exact Agent Tool telemetry joins, zero telemetry loss,
zero host OOM, zero safety interventions, and zero owned-sandbox leaks. At
c20/c40/c60, resident mean host-memory deltas were 15.81/32.99/50.34 GB and
snapshot deltas were 11.22/24.68/35.87 GB; corresponding resident peaks were
18.95/37.02/55.38 GB and snapshot peaks were 13.51/28.92/41.23 GB. Raw results
are under `/tmp/clawbox-managed-scale-smoke/{live-c20-smoke,live-c40-final-smoke,live-c60-final-smoke}`
on Kunpeng. These runs establish real Runtime+Tool VM scale and paired lifecycle
correctness, not the paper's policy result: they use one tiny trace, burst
arrivals, static Tool admission, and a non-binding memory budget. Formal claims
still require heterogeneous held-out trajectories, a frozen ClawTune KB, every
defined baseline, repeated trials, and the intended memory/NUMA constraint.

For a fresh deployment, prepare the pinned CubeSandbox source and its matching
SDK before building the CubeSandbox API/release bundle:

```bash
export CUBE_SOURCE_DIR="$PWD/.cubesandbox"
bash deploy/cubesandbox/prepare-semantic-source.sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,postgres]'
.venv/bin/python -m pip install -e "$CUBE_SOURCE_DIR/sdk/python"
```

The helper applies the checked-in semantic endpoint and same-node HostPort
hairpin patches to CubeSandbox `v0.7.0`; the public tag contains neither
ClawBox-specific addition. Install the resulting CubeSandbox deployment using
its official one-click or multi-node procedure,
then run `scripts/validate-cubesandbox-tcp-endpoints.py --count 1` before any
experiment. Do not use `Sandbox.get_host(2222)`, a guest IP, NodePort, Redis
metadata, or a ClawBox SSH proxy.

The last unpatched single-node Kunpeng diagnostic proves that CubeSandbox
returns the right semantic mapping but its CubeVS datapath did not carry same-node VM
traffic to it. From a fresh Runtime, all three distinct routes—CubeNode Pod-IP
HostPort, physical-node-IP HostPort, and the Tool's per-VM SandboxIP—were
refused before SSH authentication; the bounded probe then left zero sandboxes.
Reproduce that classification with
`scripts/probe-cubesandbox-network-topology.py`. This script reads CubeMaster
metadata only for diagnostics; the Worker continues to use only the semantic
CubeSandbox endpoint API. The remaining fix belongs in CubeVS networking, not
in a ClawBox proxy or endpoint fallback.

After a Tool pause/restore, ClawBox asks CubeSandbox for the endpoint again.
If the endpoint host changes, the Worker updates the Runtime's CubeSandbox
egress policy through the official SDK before admitting the next SSH call;
mapped-port changes remain per-invocation route data. The Runtime-to-Tool path
is still direct TCP.

## Kunpeng 920 reproducible profile

The following is the source-controlled Kunpeng/Kubernetes profile. It is useful
for checking CubeSandbox lifecycle, storage, kernel, and template setup; its
current single-node Pod-IP HostPort topology is explicitly not accepted as the
final native-SSH topology. Use the standalone guide above for the final gate.

Requirements:

- ARM64 host with KVM and cgroup v2;
- CubeSandbox 0.7.0 control/compute services and its official Python SDK;
- working S3lvol/CubeCoW storage;
- Runtime and Tool images built for ARM64;
- the validated kprobe-enabled guest kernel when native eBPF telemetry is required;
- Python 3.12+ and Docker/BuildKit for image builds.

On the validated Kunpeng deployment, inspect first and avoid changing a healthy
kernel or storage installation:

```bash
bash scripts/install-cubesandbox-kunpeng920.sh check
python -m venv .venv
.venv/bin/pip install -e '.[dev,postgres]'
CUBE_API_URL=http://127.0.0.1:30030 \
  .venv/bin/python scripts/audit-cube-sandboxes.py --json
```

Build immutable pair images. Replace the base-image digests in the Dockerfiles
only when intentionally publishing a new base generation:

```bash
KERNEL_SHA=f84e3fa28ae692f34645aa3c7034999242760eb25aab0ea667b43f16ac12c27f
docker build --network host --build-arg CUBE_GUEST_KERNEL_DIGEST="$KERNEL_SHA" \
  -f docker/Dockerfile.runtime-cube -t REGISTRY/clawbox/runtime-cube-arm64:REV .
docker build --network host --build-arg CUBE_GUEST_KERNEL_DIGEST="$KERNEL_SHA" \
  -f docker/Dockerfile.tool-cube -t REGISTRY/clawbox/tool-cube-arm64:REV .
docker push REGISTRY/clawbox/runtime-cube-arm64:REV
docker push REGISTRY/clawbox/tool-cube-arm64:REV
```

Register fresh templates by digest. Tool templates must expose SSH port 2222;
both images expose envd on 49983 for Cube readiness:

```bash
CUBE_API_URL=http://127.0.0.1:30030 .venv/bin/python \
  scripts/register-cube-template.py RUNTIME_IMAGE@sha256:DIGEST \
  --alias clawbox-runtime-REV --node NODE --memory-mib 2048 \
  --exposed-port 49983 --probe-port 49983

CUBE_API_URL=http://127.0.0.1:30030 .venv/bin/python \
  scripts/register-cube-template.py TOOL_IMAGE@sha256:DIGEST \
  --alias clawbox-tool-REV --node NODE --memory-mib 4096 \
  --exposed-port 49983 --exposed-port 2222 --probe-port 49983
```

Never reuse a failed or pre-kernel template as evidence. Gate 1 must verify
create, native Runtime-to-Tool SSH, pause/checkpoint, physical-memory release,
restore, post-restore SSH/telemetry, destroy, and zero owner leaks.

## Define and run experiments

Experiment YAML uses schema v2. Start from
`examples/experiments/openclaw-cube.yaml`; pin Runtime/Tool template IDs, target
node, memory budget, workload, replay trace, and policy tuples. Available
mechanisms are:

- admission: `lifetime_full`, `tool_full`, `tool_static`, `tool_p90`;
- residency: `resident`, `snapshot_pause`;
- eviction: `eager`, `fixed_delay`, `wait_aware_pressure`;
- restore: `reactive`, `proactive`.

Validate and inspect the randomized arm plan:

```bash
clawbox experiment validate experiment.yaml
clawbox experiment plan experiment.yaml
```

Run the standalone worker directly against CubeSandbox. `CLAWBOX_CONTROL_HOST`
must be an IP reachable from Runtime VMs; ports 18080 and 18081 are direct host
listeners for policy control and ModelGateway respectively:

```bash
export CUBE_API_URL=http://127.0.0.1:30030
# Existing CubeProxy HTTP transport for SDK data-plane requests; this is not
# the native SSH endpoint and does not allocate or proxy Tool port 2222.
export CUBE_PROXY_NODE_IP=HOST_IP_REACHABLE_FROM_WORKER
export CUBE_PROXY_PORT_HTTP=30080
export CLAWBOX_CONTROL_HOST=HOST_IP_REACHABLE_FROM_CUBE
export CLAWBOX_MODEL_GATEWAY_HOST="$CLAWBOX_CONTROL_HOST"
export OPENCLAW_API_KEY='...'       # API-recording runs only

clawbox --output-root /data/clawbox-results experiment run experiment.yaml \
  --run-id run-name
clawbox --output-root /data/clawbox-results experiment status run-name
clawbox --output-root /data/clawbox-results experiment collect run-name
```

At high session concurrency the Worker keeps session execution concurrent but
limits simultaneous Runtime/Tool pair creation to eight pairs by default, so
Cubelet/containerd are not stampeded. Set
`CLAWBOX_SANDBOX_CREATE_CONCURRENCY` to a positive value to tune that existing
control-plane throttle; it does not allocate ports or proxy SSH.

Deterministic replay is the primary comparison mode: Runtime, OpenClaw, SSH,
Tool VM, commands, memory pressure, and telemetry remain real; only model
generation is replayed. Replay cursors and response state are per session.

Formal heterogeneous workloads can set `workload.session_assignment` to
`round_robin` and provide two or more cases, each with its own frozen replay
trace. Session index deterministically selects A/B/C/A/... in every policy arm.
`execution.arrival_schedule` explicitly labels a simultaneous `burst`, or can
use `fixed_stagger` with `stagger_interval_seconds`; the assignment, offered
start offsets, trace hashes, and seed are retained in result provenance.

Run the native replay arm after the endpoint gates:

```bash
clawbox --output-root /data/clawbox-results experiment run \
  examples/experiments/openclaw-cube-replay-c60-overcommit.yaml \
  --run-id openclaw-replay-c60-overcommit
```

That checked-in smoke spec offers 360 GiB of VM allocations against a 64 GiB
policy pool and compares resident with paired snapshot under identical inputs.
Verify its pinned template IDs/digests and target node whenever the deployment
is rebuilt; provenance validation intentionally rejects stale templates.

For real inference, copy `examples/experiments/openclaw-cube.yaml` to a local
machine file, verify its accepted template IDs/digests and node, then export
the provider credential named by `inference.configuration.api_key_env`
(`OPENCLAW_API_KEY` in the example) only in the Worker environment. The
configured OpenAI-compatible `base_url` is called by the Worker-side managed
gateway; the Runtime sees only a session token. Never commit the key or put it
in the experiment YAML.

Progress through c1, c4/c8 correctness, c20 policy pilot, then c40/c60. Do not
claim an arm unless output validation passes, exact-ID join rate is 1.0,
telemetry loss and duplicate execution are zero, routing is session-correct,
and all owned sandboxes are destroyed. Agent JCT excludes validation, hashing,
cleanup, and stabilization.

Formal result bundles keep guest and host memory semantics separate. Native
Tool observation rows join policy, Runtime, bridge, cgroup-v2, and eBPF records
by `(session_id, execution_id)` and report prediction/reservation/actual peak
RSS and execution timing. CubeSandbox lifecycle rows use node-wide
`MemAvailable` observations to report physical-memory change around
checkpoint/restore; those values are not Tool-process RSS. The frozen KB hash,
prediction fallback/error summaries, evidence class, configured trace hash,
and memory-safety interventions are retained with each arm.

Recording runs also sample the host RSS of the specific Cube microVM shim over
each admitted SSH execution. Build a formal `tool_p90` artifact only from a
separate recording set: `scripts/train-p90-from-runs.py` consumes ClawTune's
guest cgroup/eBPF observations plus `--host-calibration-observations` result
JSON files. It records the empirical P90 host/guest increment ratio and emits
both the guest command P90 and the calibrated host execution increment used by
admission. An uncalibrated guest-memory-only artifact fails closed. Freeze and
hash that output, then reuse the exact file in every compared arm; held-out
observations remain a shadow/reporting stream and never mutate it.

## Local verification

```bash
python -m pytest -q
docker run --rm -e GOPROXY=https://goproxy.cn,direct \
  -v "$PWD/toolbridge:/src" -w /src golang:1.25-bookworm go test ./...
```

See [docs/HANDOFF.md](docs/HANDOFF.md) for the exact current live boundary and
next implementation tasks.
