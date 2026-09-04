# Implementation status (2026-09-04)

## Current milestone

The source architecture has been cut over from Kubernetes/HTTP Tool execution
to standalone CubeSandbox plus native OpenClaw SSH. PolicyControl is a
synchronous metadata-only control path, while Runtime-to-Tool commands and
stdio remain SSH. Cube lifecycle operations now expose explicit states and
wall/monotonic service-time records. ModelGateway exposes all four lifecycle
events and retains session-local replay state.

Source and unit/concurrency validation are green. A corrected ARM64 Tool image
was built and a fresh kernel-bound Tool template was accepted on Kunpeng. The
full live native SSH pair and managed c1 gates have not yet passed, so no c20+
paper claim is valid.

## Evidence

| Boundary | Status |
|---|---|
| Local Python suite | 183 passed, 5 environment skips at cutover |
| Policy c60 HOL/session isolation | passed in unit test |
| ARM64 Toolbridge Go suite | passed in pinned Go container |
| Runtime image build/push | passed; digest `05cb920d...` |
| Tool image build/push | passed; digest `b175fea7...` |
| Fresh Tool template + kprobe binding | passed; `tpl-b5cb6f5ee26a41448000b9c2` |
| Real Runtime-to-Tool native SSH | pending |
| Pause -> policy restore -> SSH -> telemetry | pending |
| Managed real-inference c1 | pending; credential still operator-provided |
| Deterministic c1 replay equivalence | pending |
| c4/c8/c20/c40/c60 | pending |

See `docs/HANDOFF.md` for exact provenance, discovered failures, host state,
and ordered continuation steps. Kubernetes-era code still in the tree is
unsupported legacy cleanup, not a second execution path.
