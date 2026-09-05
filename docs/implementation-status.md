# Implementation status (2026-09-05)

## Current milestone

The source architecture has been cut over from Kubernetes/HTTP Tool execution
to standalone CubeSandbox plus native OpenClaw SSH. PolicyControl is a
synchronous metadata-only control path, while Runtime-to-Tool commands and
stdio remain SSH. Cube lifecycle operations now expose explicit states and
wall/monotonic service-time records. ModelGateway exposes all four lifecycle
events and retains session-local replay state.

Source, unit, concurrency, replay, and managed-gateway validation are green. A
corrected ARM64 Tool image and fresh kernel-bound templates were accepted on
Kunpeng. Source now consumes CubeSandbox's semantic `get_tcp_endpoint(2222)`
contract. Admission-triggered restore returns the current route to the Runtime
SSH hook, which retargets the already-created invocation before execution.
Live route, native pair, and managed real-inference c1 gates remain pending; no
c20+ paper claim is made for the native OpenClaw path.

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
| Semantic TCP endpoint integration | implemented at `50db717`; live gate pending |
| Admission-time in-flight SSH reroute | implemented after `50db717`; live gate pending |
| Real Runtime-to-Tool native SSH | pending live route gate |
| Pause -> policy restore -> same SSH invocation -> telemetry | pending live pair smoke |
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

`Sandbox.get_host(2222)` remains HTTP-only and is never used for OpenSSH.
ClawBox now asks the CubeSandbox SDK for the semantic raw TCP endpoint. The
deployment gate must prove that endpoint is reachable from Runtime, unique per
active Tool, invalidated across pause/restore, and stable in host identity even
when its mapped port changes. Existing host resources and templates remain
preserved until that gate runs.

Formal replay and API experiments share the same OpenClaw process in Runtime,
ClawTune sidecar/plugin, SSH tool path, PolicyControl, and validation. Replay
changes only ModelGateway's response producer. The older
`agent.driver=replay_engine` matrices remain useful systems-capacity baselines,
but they are not end-to-end OpenClaw results.

See `docs/HANDOFF.md` for exact provenance, discovered failures, host state,
and ordered continuation steps. Kubernetes-era code still in the tree is
unsupported legacy cleanup, not a second execution path.
