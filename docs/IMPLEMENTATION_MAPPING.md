# Implementation mapping (Phase 1-3)

| Existing component | New architecture component |
| --- | --- |
| ClawTune `packages/openclaw-plugin` | Runtime plugin; unchanged, calls Tenant Scheduler |
| ClawTune FastAPI sidecar contracts/correlation | Tenant Scheduler adapter and stable execution identity |
| `RuntimeToolResourceKB` | Tenant-private prediction overlay and snapshot |
| `ClauseResourceKB` / `LatticeTimeKB` | Preserved Phase 2 prediction sources; adapter is extensible to their richer outputs |
| `claw-launch` and cgroup-v2 support | Tool execution path; retained for Linux/Kubernetes evolution |
| cgroup/process/eBPF telemetry | Tool telemetry source; Phase 3 uses portable process telemetry in Docker |
| ClawBox Runtime/Tool deployments | Phase 4+ Kubernetes backend and Kata/Firecracker packaging |
| New `allocator` | Machine capacity, quota, transactional leases, fencing |
| New `controller` | Sticky workspace-to-tool lifecycle and Docker backend |
| New `node_agent` | Read-only dynamic host topology and PSI API |

No OpenClaw core file or ClawTune checkout is modified. The Scheduler dynamically imports the
adjacent/mounted ClawTune source and serializes the existing KB format.
