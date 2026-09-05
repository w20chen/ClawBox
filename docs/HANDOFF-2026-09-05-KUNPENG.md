# ClawBox Kunpeng continuation checkpoint — 2026-09-05

This is a stop-point handoff, not a completion claim. It records the exact
source state, live-host state, evidence, failed hypotheses, and remaining gates
after the Kunpeng reboot. Read this document before making another host change.

## User's expected final outcome and required working order

The user wants a neat, technically correct research-paper system, not a
production security/privacy project. Do not spend the remaining effort on
hardening unless correctness requires it. Finish in this observable order:

1. write and test the ClawBox source design;
2. inspect Kunpeng and record prerequisites without relying on shell history;
3. write idempotent setup/provisioning code for every required host change;
4. apply that checked-in setup to Kunpeng;
5. build immutable images/templates and run correctness/scaling experiments;
6. update the README/runbook so the user can reproduce everything without a
   code agent.

The required result has these non-negotiable semantics:

- OpenClaw, its plugin, and ClawTune run in the Runtime VM. Workspace/process
  tools normally run in the Tool VM. In-process web search/fetch and agent
  memory/index tools remain Runtime-local.
- Baselines and ClawBox policies must correctly support Tool checkpoint/pause
  and restore, including route invalidation after restore.
- Every Tool-VM operation crosses a low-overhead, per-tool admission decision;
  policy may admit now or later. FIFO/idempotent queue behavior must be real and
  measured, and PolicyControl must not proxy the command data plane.
- Logs must accurately retain create, destroy, checkpoint, restore, endpoint
  discovery/update, admission queue/service/round-trip, model wait, Tool work,
  validation, telemetry, and cleanup spans with exact execution IDs.
- Formal c40 uses replayed model traces to control cost, but real API recording
  and replay simulation remain independent working modes behind the same
  OpenAI/OpenClaw contract. Both must pass c1 before c40 is accepted.
- A fresh operator must be able to reproduce the Kunpeng deployment from the
  repository plus explicit secrets/configuration. Machine-only fixes are not a
  finished artifact.

## How far the project is from the paper-system goal

Two different completion numbers matter:

- **System implementation: about 80%.** The two-VM execution path, FIFO
  admission, restore-time rerouting, replay/API model gateway, and detailed
  lifecycle/admission logging are implemented in the working tree and covered
  by tests.
- **Live research-artifact acceptance: about 50%.** Unit/integration tests and
  ARM64 Toolbridge tests pass, but the native Runtime-to-Tool route has not yet
  passed c1. Consequently the real API/replay equivalence gate and c40 formal
  experiment have not been run on the final path.

The remaining work is five ordered gates. Gate 1 is the only unresolved design
problem; gates 2–5 are validation and experiment execution after it passes.

1. Make a CubeSandbox-owned TCP route from Runtime VM to Tool VM work before
   and after Tool pause/restore.
2. Rebuild the Runtime image containing the admission-time SSH reroute hook,
   register fresh immutable templates, and pass the complete c1 pair smoke.
3. Run one real OpenClaw API case, export its model trace, replay the same case,
   and validate all six Tool-VM workspace tools plus Runtime-local web/memory.
4. Pass c4/c8 isolation, head-of-line, restore, telemetry-join, and leak gates;
   then run c20 and formal OpenClaw+replay c40/c60 arms.
5. Freeze evidence/configuration for the paper and remove unsupported legacy
   Kubernetes/WorkerBridge/direct-Firecracker execution paths.

Do not start c40 before gates 1–3 pass. A c40 run on the lightweight replay
engine is useful baseline evidence but is not the requested formal
OpenClaw+replay result.

## Fixed architecture

One logical agent is exactly two CubeSandbox VMs:

- Runtime VM: OpenClaw, ClawTune plugin/sidecar, model client/gateway contract,
  web search/fetch, and agent-memory lookup.
- Tool VM: mutable workspace and `exec`, `process`, `read`, `write`, `edit`, and
  `apply_patch` through native SSH.
- PolicyControl: host-side, synchronous, metadata-only FIFO admission. It may
  admit immediately or after queue/policy/restore delay. It never proxies
  command text, files, stdout, or stderr.
- ModelGateway: one OpenAI-compatible Runtime path. API mode forwards and
  records; replay mode sleeps for recorded model latency and returns the same
  response contract.
- CubeSandbox: owns create, pause/checkpoint, connect/restore, placement,
  endpoint publication, and destroy.

## Source work completed after `50db717`

The working tree is intentionally uncommitted. Important changes are:

- `scripts/clawbox-policy-ssh.py`: an admission response may return a new SSH
  target; the already-pending SSH invocation is rewritten before OpenSSH starts.
- `clawbox/experiments/worker.py`: restore resolves the endpoint again,
  atomically updates known-host state, publishes the route, and records route,
  lifecycle, admission-round-trip, and estimated control-overhead spans.
- `clawbox/experiments/openclaw_driver.py`: Tool-VM and Runtime-local tool sets
  are explicit. Both API and replay use formal OpenClaw; Tool admission applies
  only to Tool-VM operations.
- `clawbox/experiments/policy.py` and `policy_control.py`: FIFO ordering,
  idempotence, queue depth/wait, service-time, and distribution metrics.
- `clawbox/cube/lifecycle.py`: successful and failed create/checkpoint/restore/
  destroy attempts have wall and monotonic timings plus status/error type.
- `scripts/smoke-cubesandbox-agent-pair.py`: deliberately submits an operation
  containing a stale port; admission must restore and reroute that invocation.
- `examples/experiments/openclaw-cube-replay-c40.yaml`: formal c40
  OpenClaw+replay configuration, separate from the lightweight replay engine.
- `docs/research-system-design.md`: paper-oriented system design and metrics.

Reproducibility work added in the same tree:

- `deploy/cubesandbox/semantic-tcp-endpoint-v0.7.0.patch`: exact CubeAPI and SDK
  delta previously present only in `/home/weitianc/CubeSandbox` and a custom
  image.
- `scripts/install-cubesandbox-kunpeng920.sh`: private pinned source cache,
  deterministic patch application, custom CubeAPI build, persistent local
  registry, boot-persistent guest registry bridge, staged control-plane →
  S3lvol → cube-node installation, DNS configuration, and health status.
- `deploy/kunpeng-research.env.example`: complete non-secret operator input
  template.
- `scripts/provision-kunpeng-openclaw.sh`: builds Runtime/Tool images, pushes
  them, resolves immutable digests, creates fresh templates, writes
  `.artifacts/kunpeng-openclaw.env`, and runs route/pair/leak verification.
- Runtime and Tool Cube Dockerfiles accept explicit base-image build arguments.

## Evidence already obtained

- Local full Python suite passed with five platform-only skips before the
  reproducibility additions. The focused changed-path suite then passed 24/24.
- Remote full Python suite passed when
  `CLAWTUNE_SIDECAR_SRC=/home/weitianc/ClawTune/services/sidecar/src` was set.
- ARM64 Toolbridge Go suite passed in `golang:1.25-bookworm` with Go 1.25.
- Both experiment YAMLs validate.
- c40 PolicyControl loopback benchmark, 1,000 admit+complete pairs:
  mean 45.68 ms, p50 45.01 ms, p95 49.64 ms, max 56.15 ms. This includes two
  Python HTTP round trips and is not Tool execution latency.
- The vendored CubeSandbox patch passes reverse-apply validation against live
  commit `64102d9` and applies to the pinned upstream `d008164` source cache.
- Linux `bash -n` passes for both new/updated provisioning scripts.
- CubeSandbox template capacity is healthy after reboot and sandbox inventory
  was empty at this checkpoint.

Run the complete suite again before commit because the latest setup-script and
documentation changes were added after the earlier full run.

## Current Kunpeng state

Host: `weitianc@193.124.7.2`

- `/home/weitianc/ClawBox` is an older, dirty user checkout. It was not changed
  or cleaned.
- `/tmp/clawbox-codex-20260905` is an isolated worktree at `50db717` containing
  a copy of the current ClawBox changes for remote tests.
- `/home/weitianc/.cache/clawbox/CubeSandbox-v0.7.0` is the new reproducible
  pinned source cache; the semantic endpoint and deterministic configuration
  patches have been applied there.
- **Re-check Helm before doing anything else.** The last known stable release
  was revision 4. The host-network test created revision 5; setting it back to
  pod networking created revision 6, whose cube-node entered CrashLoopBackOff.
  `helm rollback cube 4 -n cube-system --wait --timeout 15m` was then started,
  but the turn was interrupted while Helm was still waiting and its final
  status was not captured. A subsequent read-only SSH status command also
  timed out without output. Do not assume the rollback either succeeded or
  failed.
- `cube-sandbox-s3lvol.service` is active and enabled; its socket is persistent.
- Docker registry `clawbox-registry` is active on `127.0.0.1:5000` with restart
  policy `always`.
- A temporary user-owned `socat` process currently exposes the registry on
  `172.17.0.1:5001`. The checked-in installer replaces this with
  `clawbox-registry-mirror.service`, but installing that unit requires the
  operator's interactive sudo authentication. `prepare` stopped at that sudo
  prompt; it did not build the new CubeAPI image.
- The accepted Runtime and Tool template IDs remain
  `tpl-39efe4ad90384a1fbea3caff` and `tpl-b5cb6f5ee26a41448000b9c2` until fresh
  images/templates are built.

First determine which Helm revision is active and require cube-node `3/3`:

```bash
kubectl -n cube-system get pod -l app.kubernetes.io/component=cube-node -o wide
curl --noproxy '*' -fsS http://127.0.0.1:30030/v2/sandboxes
```

Use these additional read-only checks because a Helm rollback may still have
completed after the interruption:

```bash
helm -n cube-system history cube
helm -n cube-system status cube
kubectl -n cube-system get pods -o wide
kubectl -n cube-system logs -l app.kubernetes.io/component=cube-node \
  -c cubelet --tail=120 --prefix=true
```

Before revision-4 rollback was started, the replacement pod was 2/3 and
Cubelet repeatedly died during initialization. One pod-network attempt logged
`gateway mac for eth0 via 169.254.1.1 not found`; a later attempt was killed
after loading configuration without printing that error. Sandbox inventory was
still `[]`. Do not create a sandbox until cube-node is 3/3 and CubeOps reports
the node ready.

The user also clarified that Kunpeng normally reaches foreign sites through an
SSH/VPN tunnel on their laptop, and that laptop is currently unavailable. Do
not delete or rewrite that tunnel setup. Make provisioning adaptive with
explicit `auto`, China-mirror, and offline/cache-only modes. Normal restart and
experiment execution must use pinned local images/artifacts and must not depend
on the laptop. An operation that genuinely needs an uncached foreign artifact
should fail early and say what must be supplied; it must not mutate a healthy
deployment first. This network-mode enhancement is requested but **not yet
implemented** in the scripts.

## Unresolved blocker: semantic TCP route

With cube-node in its normal pod network, CubeAPI returned an endpoint such as
`192.168.3.157:20006`. Tool setup succeeded and Toolbridge was listening in the
guest, but Runtime SSH received connection refused. This is below ClawBox's
admission layer: it is a CubeVS endpoint/reachability issue.

Observed topology:

- cube-node pod IP was `192.168.3.157`;
- CubeVS allocated ports in `20000–29999` and published the mapping through
  CubeMaster Redis metadata;
- CubeAPI returned cube-node's reported host IP plus the mapped port;
- host-port DNAT is implemented by CubeVS BPF ingress rather than a userspace
  listening socket.

A `cubeNode.hostNetwork=true` experiment made cube-node advertise
`193.124.7.2`, but Cubelet was killed/restarted and remained 2/3 with port 9999
unavailable. That hypothesis was rejected, Helm was rolled back to
`hostNetwork=false`, and the source profile does not contain that change.

This Kubernetes `hostNetwork` flag is unrelated to the user's laptop VPN/SSH
tunnel. The experiment and rollback did not intentionally alter that tunnel.

The next investigation should keep CubeSandbox as route owner. Viable designs
to test, in order:

1. Determine the intended CubeVS hairpin address/path for traffic originating
   in another VM managed by the same cube-node network namespace.
2. If CubeVS has no hairpin path, add a small deployment-owned L4 relay or a
   direct same-node sandbox endpoint to CubeSandbox. ClawBox may resolve and
   refresh it, but PolicyControl must not become a data-plane proxy.
3. Validate create → initial identity → pause/stale failure → restore/new route
   → same-invocation admission reroute → identity, at c1 and c4.

Do not hide the issue by returning an endpoint that is reachable only from the
host process. The acceptance client is the Runtime VM.

## Exact resume procedure

### 0. Recover and freeze the current host state

Run the read-only Helm/pod/inventory checks above. If revision 4 completed and
cube-node is 3/3, make no further recovery change. If the revision is stable
but cube-node is not 3/3, diagnose CubeVS/Calico pod gateway state and any
leftover BPF/network state from revision 5; do not keep cycling Helm blindly.
Preserve `/data/cubelet`, `/data/cubesandbox`, S3lvol, registry data, kernels,
templates, and results.

### 1. Finish and validate reproducible setup

From the ClawBox checkout intended for publication:

```bash
cp deploy/kunpeng-research.env.example ~/clawbox-kunpeng.env
# Fill secrets and CLAWBOX_CONTROL_HOST, then:
. ~/clawbox-kunpeng.env
bash scripts/install-cubesandbox-kunpeng920.sh check
bash scripts/install-cubesandbox-kunpeng920.sh prepare
```

The operator must authenticate sudo once so the persistent registry mirror can
be installed. Do not run full `install` against the existing release until the
rendered Helm diff has been reviewed; `prepare` is sufficient to build the
custom CubeAPI and patched SDK.

Before applying it, add the requested network mode to both setup scripts and
tests. Offline mode must skip `git fetch`, Docker pulls, and package downloads
when the exact pinned cache exists, and give a preflight error otherwise. China
mode should select the already-supported Cube image mirror and configurable
Go/npm/pip/apt mirrors. `auto` may probe first and choose without changing the
cluster.

The latest full local Python run passed 100% with five platform skips after the
handoff/setup additions. Still run `bash -n`, the Cube patch apply check, and
the full suite on Kunpeng after network-mode edits.

### 2. Solve and gate the native Tool route

After the endpoint blocker is fixed and c1 endpoint validation passes:

```bash
bash scripts/provision-kunpeng-openclaw.sh build
bash scripts/provision-kunpeng-openclaw.sh verify
```

Do not accept host-only `curl`/SSH as route proof. The identity probe must
originate inside the Runtime VM. The post-pause operation must have been built
with the stale route and then be rewritten by the admission response before a
single OpenSSH execution.

### 3. Run API/replay and scaling evidence

Then run API c1 from `examples/experiments/openclaw-cube.yaml`, retain its
trace, point `openclaw-cube-replay-c40.yaml` at that trace, and progress through
c4/c8/c20 before c40/c60. Every gate must retain its JSONL timing bundle and
finish with zero owned sandbox leaks.

## Working-tree and validation cautions

- Local repository root at handoff:
  `C:\Users\user\Desktop\ClawBox`, base `50db717`.
- All implementation/setup/handoff changes described here are uncommitted.
  Run `git status --short` and preserve all of them.
- The original remote checkout `/home/weitianc/ClawBox` has pre-existing user
  changes. Never reset, clean, or overwrite it.
- Use `/tmp/clawbox-codex-20260905` or create a new isolated worktree from the
  intended commit. Files copied there may lag the local working tree; compare
  before testing.
- The prepared CubeSandbox cache is
  `/home/weitianc/.cache/clawbox/CubeSandbox-v0.7.0`. The original
  `/home/weitianc/CubeSandbox` is also dirty and must not be reset.
- `scripts/install-cubesandbox-kunpeng920.sh prepare` successfully cloned and
  patched the cache, then stopped at the interactive sudo prompt while trying
  to install `clawbox-registry-mirror.service`. The new CubeAPI was therefore
  not built by the reproducible path yet.
- A user-owned temporary `socat` bridge was started on `172.17.0.1:5001` so the
  current session could use the loopback registry. Replace it with the checked-
  in service after sudo authentication; avoid starting a duplicate listener.

## Completion criteria

The project is complete only when all of the following are retained as
artifacts: correct Runtime/Tool placement; per-tool FIFO admission; successful
pause/restore rerouting without duplicate execution; complete exact-ID logging;
real API and replay equivalence; c40 formal replay results; join rate 1.0;
telemetry loss, wrong routing, replay divergence, duplicate execution, and
sandbox leaks all equal to zero; and a fresh operator can reproduce the host,
images, templates, and gates using only checked-in instructions plus secrets.
