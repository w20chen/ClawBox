# ClawBox continuation handoff

Updated 2026-09-05 after the native-SSH architecture cutover, c40 replay
matrices, and managed-gateway validation.

## Fixed direction

CubeSandbox is the only sandbox/multi-node substrate. One Agent is one Runtime
CubeSandbox plus one Tool CubeSandbox. OpenClaw uses its native SSH sandbox for
`exec`, `process`, `read`, `write`, `edit`, and `apply_patch`; the mutable
workspace is in Tool. PolicyCoordinator synchronously gates execution but never
proxies commands or output. ModelGateway emits request-start, generated,
released, and delivered events that drive Tool residency.

Kubernetes/SandboxTask/WorkerBridge/cube_shell/direct-Firecracker paths are not
supported. Legacy files still present in the tree are deletion work, not an
alternate architecture.

## Commits and verification

- `cea2161` — synchronous, concurrent, idempotent native-SSH policy protocol;
- `49ab5a4` — managed runner cut over to native SSH, standalone CLI, explicit
  lifecycle state/timing, model lifecycle events, `cube_shell` removed;
- `de74a26` — live-discovered fix allowing Cube to build Tool templates without
  per-session ephemeral SSH keys.

Those historical commits are on `origin/main`. The local full suite now passes,
including the continuation tests. Kunpeng targeted Python tests passed.
Toolbridge Go tests passed on ARM64 in `golang:1.25-bookworm` using
`GOPROXY=https://goproxy.cn,direct`.

The continuation commits `a4be792`, `a30292a`, and `493206d` add structured
timing spans, bounded Cube command streams, and managed API-path coverage.
They are local commits and are not yet pushed to `origin/main`.

The Runtime-side `/usr/local/bin/ssh` shim:

1. reads the ClawTune `__CBX_EXEC_1__` envelope;
2. sends session ID, execution ID, operation, command SHA-256, and prediction
   metadata to `/v1/tool/admit`;
3. blocks until `ADMIT` (including restore/admission wait);
4. launches `/usr/bin/ssh` exactly once with inherited stdio;
5. sends an idempotent completion; it never retries the command.

PolicyControl has per-session `ACTIVE -> DRAINING -> CLOSED` state, exact
execution-ID idempotency, a threaded backlog of 256, and no lock held while an
admission callback blocks. Its c60 unit HOL test proves 59 short independent
sessions finish admission before one slow session.

## Live Kunpeng boundary

Host: `weitianc@193.124.7.2`

Cube services were healthy; `cube-node` was 3/3 Running. Existing kernel,
S3lvol, templates, and results were not modified or deleted. Repository is at
`de74a26` with pre-existing untracked `results/` and `uv.lock` preserved.

Published images:

```text
Runtime (source 49ab5a4)
127.0.0.1:5000/clawbox/runtime-cube-arm64@sha256:05cb920d0c79ee381263f2d57663a8c068d889394b6a9ad942af34810931422f

Tool (source de74a26)
127.0.0.1:5000/clawbox/tool-cube-arm64@sha256:b175fea75b4cd43d5116d93e0c2fc1b745a68d24d84446d5af0b3c72d42a8781
```

Fresh accepted Tool template:

```text
template_id: tpl-b5cb6f5ee26a41448000b9c2
alias: clawbox-tool-native-ssh-de74a26
ports: 49983, 2222
kernel: sha256-f84e3fa28ae6
image: http://172.17.0.1:5001/clawbox/tool-cube-arm64@sha256:b175fea...
```

The public port-5001 registry mirror was absent after reboot. Do not expose it
again. A user-owned `socat` process is currently bound only to private Docker
bridge address `172.17.0.1:5001` and forwards to the loopback registry at
127.0.0.1:5000; Cube API pods successfully fetched `/v2/` through it. PID was
written to `/tmp/clawbox-registry-mirror.pid`; it is not reboot-persistent.

Two failed template records are expected from discovery and must not be reused:

- initial image URL used unavailable public `193.124.7.2:5001`;
- `tpl-56851ed069e44912a13b5904` exposed the template-build/session-key bug fixed
  by `de74a26`.

## Current continuation result

The replay worker was run at concurrency 40 for all prepared baseline suites:
decision (12 arms), full-system (6), reclamation (6), smoke-matrix (2), spatial
(15), and vertical-slice (1). All arms passed after retrying isolated transient
Cube API transport failures. The decision run also reproduced and then cleared
a broken command-stream hang with the bounded client deadline. Each run was
checked for zero remaining Cube sandboxes. Fresh-source runs after `a4be792`
also emit `session_timing` events containing `session`, `agent`, per-sandbox
create, validation, hashing, and cleanup spans.

The managed gateway has both replay and API implementations. Replay is covered
by session-local cursor, retry, delivery, and HOL tests. API mode is covered by
an upstream-compatible integration test that verifies model forwarding and
server-side credential handling. A real upstream model request was not sent by
automation: the operator credential is sensitive, and the live native SSH route
is still unavailable.

The remaining live blocker is concrete. On kunpeng, `get_host(2222)` returns an
HTTP ingress authority; the Tool VM's per-sandbox raw mapping is reachable from
the CubeProxy pod but not from the host node or Runtime VM. Guest `hostname -I`
values are isolated link addresses and must not be used as a native target.
Supply a deployment-owned raw TCP endpoint, then run the pair smoke and managed
OpenClaw c1 before claiming the native path or scaling it.

## Highest-priority next steps

1. Provide a deployment-owned raw TCP endpoint for each Tool sandbox and set
   `CLAWBOX_NATIVE_SSH_TARGET` (or `CLAWBOX_NATIVE_SSH_HOST` plus port). Do not
   derive it from guest `hostname -I` or `get_host(2222)`.
2. Run the Cube-only pair smoke with Runtime template
   `tpl-39efe4ad90384a1fbea3caff` and Tool template
   `tpl-b5cb6f5ee26a41448000b9c2`, then verify admitted SSH, Tool pause, demand
   restore, exact execution count, and no policy command/output leakage.
3. Run managed OpenClaw c1 in replay and API modes with the operator-provided
   credential, export the API response trace, and require replay equivalence
   before any native OpenClaw scale claim.
4. Join Runtime ClawTune spans, PolicyControl records, Tool bridge JSONL,
   cgroup artifacts, and eBPF artifacts by `(session_id, execution_id)` and
   retain the new `session_timing` events in the result bundle.
5. Validate every native file tool, not merely `exec`. Confirm ClawTune applies
   its execution envelope to SSH commands produced by
   `process/read/write/edit/apply_patch`; unenveloped Agent operations must fail
   closed while setup/validation remain explicit non-agent phases.
6. Add the focused admission-versus-delayed-pause race test, then run native
   OpenClaw c4/c8 HOL/cross-routing and leak tests.
7. Only after those native gates pass, run c20 and finally c40/c60 native
   OpenClaw policy arms. The replay c40 matrix is already green.

## Easier cleanup left intentionally

Once native c1 is green, delete the unsupported Kubernetes and HTTP-execution
surface: `clawbox/cell`, managed API/dispatcher, Kubernetes controller/backend,
old allocator/scheduler/tool-agent/node-agent services, SandboxTask manifests,
NodePort/RBAC deployment files, WorkerBridge stress/pair scripts, and their
tests. Rewrite or remove Kubernetes-era examples and status docs. Do not delete
CubeSandbox installation/kernel recovery assets merely because CubeSandbox's
own deployment happens to use Kubernetes internally; ClawBox itself must not
call that layer.

## Experimental acceptance

For every formal arm retain separate create, Agent, model wait, Tool, checkpoint,
restore, validation, hashing, destroy, and stabilization boundaries. Required
correctness is exact-ID join rate 1.0, telemetry loss 0, duplicate Tool execution
0, wrong-session routing 0, replay divergence 0, correct validation, and zero
owned sandbox leaks. Report correct agents/min, correct steps/min, JCT
mean/p50/p90/p95, Tool latency, physical mean/peak/RSS-time, pause/restore
service time and counts, reclaimed/transient memory, admission blocked time,
prediction error, and fallback rate.
