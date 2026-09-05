# ClawBox continuation handoff

Updated 2026-09-05 after semantic CubeSandbox TCP endpoint integration and the
admission-time route correction.

## Fixed research architecture

CubeSandbox is the only sandbox and multi-node substrate. Each logical Agent
owns one Runtime CubeSandbox and one Tool CubeSandbox.

- Runtime contains OpenClaw, the ClawTune plugin/sidecar, agent state, model
  client, web retrieval, and agent-memory lookup.
- Tool contains the mutable workspace and executes `exec`, `process`, `read`,
  `write`, `edit`, and `apply_patch` through native SSH.
- PolicyControl is a synchronous metadata-only admission boundary. It never
  proxies commands, file contents, stdout, or stderr.
- ModelGateway presents one OpenAI-compatible endpoint to Runtime. API mode
  forwards to a real upstream. Replay mode sleeps for recorded model latency
  and returns the recorded response. Both modes drive the same OpenClaw path.
- CubeSandbox owns create, pause/checkpoint, connect/restore, placement,
  semantic TCP endpoint publication, and destroy.

Kubernetes/SandboxTask/WorkerBridge/cube_shell/direct-Firecracker paths are not
supported architecture. Legacy files still present are cleanup work, not an
alternative execution path.

## Current source state

`main` and `origin/main` were at `50db717` when this continuation began. That
commit replaced deployment-supplied SSH targets with
`Sandbox.get_tcp_endpoint(2222)` and added an endpoint topology validator.

The continuation after `50db717` closes an additional in-flight race. OpenClaw
constructs an SSH command before the Runtime hook asks for admission. If Tool
was restored during admission, its mapped port could change while the pending
command still named the old port. PolicyControl now returns the current
`ssh_target`; the Runtime hook rewrites that same invocation before launching
`/usr/bin/ssh`. Known-host replacement is completed atomically before the new
route is published. A changed endpoint host fails explicitly because Runtime's
network allowlist is fixed at creation; a changed mapped port is supported.

Admission uses a process-wide FIFO reservation queue. Each Runtime HTTP request
is handled concurrently, so a slow callback in one session does not serialize
independent sessions. Duplicate `(session_id, execution_id)` admissions and
completions are idempotent and execute callbacks once. Results include FIFO
queue depth/wait distributions, Runtime round-trip time, and estimated control
overhead after intentional queue and restore time are removed.

ClawBox JSONL events include a per-arm sequence number, nanosecond wall and
monotonic timestamps, lifecycle service records, endpoint refresh spans,
model-request lifecycle records, admission records, validation/hashing spans,
and cleanup spans. Native Tool evidence remains joined by
`(session_id, execution_id)` across ClawTune, PolicyControl, Tool bridge, cgroup,
and eBPF artifacts.

## Existing evidence

- Local Python suite passes with platform-only skips on the Windows checkout.
  That checkout uses Python 3.11 and lacks the Cube SDK, so repeat under the
  declared Python 3.12 environment on Kunpeng.
- Policy c60 independent-session HOL test passes.
- Toolbridge Go tests previously passed on ARM64 with Go 1.25.
- Managed replay and API gateway integration tests pass.
- Lightweight replay-engine c40 matrices passed for decision, full-system,
  reclamation, smoke-matrix, spatial, and vertical-slice suites. These are
  capacity/system baselines, not formal OpenClaw c40 evidence.

Published immutable images and accepted templates:

```text
Runtime template: tpl-39efe4ad90384a1fbea3caff
Runtime image:    127.0.0.1:5000/clawbox/runtime-cube-arm64@sha256:05cb920d0c79ee381263f2d57663a8c068d889394b6a9ad942af34810931422f

Tool template:    tpl-b5cb6f5ee26a41448000b9c2
Tool image:       127.0.0.1:5000/clawbox/tool-cube-arm64@sha256:b175fea75b4cd43d5116d93e0c2fc1b745a68d24d84446d5af0b3c72d42a8781
```

Do not expose the loopback registry publicly. The previously used private
Docker-bridge mirror is deployment plumbing and may need recreation after a
reboot. Preserve existing kernels, S3lvol state, templates, and result bundles.

## Next live gates

The Kunpeng host setup is now represented by
`install-cubesandbox-kunpeng920.sh`, the vendored CubeSandbox endpoint patch,
`kunpeng-research.env.example`, and `provision-kunpeng-openclaw.sh`. New runs
must use these entry points; shell-history-only registry or CubeAPI setup is no
longer acceptable evidence.

On `weitianc@193.124.7.2`, first update the checkout and use Python 3.12 with
the declared CubeSandbox SDK. Then:

1. Run `validate-cubesandbox-tcp-endpoints.py` at c1 and c4. Require unique
   active/resumed routes, wrong-session rejection, stale-route failure, stable
   endpoint host, changed mapped port, and zero owner leaks.
2. Rebuild the Runtime image because the SSH hook changed, register a fresh
   Runtime template by immutable digest, and keep the existing Tool template if
   its source inputs are unchanged.
3. Run `smoke-cubesandbox-agent-pair.py`. Its post-pause operation is built with
   the stale route; the admission callback must restore Tool and the hook must
   reroute that same operation. Require exact execution count, complete cgroup
   and eBPF evidence, no command/output in PolicyControl, and zero leaks.
4. Run managed OpenClaw c1 in API mode with an operator-provided credential.
   Export the generated model trace, then run the same case in replay mode and
   require canonical input match, complete delivery, identical validation, and
   zero divergence.
5. Validate all six Tool-VM workspace tools. Confirm Runtime-local web/memory
   operations do not create PolicyControl admissions or wake a paused Tool.
6. Run native c4/c8 HOL, cross-routing, delayed-pause, and leak gates. Then run
   c20 and formal OpenClaw+replay c40/c60 policy arms.

Use `examples/experiments/openclaw-cube.yaml` for API c1 and copy its exported
trace to the path referenced by
`examples/experiments/openclaw-cube-replay-c40.yaml` for formal replay.

## Acceptance metrics

For every formal arm retain separate Runtime/Tool create, Agent, model wait,
Tool, admission queue, checkpoint, restore, endpoint refresh, validation,
hashing, destroy, and stabilization boundaries. Required correctness is exact-ID
join rate 1.0, telemetry loss 0, duplicate Tool execution 0, wrong-session
routing 0, replay divergence 0, correct validation, and zero owned sandbox
leaks. Report agents/min, steps/min, JCT mean/p50/p90/p95, Tool latency,
admission round-trip and control overhead, FIFO depth/wait, physical
mean/peak/RSS-time, pause/restore service time and counts, reclaimed/transient
memory, prediction error, and fallback rate.

After native c1 is retained as a reproducible checkpoint, delete unsupported
Kubernetes and HTTP-execution components and their tests/examples. Do not
delete CubeSandbox installation, kernel, registry, or storage recovery assets
merely because CubeSandbox itself is deployed on Kubernetes.
