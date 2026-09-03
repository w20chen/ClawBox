# Final architecture migration status

The local checkout remains the source of truth. Before this migration, the
managed Run/Attempt/outbox persistence, replay parsing, validation, reporting,
and generic admission accounting were reusable. The active SandboxTask
controller and lifecycle code were not: they created separate Runtime and Tool
workloads and depended on Kata, direct Firecracker, SSH, TAP devices, and local
snapshot files.

The final path is deliberately narrower:

`clawbox experiment` -> managed Run/Attempt/outbox -> one SandboxTask -> one
ExperimentWorker Job -> official CubeSandbox Python SDK -> one Cube workspace
per logical session.

The worker owns matrix expansion, sequential arm execution, concurrent logical
sessions, policy decisions, host-memory sampling, validation, atomic results,
and cleanup. Kubernetes owns durable task state and the worker Job.
CubeSandbox owns every MicroVM operation. There is no public runtime or
transport selector.

Pinned runtime:

- CubeSandbox tag: `v0.7.0`
- CubeSandbox commit: `d0081641c59822e4e5653b7462e914410b81910a`
- Python SDK: `cubesandbox==0.7.0`
- target: native ARM64 KVM; PVM disabled

Host audit (2026-09-03): openEuler 24.03 LTS-SP1, aarch64, cgroup v2 and
`/dev/kvm` are present. The previous Firecracker thin pool and large crash
dumps were removed. Kubernetes was unavailable because kubelet could not use
the configured CRI v1 endpoint; deployment validation remains pending until
that host service is repaired.
