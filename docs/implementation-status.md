# CubeSandbox migration status (2026-09-04)

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
