# Implementation status (2026-09-05)

## Current milestone

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
SSH reaping.

Source, unit, concurrency, replay, and managed-gateway validation are green. A
corrected ARM64 Tool image was built and a fresh kernel-bound Tool template was
accepted on Kunpeng. The full live native SSH pair and managed real-inference
c1 gates remain pending because this Cube deployment does not expose the
per-sandbox mapped SSH port to Runtime; no c20+ paper claim is made for the
native OpenClaw path.

## Evidence

| Boundary | Status |
|---|---|
| Local Python suite | passed; environment-only skips unchanged |
| Policy c60 HOL/session isolation | passed in unit test |
| ARM64 Toolbridge Go suite | passed in pinned Go container |
| Bounded Cube command stream | passed; client deadline regression covered |
| Structured agent/sandbox spans | passed; `session_timing` JSONL plus result spans |
| Managed replay gateway | passed; session-local cursor/delivery/HOL tests |
| Managed API gateway | passed; upstream-compatible forwarding contract test |
| Runtime image build/push | passed; digest `05cb920d...` |
| Tool image build/push | passed; digest `b175fea7...` |
| Fresh Runtime template | passed; `tpl-39efe4ad90384a1fbea3caff` |
| Fresh Tool template + kprobe binding | passed; `tpl-b5cb6f5ee26a41448000b9c2` |
| Replay decision c40 | passed; 12/12 arms |
| Replay full-system c40 | passed; 6/6 arms |
| Replay reclamation c40 | passed; 6/6 arms |
| Replay smoke-matrix c40 | passed; 2/2 arms |
| Replay spatial c40 | passed; 15/15 arms |
| Replay vertical-slice c40 | passed; 1/1 arm |
| CubeMaster/CubeProxy existing Tool 2222 mapping | proved; semantic API returns per-sandbox raw endpoint |
| Endpoint identity/epoch/stale/cross-Tool unit gates | passed; cross-Tool route is rejected before SSH spawn |
| OpenClaw target semantics/PID witness | passed; target is captured and Agent PID witness is stable across lifecycle callbacks |
| Real Runtime-to-Tool native SSH | blocked; current pod-IP mapping is reachable from host but refused from Runtime |
| Pause -> policy restore -> SSH -> telemetry | blocked by deployment topology; no live c1 claim |
| Managed real-inference c1 | pending; operator credential is present but not sent by automation |
| Deterministic c1 replay equivalence | replay path green; native OpenClaw c1 pending |
| c4/c8/c20/c40/c60 | replay c40 matrices green; native OpenClaw scale pending |

The c40 artifacts were retained on kunpeng under
`/tmp/clawbox-baseline-results40`. The latest-source refreshes are
`baseline40-decision-a30292a`, `baseline40-smoke-a30292a`,
`baseline40-reclamation-a30292a`,
`baseline40-full-a30292a`, `baseline40-spatial-a30292a`, and
`baseline40-vertical-a30292a`; the decision run was resumed as
`baseline40-decision` after the command-stream fix. Every run finished with
`GET /v2/sandboxes` returning `[]`.

Live route finding: `Sandbox.get_host(2222)` is an HTTP ingress authority, not
an OpenSSH endpoint. CubeMaster/CubeProxy metadata proves the existing
per-sandbox mapping, and the semantic CubeSandbox API exposes it, but this
deployment reports the CubeNode pod IP as `HostIP`. The host can reach the
mapping while the Runtime VM cannot. Guest `hostname -I` is isolated and must
not be used. A temporary host-network experiment was reverted; the host then
rebooted to clear stale containerd tasks and must be health-checked before
further tests. No c4/c8/c20/c40/c60 native result is valid yet.

See `docs/HANDOFF.md` for exact provenance, discovered failures, host state,
and ordered continuation steps. Kubernetes-era code still in the tree is
unsupported legacy cleanup, not a second execution path.
