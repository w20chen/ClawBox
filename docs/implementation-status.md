# CubeSandbox migration status (2026-09-04)

## Managed OpenClaw measurement boundary (stopped cleanly)

The current important milestone is source and controller readiness for a
semantically correct managed c1 run; the formal paper matrix has not started.

- Latest implementation commit: `386170a` (`Make cell controller services
  and worker failures diagnosable`); the documentation handoff is committed
  at repository HEAD `d9ee6dc`, both pushed to `origin/main`.
- Local verification: targeted managed/controller tests and `git diff --check`
  pass; the preceding full suite passed with five environment skips. The new
  Worker image publication was intentionally stopped before completion, so the
  live Worker image remains the prior immutable digest listed below.
- Controller image live on Kunpeng:
  `sha256:2b3814b06de6da7ef00b86201db0cbcfc23061a6fe441ef0520146bae4e0a1d5`.
- Worker image used by the temporary controller-created attempt:
  `sha256:086f6556f345f76e2c763b3a77f4f53a0de7e3b3c05527f1c5d48d01063475b6`
  (built from the managed c1 fixture commit, before the sanitized top-level
  failure-log change).
- The missing cell-controller Service RBAC was the concrete infrastructure
  defect found at this boundary. `deploy/control-plane-rbac.yaml` now grants
  only `get/create/delete` on Services in `clawbox-benchmarks`; the live
  `can-i` checks returned `yes` for all three verbs.
- Normal reconciliation was verified: a fresh `SandboxTask` caused the
  controller to create its owner-referenced two-port NodePort Service and
  Worker Job. The temporary attempt was then removed after it failed before
  producing a result; it is not accepted measurement evidence.
- Temporary task, Job, model-mock Pod/Service/Secret, and task-owned NodePort
  resources are gone. A direct Cube inventory returned no Sandbox entries
  (only Template records); existing historical project resources were not
  deleted.
- No real API c1 can be recorded yet: `clawbox-benchmarks/clawbox-llm` is
  absent. The available old result traces use a different tool schema and must
  not be relabeled as current managed evidence.

Evidence classification at this boundary:

| Area | Classification |
|---|---|
| Kernel, S3lvol, fresh templates, Cube node, NodePort bridge, Tool telemetry | live Kunpeng verified in the prior acceptance milestone |
| Session-local gateway, command-specific P90, policy-event timing, timing separation | source/unit evidence; targeted tests pass |
| Controller Service RBAC and normal Service/Job realization | live Kunpeng verified in this milestone |
| Managed c1 recording/replay and paper results | not yet run; real credential is required |

Next operator actions are deliberately narrow: publish a Worker image from
`386170a`, create the LLM Secret without committing values, run one real c1
record through the managed path, freeze the trace/KB/Tool hashes, run the
session-local replay equivalence gate, and retain all raw outputs. Only after
zero divergence, zero telemetry loss, exact-ID joins, correct outputs, and
zero leaks should c4/c8/c20 be considered.

The supported path is now:

`clawbox experiment -> API -> Run/Attempt/outbox -> SandboxTask v1alpha2 -> one ExperimentWorker Job -> one PolicyCoordinator per arm -> cubesandbox==0.7.0 -> one Runtime VM plus one Tool VM per Agent`.

Verified on the Kunpeng host:

- CubeSandbox `v0.7.0`, commit `d0081641c59822e4e5653b7462e914410b81910a`;
- Kubernetes `v1.35.7`, containerd `2.3.4`, native ARM64 KVM and cgroup v2;
- all nine Cube Helm tests passed;
- create/command/file/pause/connect/kill passed and pause reclaimed about 598 MiB;
- four concurrent Agents and resident/eager-reactive policy arms passed without leaks;
- SandboxTask vertical slice succeeded with immutable worker digest, concise resultRef, JSON/JSONL/CSV/Markdown, and zero remaining sandboxes;
- cancellation reached `cleaned`, removed the Job and sandboxes;
- controller restart retained exactly one deterministic Job;
- the complete current local unit suite passes (five environment tests skipped).

The installer now preserves Helm database templates, configures wildcard Cube
DNS correctly, and repairs result hostPath ownership for the non-root Worker.
Failed arm markers are never reused as completed work.

The paired OpenClaw implementation is present locally: the Runtime VM contains
OpenClaw, native ClawTune prediction/model proxying, and only authenticated
`cube_shell`; the Worker owns the node-routed ModelGateway and keeps the
upstream model credential outside Runtime; the Tool VM owns `/workspace`, command execution,
per-execution cgroups, and the eBPF guest collector. The Worker owns both
lifecycles, applies policy to the Tool VM, and persists exact-ID joined Runtime,
bridge, cgroup, and eBPF records. The complete local suite passes with five
environment skips, and the Tool bridge cross-compiles for Linux/ARM64.

The post-reboot reduced real paired acceptance is now verified on Kunpeng:

- custom guest kernel `sha256:f84e3fa28ae6...` boots and exposes kprobes;
- fresh immutable Runtime/Tool templates boot with the new kernel identity;
- Runtime reaches the authenticated fixed-port WorkerBridge through the
  node-routed NodePort adapter;
- Tool pause/resume preserves the workspace while Runtime remains resident;
- cgroup-v2 and native eBPF telemetry are valid with zero loss and exact
  execution-ID joins;
- bridge stress passes 141 requests with head-of-line isolation and no secrets;
- final owner audit reports zero sandboxes and zero NodePort Service leaks.

This is a reduced two-VM acceptance, not the full paper matrix. Before broader
runs, the managed measurement gate must pass real c1 trajectory recording and
session-local deterministic replay equivalence, command-specific prediction
provenance, logical model-step accounting, nonblocking request events,
non-oracle wait predictions, separated timing, exact telemetry joins, and zero
leaks. Remaining paper work is then to run the Runtime-to-Worker-to-Tool arms at
the target concurrency levels, compare real API inference with deterministic
replay, produce policy-arm statistics, and publish the final experiment report.
The live evidence and exact provenance are recorded in
`docs/kunpeng920-nodeport-acceptance.md`.
