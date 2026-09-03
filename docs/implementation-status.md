# CubeSandbox migration status (2026-09-03)

The supported path is now:

`clawbox experiment -> API -> Run/Attempt/outbox -> SandboxTask v1alpha2 -> one ExperimentWorker Job -> one PolicyCoordinator per arm -> cubesandbox==0.7.0 -> one ARM64 workspace per Agent`.

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

Remaining limitation: `agent.driver=openclaw` still needs a trusted Worker-side
OpenClaw tool adapter that routes tools to the Cube command executor. Replay is
fully exercised; OpenClaw acceptance must not be claimed yet. Legacy runtime
modules remain in the source tree for later deletion, but their console entry
points, tests, deployment permissions, and README path have been retired.
