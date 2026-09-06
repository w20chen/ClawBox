# Deployment files

The supported ClawBox experiment path is the standalone CubeSandbox Worker
described in [experiment operations](../docs/experiment-operations.md) and
[CubeSandbox setup](../docs/cubesandbox-setup.md).

`deploy/cubesandbox/` contains the active CubeSandbox integration assets:

- pinned semantic TCP endpoint, HostPort hairpin, template-provenance, and
  checkpoint phase-timing patches;
- the Kunpeng CubeSandbox values overlay;
- kprobe-capable guest-kernel metadata and configuration;
- source preparation helpers.

Other root-level YAML, service, containerd, and RuntimeClass files are retained
only to reproduce older Kubernetes/Kata experiments and tests. They are not a
second supported sandbox backend. New ClawBox runs must not launch Pods, Jobs,
SandboxTasks, or direct Firecracker VMs, and must not use NodePort, Redis, or
guest-IP endpoint fallbacks.
