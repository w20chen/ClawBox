# Control-plane compatibility note

The original ClawBox scheduler/allocator/controller services remain available for non-benchmark API compatibility, but they are not the SWE-ReBench execution path. The production benchmark path is the `SandboxTask` CRD and Cell controller described in `CONCURRENT_KATA_SWE.md`.

This distinction is intentional:

- legacy API objects cannot launch benchmark Jobs directly;
- the benchmark launcher submits only immutable ARM64 image digests;
- the Cell controller is the sole owner of Tool and Runtime Pods;
- the only supported production RuntimeClass is `kata-fc-arm64`;
- central trace/result ingestion replaces shared trace volumes.

Do not wire the old single-Pod Kubernetes backend into SWE-ReBench. New orchestration features should extend `CellSizer`, `NodeCapacityProvider`, `PlacementPolicy`, or the `SandboxTask` spec/status contract.
