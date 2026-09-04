# ClawBox continuation handoff

Updated 2026-09-04 after the native-SSH architecture cutover and first live
Tool-template gate.

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

All are pushed to `origin/main`. Local full suite at `49ab5a4` passed 183 tests
with 5 environment skips. Kunpeng targeted Python tests passed. Toolbridge Go
tests passed on ARM64 in `golang:1.25-bookworm` using
`GOPROXY=https://goproxy.cn,direct`.

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

## Highest-priority next steps

1. Rebuild/push Runtime at `de74a26` for clean shared provenance, then register
   a fresh Runtime template exposing 49983 and bound to the same kernel.
2. Add/run a Cube-only pair smoke using Tool template
   `tpl-b5cb6f5ee26a41448000b9c2`. Verify what address/port from
   `tool.get_host(2222)` is reachable by Runtime native OpenSSH. Do not assume
   `:2222` until tested.
3. Exercise the actual Runtime SSH shim against a host PolicyControlServer:
   admitted command, Tool pause, demand-triggered restore before SSH connect,
   completion, exact command execution count, and no command/output in policy
   logs.
4. Join Runtime ClawTune spans, PolicyControl records, Tool bridge JSONL,
   cgroup artifacts, and eBPF artifacts by `(session_id, execution_id)`. The
   current native runner returns control records but does not yet copy and
   fail-closed validate all Tool artifacts; this is the main source gap.
5. Validate every native file tool, not merely `exec`. Confirm ClawTune applies
   its execution envelope to the SSH commands produced by
   `process/read/write/edit/apply_patch`. Any unenveloped Agent operation must
   fail closed; setup/validation operations need an explicit non-agent phase.
6. Fix one lifecycle race before scale: model-wait pause checks `tool_active`,
   and per-session locks serialize callbacks, but add a focused race test where
   admission and a delayed pause become ready simultaneously.
7. After c1 record/replay equivalence, run real Cube c4/c8 HOL/cross-routing and
   leak tests. Only then run c20 and finally c40/c60 policy arms.

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
