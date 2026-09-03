# CubeSandbox migration status (2026-09-03)

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
OpenClaw, native ClawTune, model access, and only authenticated `cube_shell`;
the Tool VM owns `/workspace` and command execution. The Worker owns both
lifecycles and applies policy to the Tool VM. Real paired OpenClaw acceptance,
Tool-VM cgroup/eBPF collection, final image digests, and the post-refactor live
Kubernetes matrix are not yet verified and must not be claimed as passing.
