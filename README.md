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

For a fresh deployment, prepare the pinned CubeSandbox source and its matching
SDK before building the CubeSandbox API/release bundle:

```bash
export CUBE_SOURCE_DIR="$PWD/.cubesandbox"
bash deploy/cubesandbox/prepare-semantic-source.sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,postgres]'
.venv/bin/python -m pip install -e "$CUBE_SOURCE_DIR/sdk/python"
```

The helper applies the checked-in semantic endpoint patch to CubeSandbox
`v0.7.0`; the public tag does not contain this API. Install the resulting
CubeSandbox deployment using its official one-click or multi-node procedure,
then run `scripts/validate-cubesandbox-tcp-endpoints.py --count 1` before any
experiment. Do not use `Sandbox.get_host(2222)`, a guest IP, NodePort, Redis
metadata, or a ClawBox SSH proxy.

The current single-node Kunpeng diagnostic proves that CubeSandbox returns the
right semantic mapping but its CubeVS datapath does not yet carry same-node VM
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
  examples/experiments/openclaw-cube-replay-c40.yaml --run-id openclaw-replay-c40
```

For real inference, copy `examples/experiments/openclaw-cube.yaml` to a local
machine file, replace its accepted template IDs/digests and node, then export
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

## Local verification

```bash
python -m pytest -q
docker run --rm -e GOPROXY=https://goproxy.cn,direct \
  -v "$PWD/toolbridge:/src" -w /src golang:1.25-bookworm go test ./...
```

See [docs/HANDOFF.md](docs/HANDOFF.md) for the exact current live boundary and
next implementation tasks.
