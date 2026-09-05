# ClawBox continuation handoff

Updated 2026-09-05 after the semantic CubeSandbox TCP-endpoint cutover and
native-SSH route-gate investigation.

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
They are part of the fast-forward branch being published to `origin/main`.

The current native-endpoint commits are `50db717` (semantic CubeSandbox
endpoint client and initial gates), `1b79924` (synchronous per-admission route
refresh), `b3a1ee7` (long-lived OpenClaw Agent PID witness), and `147cb8e`
(SSH reaping/completion-order proof). `98d3f5d` makes the Runtime shim reject
admissions for any container port other than the Tool SSH port 2222 before
starting OpenSSH. `1903c59` records the current post-stress recovery probe;
`b076890` and `f39cfe0` bound pair creation and document its queue spans. The
companion
CubeSandbox source commit is `64102d9`, which exposes
`Sandbox.get_tcp_endpoint(container_port)` and keeps CubeMaster/CubeProxy route
metadata parsing inside CubeSandbox.

OpenClaw 2026.7.1 was inspected in the installed Runtime image. Its native SSH
backend captures `agents.defaults.sandbox.ssh.target` when the backend is
constructed and reuses that target for later sessions. The old 200 ms config
patch/watch approach is therefore not a valid refresh mechanism and has been
removed. ClawBox now resolves a semantic raw endpoint synchronously during
`/v1/tool/admit`; the admission returns `sandbox_id`, `epoch`, `host`, and
`port`, and the existing policy shim injects that route into only the current
`/usr/bin/ssh` invocation. No allocator, proxy, NodePort, Redis lookup, or
guest-IP discovery was added.

The route contract is identity-bound: stable `HostKeyAlias` and a unique SSH
host key identify the Tool, while `host:port` is treated as ephemeral. Worker
completion requires the recorded SSH subprocess to have been reaped before
completion can release the Tool reservation or trigger pause. Unit tests cover
cross-Tool rejection before subprocess launch, endpoint epoch refresh without
requiring an address change, strict host-key settings, and the OpenClaw Agent
PID surviving Tool pause/restore.

The alternate GitHub child commit `edcbd8a97f47bbe81c3119931bc449bc28f54fbd`
was reviewed against this post-`50db717` tree and is intentionally not
cherry-picked. Its useful lifecycle-failure, per-execution timing, FIFO
admission-metric, and Runtime-local-tool ideas are incorporated selectively in
`7a0740f`; its older endpoint-keyed known-host refresh and restore-must-change-
address assumptions are incompatible with this contract. The commit remains
available by its original SHA; no branch or tag is force-updated or deleted.

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

The semantic CubeSandbox route was proved from CubeMaster/CubeProxy metadata:
Tool port 2222 already produces a per-sandbox `HostIP:mappedPort` mapping, and
pause/resume can change the mapped port. The public SDK contract now exposes
that route without exposing those internal fields. The local route gate then
found a deployment-topology blocker: the current CubeNode is a normal pod, so
the semantic endpoint resolves to its pod IP (for example
`192.168.3.166:mappedPort`). The host can reach that mapping, but a Runtime VM
cannot; the Runtime receives `Connection refused` even though the Tool bridge
is listening and the host-side TCP probe succeeds. The isolated Tool guest
address is not a valid substitute.

During diagnosis, a temporary `hostNetwork=true` CubeNode experiment was
reverted and its failed replacement/debug pods were removed. The node then
required a root-initiated reboot to clear stale containerd tasks. The first
post-reboot health check was green, but the subsequent c40 replay stress left
CubeNode in CrashLoopBackOff (`gateway mac for eth0 via 169.254.1.1 not found`)
and the host later stopped completing SSH handshakes. This is a deployment
recovery blocker, not a reason to add a ClawBox networking layer. Existing
kernel, S3lvol, templates, and results were not intentionally modified or
deleted. The remote ClawBox checkout is at
`d42da58` with the native-endpoint production files synchronized in its working
tree; pre-existing untracked `results/` and `uv.lock` are preserved.

The updated Cube API image was built and deployed as
`127.0.0.1:5000/clawbox/cube-api:route-endpoint-b3a1ee7`, registry digest
`sha256:5556089ac9167040f29f6bac36bfc90043f4cc2d105d43c51dcdd8c3192113de`.

The post-reboot c1 route gate created a fresh Tool and resolved
`192.168.3.157:20004` for container port 2222, then the Runtime identity probe
returned `Connection refused`. The gate failed closed at the intended identity
check and cleaned up; `/v2/sandboxes` was `[]` afterward. No c4/c8 run was
started because the required c1 identity gate is not green.

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

The pre-reboot c40 replay bundles recorded in the earlier handoff are historical
evidence only. Their remote result directory was lost by the reboot and must
not be presented as the current source/configuration result. A post-reboot
smoke c40 attempt initially omitted the existing CubeProxy SDK transport
variables and failed with virtual-host DNS errors after creating and cleaning
all sessions. A second attempt supplied that transport and completed all
creates/cleans, but used the legacy `sandbox-code` template without the
ClawTune Tool bridge; its 40 sessions per arm failed validation. Commit
`65ed9cf` now points every replay suite at the accepted instrumented Tool
template and updates the real-LLM example to current templates. The corrected
c40 suites remain pending CubeNode recovery.

The latest read-only recovery probe on 2026-09-05 confirmed that the public
Kubernetes API is alive (`/version`, `/readyz`, and `/livez` returned 200), the
Cube API health endpoint is alive, and both accepted templates still report
`READY` with the expected CubeNode replica metadata. However,
`GET /v2/sandboxes` remains empty, the CubeNode daemon port 9999 is closed, and
the host SSH service now stalls during key exchange or session setup. One
owner-tagged SDK Tool create was attempted with the existing
`CUBE_PROXY_NODE_IP`/`CUBE_PROXY_PORT_HTTP` transport and a 20-second request
timeout; it hung before returning a sandbox ID and the post-check still showed
zero sandboxes. No c40 retry was started and no recovery mutation was made.

Current-source runs after `a4be792` emit `session_timing` events containing
`session`, `agent`, per-sandbox create, validation, hashing, and cleanup spans.
`7a0740f` additionally records failed lifecycle attempts, control admission and
completion service spans, FIFO admission wait distributions, and keeps the
Runtime-local retrieval/memory tool set separate from the six Tool-VM tools.
The reproducible OpenClaw replay c40 smoke input is
`examples/experiments/openclaw-cube-replay-c40.yaml` with its checked-in model
trace; it is a pending live gate, not a passed run. A 2026-09-05 replay-contract
audit adds an important qualification: that checked-in marker trace is
hand-authored and its `raw_request` contains only the marker conversation. A
representative API-captured OpenClaw request contains the full six-field
chat-completions envelope, the evolving conversation, and six configured Tool
functions; strict replay correctly rejects that marker trace at step 0. It is
therefore a loadable smoke fixture, not exact OpenClaw replay evidence. Exact
OpenClaw replay evidence must come from an API-mode c1 run, the gateway's
exported request/response trace, and a subsequent replay run. That export path
was exercised locally against a retained 27-step API-captured artifact:
27/27 streaming responses were delivered, all canonical inputs matched, and
the replay completeness verdict passed. That artifact is historical and is
not a current live c40 result.
The local full Python suite passes; no new live c40 success claim is made until
the corrected runs complete and each run is checked for zero remaining
CubeSandbox resources.

The managed gateway has both replay and API implementations. Replay is covered
by session-local cursor, retry, delivery, and HOL tests. API mode is covered by
an upstream-compatible integration test that verifies model forwarding and
server-side credential handling. A real upstream model request was not sent by
automation: the operator credential is sensitive, and the live native SSH route
is still unavailable.

The remaining live blocker is concrete and is not a ClawBox endpoint-resolution
bug: this deployment does not route the already-existing CubeSandbox mapping
from a Runtime VM. `get_host(2222)` remains an HTTP ingress authority and is not
an OpenSSH endpoint. Do not claim native c1/c4/c8 or scale results until the
CubeSandbox deployment supplies a deployment-owned, Runtime-reachable raw TCP
endpoint for the same semantic API. Do not work around this with guest IPs,
NodePort, a ClawBox proxy, or a second allocator.

## Highest-priority next steps

1. Recover the CubeSandbox node/Cubelet deployment and verify the existing
   semantic endpoint still has the documented Runtime reachability boundary.
2. Correct the CubeSandbox deployment topology so its existing semantic raw
   endpoint is reachable from Runtime, without adding a ClawBox networking
   layer. Re-run the host/Runtime reachability proof.
3. Run the Cube-only pair smoke with Runtime template
   `tpl-39efe4ad90384a1fbea3caff` and Tool template
   `tpl-b5cb6f5ee26a41448000b9c2`, then verify admitted SSH, Tool pause, demand
   restore, exact execution count, and no policy command/output leakage.
4. Rerun the corrected replay c40 suites sequentially with the accepted
   instrumented Tool template, bounded pair creation, and a zero-leak audit.
5. Run managed OpenClaw c1 in replay and API modes with the operator-provided
   credential, export the API response trace, and require replay equivalence
   before any native OpenClaw scale claim.
6. Join Runtime ClawTune spans, PolicyControl records, Tool bridge JSONL,
   cgroup artifacts, and eBPF artifacts by `(session_id, execution_id)` and
   retain the new `session_timing` events in the result bundle.
7. Validate every native file tool, not merely `exec`. Confirm ClawTune applies
   its execution envelope to SSH commands produced by
   `process/read/write/edit/apply_patch`; unenveloped Agent operations must fail
   closed while setup/validation remain explicit non-agent phases.
8. Add the focused admission-versus-delayed-pause race test, then run native
   OpenClaw c4/c8 HOL/cross-routing and leak tests.
9. Only after those native gates pass, run c20 and finally c40/c60 native
   OpenClaw policy arms. The formal replay c40 matrix is also pending a
   corrected live run after CubeNode recovery.

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
