# ClawBox

ClawBox is a research harness for high-density coding-agent execution on
CubeSandbox MicroVMs. CubeSandbox is the sole sandbox and multi-node substrate;
ClawBox supplies Agent-aware memory admission and residency policy above it.

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

During model waits, lightweight ModelGateway events are queued to policy workers.
Policy decides when an idle Tool VM should be swapped out and restored;
CubeSandbox performs create, pause/checkpoint, connect/restore, placement, and
destroy. Gateway locks never contain slow lifecycle work.

ClawBox does not use SandboxTask CRDs, controllers, Jobs/Pods, Services,
NodePorts, Kubernetes scheduling, or direct Firecracker management. Some legacy
modules and deployment files still await deletion after the native SSH live gate;
they are not supported architecture.

## Set up a research host

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
export CLAWBOX_CONTROL_HOST=HOST_IP_REACHABLE_FROM_CUBE
export CLAWBOX_MODEL_GATEWAY_HOST="$CLAWBOX_CONTROL_HOST"
export OPENCLAW_API_KEY='...'       # API-recording runs only

clawbox --output-root /data/clawbox-results experiment run experiment.yaml \
  --run-id run-name
clawbox --output-root /data/clawbox-results experiment status run-name
clawbox --output-root /data/clawbox-results experiment collect run-name
```

Deterministic replay is the primary comparison mode: Runtime, OpenClaw, SSH,
Tool VM, commands, memory pressure, and telemetry remain real; only model
generation is replayed. Replay cursors and response state are per session.

Progress through c1, c4/c8 correctness, c20 policy pilot, then c40/c60. Do not
claim an arm unless output validation passes, exact-ID join rate is 1.0,
telemetry loss and duplicate execution are zero, routing is session-correct,
and all owned sandboxes are destroyed. Agent JCT excludes validation, hashing,
cleanup, and stabilization.

## Local verification

```bash
python -m pytest -q
docker run --rm -e GOPROXY=https://goproxy.cn,direct \
  -v "$PWD/toolbridge:/src" -w /src golang:1.25-bookworm go test ./...
```

See [docs/HANDOFF.md](docs/HANDOFF.md) for the exact current live boundary and
next implementation tasks.
