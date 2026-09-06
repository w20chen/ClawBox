# Implementation status (2026-09-06)

## Current milestone

### Superseding live update: native managed c1 is green

The real managed deterministic-replay path now passes c1 on Kunpeng. OpenClaw
in the Runtime VM consumed two exact recorded model steps, issued one logical
`exec`, synchronously obtained PolicyControl admission, and ran through native
SSH in the Tool VM. Five SSH processes (four backend-maintenance operations and
the Agent operation) each produced an exact policy/Tool-bridge/cgroup-v2/native
eBPF join; the Agent operation also joined the Runtime ClawTune span by the same
execution ID. Final workspace validation passed, telemetry loss and duplicate
execution were zero, and CubeSandbox inventory was empty after cleanup. The
smoke result is `/tmp/clawbox-managed-c1/live-c1-two-step` on Kunpeng; summary
SHA-256 is `47f43e0f10fca147740b86b77a0e3443a890cc93877c917b10f95dff5093e235`.
It is infrastructure evidence only because the temporary two-step marker trace
is not a representative held-out coding trajectory.

The paired `snapshot_pause` c1 path is now live-green as well. Result
`/tmp/clawbox-managed-c1/live-c1-snapshot-fast-reconnect` checkpointed Tool and
Runtime during the pending model request, restored Runtime before releasing the
response, preserved OpenClaw PID 141, and restored Tool only at the subsequent
SSH admission. The endpoint was re-resolved and its epoch advanced even though
Tool identity stayed constant. The run completed two replay model steps, one
logical Agent Tool operation, a 100% exact telemetry join, final workspace
validation, and cleanup to an empty CubeSandbox inventory. Each model record
now states whether a snapshot was actually performed for that request; the
short second wait correctly reports false and contains no stale timestamps.

Runtime checkpoint invalidates the pre-checkpoint upstream HTTP delivery. An
OpenClaw reconnect receives the cached response under the same request ID and
does not consume another replay entry. OpenClaw 2026.7.1 duplicates its original
user turn when it performs that reconnect, so replay normalization is enabled
only after the gateway has proved the exact one-message insertion against an
undelivered request. The proven artifact is then removed from later comparisons;
unrelated duplicate turns continue to fail closed. This is transport recovery,
not relaxed replay matching.

This gate exposed and fixed four installed-version integration details. The
OpenClaw environment sanitizer removes names ending in `_TOKEN`, so ClawBox
passes the per-session policy capability as `CLAWBOX_POLICY_CONTROL_AUTH` and
an explicitly configured session SSH launcher maps it only for the existing
policy shim. The Tool bootstrap creates OpenClaw's deterministic shared SSH
runtime marker so the Tool-owned workspace is never overwritten by a Runtime
workspace mirror. Hook events omit an explicit host before backend resolution,
so ClawTune instruments the selected Tool names for both `gateway` and
`sandbox` hook labels. Finally, mirrored direct/sidecar Runtime span records are
collapsed only when trace, span, execution identity, and outcome agree; a true
execution-ID reuse still fails closed. Replay mismatch and exhaustion are now
sticky for the session and preserve rejection evidence.

### Memory reclamation and native c8 are green

CubeSandbox branch commit `559f894` now preserves CubeMaster's `image_info` in
the CubeAPI template-detail response. The deployed API returned the expected
immutable registry digests for both current templates, and ClawBox's strict
pre-creation provenance gate passed without weakening digest validation. The
first managed c1 run then exposed a separate Runtime environment ordering bug:
the per-session ModelGateway token was generated after the lifecycle object had
copied its environment. That token is now inserted into the lifecycle payload
before VM creation and covered by a regression test. Live OpenClaw subsequently
reached the managed replay gateway; the checked-in smoke trace mismatched the
actual OpenClaw request and failed closed with zero leaked sandboxes, as it
must. Divergence now writes a session-qualified expected/actual canonical
request witness even when the gateway output directory does not exist yet, so
concurrent sessions cannot overwrite one another's rejection evidence. This is
an infrastructure gate result, not representative replay evidence.

The earlier deployment-topology blocker described later in this document has
been resolved in CubeSandbox, not bypassed in ClawBox. CubeSandbox source commit
`0dda1c4` recognizes an exact `remote_port_mapping` route before the ordinary
Runtime egress deny decision. The Kunpeng CubeNode is running the corresponding
temporary diagnostic binary. Runtime now reaches only CubeSandbox's semantic
`get_tcp_endpoint(2222)` result; no proxy, guest-IP fallback, NodePort, Redis
lookup, or ClawBox port allocator was introduced.

Fresh immutable artifacts used by the current gates are Runtime template
`tpl-437392a8c57b48ccb32ef2ee` (image digest `sha256:f4009d045edd932e0246e790af6e43bff36a295b9fb0dd41d2aa38864ea132d0`)
and Tool template `tpl-4a67524e1fcd41859905c77b` (image digest
`sha256:95ef090dc3036f7f2acd9190234f04d07a226b95b6855e855adf76a4e69aa092`).
The Tool image contains the exact patched OpenCloudOS 6.6.119 kernel source
needed by ClawTune/BCC; the prior generic 6.18 headers produced invalid native
telemetry and are not accepted evidence.

The bounded physical-memory probe is now green. A Tool with a touched 1 GiB
allocation had 1,248,739,328 bytes of matching host-process RSS before pause
and no matching live process afterward. Whole-host `MemAvailable` increased by
948,801,536 bytes. Restore completed in 0.203 seconds and preserved guest PID
20 and its allocation. The evidence is
`/tmp/clawbox-memory-reclaim-exact.json` on Kunpeng (SHA-256
`1b2104dd17194886f3fb7b9b43b170066dc4e99d25f3a289f2b9a6d6f6985979`).
This verifies that this deployment's CubeSandbox pause means snapshot plus live
microVM eviction; the formal experiments must still report every event's
observed bytes rather than infer reclamation from API success.

The exact native route/identity/telemetry gate passes at c4 and c8. At c8 all
16 admitted SSH executions joined exactly across PolicyControl, Tool bridge,
cgroup-v2, and native eBPF artifacts; telemetry loss, duplicates, wrong-Tool
execution, and leaks were zero. Every stale pre-pause endpoint failed, every
endpoint epoch advanced, and cross-Tool identity was rejected before OpenSSH.
Kunpeng evidence files are `/tmp/clawbox-route-c4-telemetry.json` (SHA-256
`dc025d26e8af320d1675e4a201eed27a6da4ad644e76a61bc5e3d8a83f5a6c84`)
and `/tmp/clawbox-route-c8-telemetry.json` (SHA-256
`253c19870d10c070c1103496be0fb44da7156bba42d2eb10ded6ee4c0e7396c2`).
The post-gate CubeSandbox inventory was empty.

The shared memory ledger now distinguishes observed/reserved physical capacity
from incremental commitments. Admission charges
`max(observed_host_delta, lifetime_capacity_claims) + incremental_reservations
+ safety_headroom`; VM create/restore footprints and predicted Tool execution
increments are reserved before materialization, while conservative lifetime
claims are not double-counted once resident. Proactive model-response handling
restores only Runtime. Tool remains swapped until its next admission has first
reserved execution and restore memory. These are implementation mechanisms,
not oracle policy choices; replay still cannot consume the recorded future wait.

The remaining paper gates are representative API-captured trajectories, frozen
ClawTune KB calibration and guest-to-host increment calibration, managed replay
c20/c40/c60 with repeated orthogonal baselines, and real-LLM c1/c2/c4. The
checked-in single marker trace remains smoke-only and must not be presented as
formal evidence.

The ClawTune reuse path was also exercised against four preserved Kunpeng
recording directories. Exact span/bridge joins were 100% and one observation
per directory passed the current quality gate. This exposed a typed-enum bug in
ClawBox's cgroup overlay: the validated `CollectionQuality` value was replaced
with a raw string, which later broke KB snapshot construction. The join now
retains the enum and the ClawTune-backed offline pipeline completes. Those four
homogeneous observations are useful regression evidence only; they are too few
and too narrow to become the frozen formal KB.

The deployment contract is now written down in
[`docs/cubesandbox-setup.md`](cubesandbox-setup.md) and linked from the
README. It has two explicit paths: prepare a fresh standalone CubeSandbox
deployment, or preflight an existing deployment before running ClawBox. The
checked-in CubeSandbox endpoint, hairpin, and template-provenance patches plus
`prepare-semantic-source.sh` makes the semantic CubeAPI route and matching SDK
reproducible from the public v0.7.0 source tag. The Kunpeng Kubernetes profile
is explicitly diagnostic only because its HostPort address is a Pod IP that
Runtime cannot reach.

The source architecture has been cut over from Kubernetes/HTTP Tool execution
to standalone CubeSandbox plus native OpenClaw SSH. PolicyControl is a
synchronous metadata-only control path, while Runtime-to-Tool commands and
stdio remain SSH. Cube lifecycle operations now expose explicit states and
wall/monotonic service-time records. ModelGateway exposes all four lifecycle
events and retains session-local replay state.

Native SSH now consumes CubeSandbox's semantic `get_tcp_endpoint(2222)` API.
The Worker resolves it synchronously during admission and returns an endpoint
epoch; the existing Runtime policy shim applies the returned route only to the
current SSH process. The OpenClaw target watcher was removed after verifying
that the installed backend captures its target at construction. Stable
HostKeyAlias/host keys preserve Tool identity, and completion is ordered after
SSH reaping. Raw semantic endpoints without an explicit mapped port now fail
closed instead of falling back to SSH port 22. Commit `147cb8e` fixes the
timestamp sampling order and adds a
blocking-child regression proving `/complete` is not posted while the SSH child
is still active. Commit `98d3f5d` also rejects an admission for any container
port other than the Tool SSH port 2222 before OpenSSH starts.

Native OpenClaw `snapshot_pause` now checkpoints/swap-outs both the Runtime and
Tool VMs during a model wait. Runtime is restored synchronously before the
ModelGateway releases the pending response, and Tool remains eligible to stay
swapped until the next SSH admission. The OpenClaw Agent PID is witnessed before
Runtime checkpoint and after restore; gateway records include paired role
timings and the response-release hold. Resident arms keep both VMs resident,
while replay-engine compatibility baselines retain their historical Tool-only
lifecycle.

Every session result and `session_timing` JSONL event now carries role-labeled
time spans for Runtime and Tool lifecycle operations, native Tool operations,
replay model waits, and managed model response hold/delivery. Lifecycle spans
include wall-clock and monotonic timestamps plus service time, state transition,
and status fields, so cross-process correlation does not depend on a single
adjustable clock.

Managed native Tool admissions now become execution-level observation rows
after the strict policy/Runtime/bridge/cgroup/eBPF join succeeds. Each row
retains the command prediction key and source, fallback level, predicted
incremental memory, admitted reservation, admission wait, guest cgroup-v2 peak
RSS, execution duration, average CPU use, telemetry validity, and prediction
error/coverage. Arm results aggregate P90 prediction error, absolute error,
underestimation, and fallback rate, and record the frozen prediction artifact
SHA-256 in provenance. These observations are exportable training evidence;
the Worker does not mutate the frozen KB during a comparison arm.

CubeSandbox lifecycle records also sample node physical memory immediately
before and after create/checkpoint/restore/destroy. The records explicitly
label whole-host `MemAvailable`-derived usage separately from guest Tool
cgroup memory and report observed reclamation/growth. This is a node-wide
concurrent observation, not exclusive per-VM attribution, but it prevents a
successful pause API response from being reported without physical-memory
evidence. Host `oom_kill` deltas fail the arm, and admission metrics count each
configured-budget or emergency-free-memory safety intervention.

The CubeSandbox `64102d9` pause implementation and a bounded live Kunpeng
probe now establish the lifecycle semantics behind those records. Pause runs
`PauseToSnapshot` and then destroys the live microVM/shim while retaining a
paused tombstone. With a touched 1.5 GiB Tool allocation, the diagnostic probe
observed about 1.69 GiB sandbox-process RSS before pause, no matching live
process after pause, and about 1.64 GiB returned to whole-host
`MemAvailable`; resume preserved guest PID 29 and the allocation. The sandbox
inventory was empty after cleanup. This is lifecycle-gate evidence rather than
a formal policy result. Lifecycle JSON now labels the mechanism and reports a
signed whole-host net change plus whether reclamation was actually observed.

The durable system boundary is recorded in
[`docs/research-system-contract.md`](research-system-contract.md). It explicitly
assigns command normalization, duration/resource prediction, P50/P90 and
fallback behavior, cgroup-v2 collection, and eBPF/kprobe telemetry to the
pinned sibling ClawTune implementation. ClawBox owns the surrounding managed
admission, VM residency, exact joins, freezing, and reporting layers.
The legacy ClawBox compatibility projector no longer copies ClawTune's shell
lexer, fallback-node layout, target calculations, or snapshot serialization.
It now constructs native `CompletedCall` records and uses the pinned
`RuntimeToolResourceKB` and `ToolCallQuery` APIs to causally advance, serialize,
and reload-check the frozen snapshot. Managed command keys likewise call
ClawTune's native normalizer through this thin adapter.

Schema-v2 now supports deterministic heterogeneous managed workloads without
splitting each trace into a different policy arm. With
`workload.session_assignment: round_robin`, session indices consume the same
ordered A/B/C trace mix for every baseline. Results retain the exact
session-to-case/trace assignment and all configured trace hashes. Arrival is
also explicit: the default is labeled `burst`, while `fixed_stagger` uses a
deterministic per-index offset and records that offered schedule separately
from provisioning and Agent JCT.

When an experiment pins `image_digest`, Worker now checks the official
CubeSandbox Template record before creating a VM and rejects a `READY` record
whose image digest does not match. This makes the currently stale Runtime
template fail closed instead of silently running an older artifact.

The Worker keeps c40 session concurrency at 40 while bounding simultaneous
Runtime/Tool pair creation (eight by default) and records any queue wait as a
`sandbox.create.queue` span. This protects Cubelet/containerd during a stress
run without changing the endpoint contract or SSH data path.

Non-lifetime policies now reserve each VM's configured memory footprint before
calling CubeSandbox create or restore. Tool restore therefore occurs while the
command prediction reservation is already held, and adds the Tool VM footprint
under the same coordinator; Runtime response-path restore reserves the Runtime
VM footprint. The configured checkpoint/restore headroom remains in every
capacity calculation. Lifecycle reservation waits are reported separately and
no longer inflate Tool-admission counts or blocked-time percentiles.

Tool admission now marks the session active before restore or memory waiting,
so a competing pressure admission cannot pause that Tool in the admission
window. Failed admission clears the marker, and native route resolution occurs
only after the reservation/restore boundary. The focused race regression is
covered alongside the existing SSH-child completion-order test.

The policy shim also fails closed on a duplicate idempotent admission response;
it never launches a second SSH subprocess for the same execution identity.
The installed OpenClaw 2026.7.1 backend audit additionally found that only
`exec` exposes a command that ClawTune can envelope. Native filesystem and SSH
backend-preparation calls are created below the tool-hook boundary. The shim now
recognizes only the captured OpenClaw `-F ... openclaw-sandbox <command>` shape,
mints a unique ID, synchronously admits it, and inserts the Tool-bridge envelope
before launching OpenSSH. Arbitrary unenveloped SSH still fails closed. Actual
filesystem calls and backend-maintenance calls are labeled separately; both are
exactly joined to bridge/cgroup/eBPF records, while only the former contributes
to Agent Tool-call throughput. Runtime ClawTune span coverage is reported
separately because these below-hook executions have no Runtime span ID.
Commit `5b41a48` hardens zero-leak cleanup: an admission that never acquired a
lifetime reservation cannot release one during `finally`, and cleanup attempts
all task-owned sandboxes before reporting any kill error or remaining owner.
The native artifact join now also fails closed when policy, Runtime trace, or
Tool bridge records belong to a different session; the Worker’s refreshed SSH
descriptor is used only by post-agent artifact collection, not by the captured
OpenClaw backend.
Runtime egress now honors the arm's explicit `allow_internet_access` setting:
closed-network arms deny default IPv4 egress after applying their control-plane
allowlist, while explicitly internet-enabled arms retain egress.

Commit `7a0740f` adds failed lifecycle-attempt records, per-execution
admission/completion service spans, FIFO admission wait distributions, and
explicit Runtime-local versus Tool-VM tool policy. Commit `743c68a` adds
ordered wall-clock and monotonic nanosecond fields to experiment event JSONL.
The OpenClaw replay c40 smoke spec and hand-authored model trace are
`examples/experiments/openclaw-cube-replay-c40.yaml` and
`examples/traces/openclaw-cube-replay.jsonl`; they validate the experiment
loader and replay response shape, but are not exact OpenClaw request evidence.
An exact OpenClaw replay trace must be exported from a successful API-mode c1
run before replay c40 is claimed.

The replay-engine compatibility planner no longer receives the held-out model
wait as a prediction. Fixed-delay timers race model completion causally, and
wait-aware/proactive decisions consume only the configured request-time wait
estimate. Proactive restore uses the predicted target and replay still waits
the full recorded duration; it no longer shortens a model wait by the prefetch
lead. The matrix audit requires both the estimate and its provenance source for
formal wait-aware/proactive arms.

Source, unit, concurrency, replay, and managed-gateway validation are green. A
corrected ARM64 Tool image was built and fresh kernel-bound Runtime/Tool
templates were accepted on Kunpeng. The full live native SSH pair and managed
real-inference c1 gates remain pending because this Cube deployment does not
make the existing per-sandbox mapped SSH port Runtime-reachable; no c20+ paper
claim is made for the native OpenClaw path.

The checked-in baseline matrix audit is also green: all eight schema-v2
experiment files load and plan with the expected c1/c4/c20/c40/c60 levels,
all policy tuples resolve through the immutable current catalog, and no
removed Tool template or `cube_shell` OpenClaw prompt is present. The old
non-paper direct-Firecracker study translator is now explicit and fail-closed;
it cannot silently select a removed workflow schema. The older
`paper_experiment` runner remains legacy code and is outside the supported
native Worker path.
Current replay-worker telemetry likewise records the actual Tool operation name
(for example `exec`) instead of the retired `cube_shell` label.

The matrix audit now also requires formal OpenClaw experiments to pin both
Runtime and Tool by immutable template ID, source image reference, and SHA-256
image digest. Historical replay-engine capacity matrices may retain aliases,
but they are not admissible as OpenClaw artifact evidence.

A local c40 Worker regression materializes every baseline-catalog entry—10
canonical recipes plus 7 compatibility aliases—with 40 concurrent sessions
per arm and verifies successful completion, complete session/sandbox/agent/
cleanup spans, and zero remaining fake owned sandboxes. The policy shim tests
cover ClawTune-enveloped operation names, recognized OpenClaw
filesystem/backend SSH, and fail-closed behavior for any other unenveloped SSH
operation. Result bundles
now also separate Runtime and Tool template provenance fields.
The live CubeSandbox c40 result remains pending the Runtime-reachable endpoint.
The latest bounded Kunpeng probe completes SSH authentication but stalls before
the session channel opens, so the remote host still cannot be used for the
required read-only recovery or native pair gates.

The semantic CubeSandbox API route is now deployed on Kunpeng from the existing
CubeSandbox source commit `64102d9`; before that rollout the API returned an
empty-body 404 even though direct CubeMaster metadata contained the per-sandbox
`HostIP:mappedPort` route. Fresh current-kernel Runtime/Tool templates were
registered and the SDK endpoint call now returns the expected raw endpoint.
The host-side Tool probe succeeds, but Runtime-to-CubeNode-Pod traffic is still
refused, so this remains a deployment-topology blocker rather than a ClawBox
endpoint-resolution gap. A zero-sandbox host-network experiment confirmed the
existing `remote_port_mapping` and attached `from_world` BPF program, but host,
CubeProxy, and external TCP probes all refused the advertised endpoint. The
experiment was reverted; a root reboot then restored CubeNode to `3/3` with
`hostNetwork=false` and left the API inventory empty.
The current-template follow-up used one fresh Runtime/Tool pair and tested three
distinct routes with strict host-key and Tool-marker validation. The semantic
endpoint `192.168.3.157:20019`, direct Tool `172.16.0.2:2222`, and physical-node
`193.124.7.2:20019` routes were all refused before SSH authentication. The
probe cleaned its pair and `GET /v2/sandboxes` remained empty. The result rules
out treating direct SandboxIP or the physical address as a ClawBox fallback;
the remaining same-node path is a CubeVS forwarding/hairpin problem.

The standalone one-click bundle was not installed on Kunpeng. Two build-only
attempts were made from the prepared CubeSandbox checkout: the first stalled
in the ARM64 builder's Ubuntu package download, and the retry was canceled in
the Ubuntu/LLVM package layers before an image or bundle was produced. The
live diagnostic Kubernetes deployment was not modified and remains healthy
with zero sandboxes.

## Evidence

| Boundary | Status |
|---|---|
| Local Python suite | passed; environment-only skips unchanged |
| Policy c60 HOL/session isolation | passed in unit test |
| ARM64 Toolbridge Go suite | passed in pinned Go container |
| Bounded Cube command stream | passed; client deadline regression covered |
| Pinned Template provenance gate | passed; mismatched READY image is rejected before VM creation |
| Structured agent/sandbox spans | passed; `session_timing` JSONL, lifecycle failure records, and ordered event timestamps |
| Managed replay gateway | passed; session-local cursor/delivery/HOL tests |
| Managed API gateway | passed; HTTP listener and upstream-compatible forwarding tests |
| Runtime image build/push | passed; digest `05cb920d...` |
| Tool image build/push | passed; digest `b175fea7...` |
| Runtime template record | reconciled; `tpl-67569219b64f4a80836a1f35` is `READY` on `sha256-a63aa77e9c2d` and pins Runtime image `sha256:e3b0bb69751c...` built from ClawBox `346da48` |
| Tool template record | reconciled; `tpl-06b699a92c694c7ba3e6465b` is `READY` on the same guest kernel and pins Tool image `sha256:b175fea75b4c...` |
| Fresh normal-network Runtime template + kprobe binding | passed; `tpl-3871262f976946fa835f3035` |
| Fresh normal-network Tool template + kprobe binding | passed; `tpl-bc7533c482984dcc9594efdf` |
| Host-network diagnostic templates | not reusable after rollback; Runtime `tpl-55ad06ce2a3a4d61b5682ef2`, Tool `tpl-980d2310ac4c4dfcbd077128` |
| Replay decision c40 | historical bundle; corrected rerun pending a Runtime-reachable semantic endpoint |
| Replay full-system c40 | historical bundle; corrected rerun pending a Runtime-reachable semantic endpoint |
| Replay reclamation c40 | historical bundle; corrected rerun pending a Runtime-reachable semantic endpoint |
| Replay smoke-matrix c40 | corrected config committed; rerun pending a Runtime-reachable semantic endpoint |
| Replay spatial c40 | historical bundle; corrected rerun pending a Runtime-reachable semantic endpoint |
| Replay vertical-slice c40 | corrected config committed; rerun pending a Runtime-reachable semantic endpoint |
| CubeMaster/CubeProxy existing Tool 2222 mapping | proved; semantic API returns per-sandbox raw endpoint |
| Endpoint identity/epoch/stale/cross-Tool unit gates | passed; cross-Tool route is rejected before SSH spawn |
| OpenClaw target semantics/PID witness | passed; target is captured and Agent PID witness is stable across lifecycle callbacks |
| Native OpenClaw paired Runtime/Tool snapshot | passed in local integration gate; Runtime PID survives checkpoint/restore and response release is held until Runtime restore |
| SSH completion ordering | passed; `/complete` follows child reaping and records `ssh_reaped_at <= execution_completed_at` |
| Real Runtime-to-Tool native SSH | blocked in CubeVS; current-template Runtime gets TCP refusal from semantic Pod-IP HostPort, physical-IP HostPort, and direct SandboxIP; zero leaks |
| Pause -> policy restore -> SSH -> telemetry | blocked by deployment topology; no live c1 claim |
| Managed real-inference c1 | pending; operator credential is present but not sent by automation |
| Deterministic c1 replay equivalence | prior 27-step API-captured export/replay matched 27/27; current native OpenClaw c1 pending |
| c4/c8/c20/c40/c60 | corrected replay c40 and native OpenClaw scale pending a Runtime-reachable semantic endpoint |

The earlier c40 artifacts were retained on kunpeng under
`/tmp/clawbox-baseline-results40`, but the post-reboot cleanup removed that
directory. Do not reuse those historical claims as current evidence. The first
current smoke attempt omitted `CUBE_PROXY_NODE_IP` and
`CUBE_PROXY_PORT_HTTP`; the second used them but selected the old
`sandbox-code` template and failed validation because replay commands were
wrapped by the required Tool bridge. Commit `65ed9cf` corrects the suite
templates. The corrected c40 run is pending recovery of CubeNode, which is
currently not healthy after the stress attempt.

The latest read-only recovery probe found the Kubernetes API ready and both
accepted templates still `READY`, but Cube API `GET /v2/sandboxes` empty,
CubeNode port 9999 closed, and host SSH stalling during key exchange/session
setup. A single owner-tagged SDK Tool create with the existing CubeProxy
transport hung before returning an ID and left zero sandboxes afterward; no
additional stress or recovery mutation was attempted. A subsequent
`GET /templates/<id>` check also found that the accepted Runtime ID reports
`sha256:79a492d2...` and an older `CLAWBOX_REVISION`, while the checked-in
experiment pins `sha256:05cb920d...`; the Runtime template must be rebuilt or
reconciled before live native-SSH evidence is admissible. The Tool template
still reports the checked-in Tool digest and its create metadata exposes the
existing `2222:49983` mapping. The older replay-matrix Runtime alias also
resolves to a `READY` template with `sha256:02ae0ff3...`, so its historical
results are not evidence for the current Runtime artifact.

Live route finding: `Sandbox.get_host(2222)` is an HTTP ingress authority, not
an OpenSSH endpoint. CubeMaster/CubeProxy metadata proves the existing
per-sandbox mapping, and the semantic CubeSandbox API exposes it, but this
deployment reports the CubeNode pod IP as `HostIP`. The host can reach the
mapping while the Runtime VM cannot. Guest `hostname -I` is isolated and must
not be used. A temporary host-network experiment was reverted; the host then
rebooted to clear stale containerd tasks. Post-reboot c1 again failed at
Runtime -> mapped endpoint with `Connection refused` and cleaned up to zero
sandboxes. The later c40 stress temporarily left CubeNode CrashLooping with a
missing pod-network gateway MAC. A subsequent zero-sandbox rollback and reboot
restored CubeNode `3/3`; no c4/c8/c20/c40/c60 native result or corrected replay
c40 result is valid yet because Runtime still cannot reach the semantic
endpoint.

See `docs/HANDOFF.md` for exact provenance, discovered failures, host state,
and ordered continuation steps. Kubernetes-era code still in the tree is
unsupported legacy cleanup, not a second execution path.
