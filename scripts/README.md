# Script index

The supported user entry point is `.venv/bin/python -m clawbox.cli`. Most users
need only the following helpers:

| Script | Purpose |
| --- | --- |
| `register-cube-template.py` | Build an immutable Runtime or Tool template, including per-VM writable-disk size. |
| `audit-cube-sandboxes.py` | Read-only sandbox/template inventory before and after a run. |
| `validate-cubesandbox-tcp-endpoints.py` | c1/c4/c8 native SSH identity, lifecycle, epoch, telemetry, and leak gate. |
| `smoke-cubesandbox-agent-pair.py` | Lower-level Runtime/Tool pair smoke. |
| `probe-cubesandbox-memory-reclaim.py` | Measure host physical-memory change around Cube pause/restore. |
| `probe-cubesandbox-network-topology.py` | Diagnose Cube networking without changing the Worker endpoint contract. |
| `diagnose-cube-kprobes.py` | Verify Tool guest kprobe support. |
| `train-p90-from-runs.py` | Build the immutable ClawTune P90 artifact from separate recording data. |
| `audit-experiment-matrices.py` | Validate checked-in experiment matrices. |
| `evidence-manifest.py` | Produce artifact provenance/evidence manifests. |

Image, kernel, and CubeSandbox preparation helpers are documented by
`docs/cubesandbox-setup.md` and `docs/experiment-operations.md`. In particular,
`prepare-semantic-source.sh`, `install-kprobe-kernel-kunpeng920.sh`, and
`rebuild-swe-rebench-tool-overlay.sh` are provisioning tools, not experiment
launchers.

Other scripts remain only where tests, artifact compatibility, or historical
Kunpeng recovery still reference them. They are not alternative ClawBox
sandbox backends and must not be used to claim native managed results. Do not
choose old Kubernetes, direct-Firecracker, Pod, or `SandboxTask` launchers for
new experiments.
