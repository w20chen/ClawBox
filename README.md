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

Tool placement follows execution semantics rather than the name "tool". The
workspace/process tools (`exec`, `process`, `read`, `write`, `edit`, and
`apply_patch`) execute in Tool over SSH and are individually admitted. OpenClaw
in-process services such as `web_search`, `web_fetch`, `memory_search`, and
`memory_get` remain in Runtime and do not wake or reserve memory for Tool.

CubeSandbox supplies a semantic raw TCP endpoint for Tool port 2222. After a
checkpoint/restore, PolicyControl resolves the replacement endpoint, refreshes
Runtime's pinned known-host entry, and returns the route with `ADMIT`. The SSH
hook rewrites the invocation that requested admission before launching OpenSSH;
it does not depend on a later OpenClaw configuration refresh. Endpoint hosts
must remain stable across restore because Runtime network policy is fixed at VM
creation; mapped ports may change.

During model waits, lightweight ModelGateway events are queued to policy workers.
Policy decides when an idle Tool VM should be swapped out and restored;
CubeSandbox performs create, pause/checkpoint, connect/restore, placement, and
destroy. Gateway locks never contain slow lifecycle work.

ClawBox does not use SandboxTask CRDs, controllers, Jobs/Pods, Services,
NodePorts, Kubernetes scheduling, or direct Firecracker management. Some legacy
modules and deployment files still await deletion after the native SSH live gate;
they are not supported architecture.

Current continuation state and unresolved Kunpeng gates are recorded in
[`docs/HANDOFF-2026-09-05-KUNPENG.md`](docs/HANDOFF-2026-09-05-KUNPENG.md).

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
cp deploy/kunpeng-research.env.example ~/clawbox-kunpeng.env
# Edit the copy, then source it. Do not commit it.
. ~/clawbox-kunpeng.env
bash scripts/install-cubesandbox-kunpeng920.sh check
bash scripts/install-cubesandbox-kunpeng920.sh install
bash scripts/provision-kunpeng-openclaw.sh build
bash scripts/provision-kunpeng-openclaw.sh verify
```

`install` now reproduces the ClawBox CubeAPI semantic-TCP patch, persistent
loopback registry plus guest-only bridge, staged MinIO/S3lvol/node bootstrap,
CoreDNS routing, and patched SDK. `provision` builds both VM images, pins their
registry digests, registers fresh templates, and writes the selected IDs to
`.artifacts/kunpeng-openclaw.env`.

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

Deterministic OpenClaw replay is the primary formal comparison mode: Runtime,
OpenClaw, ClawTune, native SSH, Tool VM, commands, admission, memory pressure,
and telemetry remain real. Only model generation is replaced by a session-local
ModelGateway that waits for the recorded latency and returns the recorded
OpenAI-compatible response. API mode uses the same Runtime/OpenClaw path and
gateway interface but forwards to the configured upstream model. An API run can
export its successful responses as the exact input for a later replay run.

`agent.driver=replay_engine` is a lightweight systems-only trace driver retained
for inexpensive capacity exploration. It does not represent OpenClaw overhead
and must not be used for formal end-to-end agent claims. Use
`examples/experiments/openclaw-cube-replay-c40.yaml` for the formal c40 path
after recording a valid c1 API trace.

Progress through c1, c4/c8 correctness, c20 policy pilot, then c40/c60. Do not
claim an arm unless output validation passes, exact-ID join rate is 1.0,
telemetry loss and duplicate execution are zero, routing is session-correct,
and all owned sandboxes are destroyed. Agent JCT excludes validation, hashing,
cleanup, and stabilization.

Before c1, validate the deployment route and the admission-triggered restore:

```bash
python scripts/validate-cubesandbox-tcp-endpoints.py \
  --runtime-template RUNTIME_TEMPLATE --tool-template TOOL_TEMPLATE \
  --node NODE --control-host HOST_IP --count 4 --output route-gate.json
python scripts/smoke-cubesandbox-agent-pair.py \
  --runtime-template RUNTIME_TEMPLATE --tool-template TOOL_TEMPLATE \
  --node NODE --control-host HOST_IP --output pair-smoke.json
```

The result envelope reports FIFO admission count/depth/wait percentiles,
Runtime-to-PolicyControl round-trip latency, control overhead after subtracting
intentional queue and restore time, lifecycle service times, model lifecycle
times, and exact-ID Tool telemetry.

Measure the metadata path separately from Cube and Tool work before a campaign:

```bash
python scripts/benchmark-policy-control.py --concurrency 40 \
  --requests-per-session 25
```

Set `--max-p95-ms` to the campaign's preregistered host-specific limit. Also
report control overhead as a fraction of Tool latency; do not hide intentional
FIFO waiting or restore service time inside the overhead number.

## Local verification

```bash
python -m pytest -q
docker run --rm -e GOPROXY=https://goproxy.cn,direct \
  -v "$PWD/toolbridge:/src" -w /src golang:1.25-bookworm go test ./...
```

See [docs/research-system-design.md](docs/research-system-design.md) for the
paper-facing component, protocol, and measurement design, and
[docs/HANDOFF.md](docs/HANDOFF.md) for the exact current live boundary.
