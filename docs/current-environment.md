# Current kunpeng environment

This snapshot was checked on September 8, 2026. Use the status command for live progress.

## Deployment

- Access: `ssh kunpeng`, user `weitianc`.
- ARM64 Kunpeng, openEuler, approximately 2 TiB RAM.
- Host kernel: `6.6.0-72.0.0.76.oe2403sp1.aarch64`.
- Backend: standalone CubeSandbox v0.7.0 with ClawBox patches.
- Backend source: `/home/weitianc/CubeSandbox-standalone-20260907`, commit `48b3268`.
- Host ClawTune: `/home/weitianc/ClawTune`, checked revision `76eab6f`.
- Machine settings: `/home/weitianc/.config/clawbox/machine.env`.
- API port 3000; CubeMaster 8089; Cubelet 9998; egress 9091.
- Services: `cube-sandbox-*.service`. No Kubernetes deployment is maintained.

The guest kernel is distinct from the host kernel. Its files are under `/usr/local/services/cubetoolbox/cube-kernel-scf`, component `sha256-5b59ed694175`. Transfer it with the working guest images.

## Experiment settings

| Item | Setting |
| --- | --- |
| Runtime VM | 2 vCPU, 2 GiB |
| Tool VM | 2 vCPU, 4 GiB |
| LOCAL | 160 GiB, NUMA0, CPUs 0–79, no swap |
| WARM | 64 GiB tmpfs, NUMA1, no swap |
| COLD | SSD snapshots |
| Load | 40 agents per baseline, 13 policies |
| Current workload | Five recorded rounds and an explicit stop |

LOCAL is `/sys/fs/cgroup/cube_sandbox/sandbox`. WARM and COLD are `/data/cubelet/clawbox-tiered-20260907/warm` and `/data/cubelet/clawbox-tiered-20260907/cold`. The 8 GiB operation headroom is inside LOCAL.

Runtime template: `tpl-c947c1efb94446198000eefb`. Tool template: `tpl-f623b38f249f485cafc91478`. IDs are specific to this deployment.

Active output:

```text
/home/weitianc/clawbox-tiered-study-20260907/five-c40-20260908-v3
```

Frozen execution source: `3ac8fdd` at `/home/weitianc/ClawBox-experiment-five-c40-v3`. Do not delete or update that checkout while its supervisor runs.

The older `/home/weitianc/ClawBox` checkout has uncommitted source changes. Do not reset or pull over them. For the new wrapper commands, use a separate current checkout as described in the installation guide. The documentation and command tests were run in `/home/weitianc/ClawBox-docs-check-20260908-v2`, without changing the active experiment.

From that checkout:

```bash
.venv/bin/python scripts/study-status.py \
  /home/weitianc/clawbox-tiered-study-20260907/five-c40-20260908-v3
```

## Verified results and open work

The first policy, `tool-static-time-oracle-reactive`, passed 40/40 sessions, exact execution-ID joins of 1.0, and zero telemetry loss. Its execution-source suite passed 332 tests.

At 07:38 UTC, the second policy, `tool-p90-wait-reactive`, had timed out with 0/40 validated sessions. The third policy was still making progress. The remaining results are not certified. This is not a completed full-trace paper experiment.

Earlier full-trace c1/c4 checks, physical snapshot-placement checks, and failed attempts remain under `/home/weitianc/clawbox-tiered-study-20260907`. Keep those research records.

## Recovery facts

Creation and tool admission previously shared a blocking queue. Existing sessions now take priority, and VM pairs are reserved together. Retained LOCAL cache can also block admission below the hard memory limit. A single asynchronous cgroup reclamation request lets admission recheck actual usage while the kernel works. Host-RSS periodic sampling reads only the discovered VM processes.

A fatal PCIe error caused a host reboot on September 8. The authorized cleanup removed the 269 GiB memory dump, but retained its diagnostic log. This was a hardware failure, not established as a ClawBox software fault.

After reboot, restore the temporary cgroup and tmpfs settings. Check disk space: CubeMaster can reject new VMs at its storage scheduling threshold even when RAM is available.
