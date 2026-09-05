# Implementation status (2026-09-06)

## Current milestone

The deployment contract is now written down in
[`docs/cubesandbox-setup.md`](cubesandbox-setup.md) and linked from the
README. It has two explicit paths: prepare a fresh standalone CubeSandbox
deployment, or preflight an existing deployment before running ClawBox. The
checked-in `deploy/cubesandbox/semantic-tcp-endpoint.patch` plus
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

When an experiment pins `image_digest`, Worker now checks the official
CubeSandbox Template record before creating a VM and rejects a `READY` record
whose image digest does not match. This makes the currently stale Runtime
template fail closed instead of silently running an older artifact.

The Worker keeps c40 session concurrency at 40 while bounding simultaneous
Runtime/Tool pair creation (eight by default) and records any queue wait as a
`sandbox.create.queue` span. This protects Cubelet/containerd during a stress
run without changing the endpoint contract or SSH data path.

Tool admission now marks the session active before restore or memory waiting,
so a competing pressure admission cannot pause that Tool in the admission
window. Failed admission clears the marker, and native route resolution occurs
only after the reservation/restore boundary. The focused race regression is
covered alongside the existing SSH-child completion-order test.

The policy shim also fails closed on a duplicate idempotent admission response;
it never launches a second SSH subprocess for the same execution identity.
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
cleanup spans, and zero remaining fake owned sandboxes. The policy shim tests also
cover `exec`, `process`, `read`, `write`, `edit`, and `apply_patch`, plus
fail-closed behavior for an unenveloped Agent SSH operation. Result bundles
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
The follow-up normal-network probe also tested the distinct Runtime-to-Tool
`SandboxIP:2222` path: CubeProxy setup succeeded, but Runtime's direct SSH
attempt timed out. The single-node inventory has no second physical node or
private NIC for an independent HostPort test, so no same-node hairpin or BPF
hit-path success is claimed.

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
| Runtime template record | mismatch; `tpl-39efe4ad90384a1fbea3caff` is `READY` but currently reports `sha256:79a492d2...` / `CLAWBOX_REVISION=c4af3d825...`, not the checked-in `sha256:05cb920d...` pin |
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
| Real Runtime-to-Tool native SSH | blocked; normal-network pod-IP mapping is reachable from host but refused from Runtime; host-network map/BPF proof still refused TCP |
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
