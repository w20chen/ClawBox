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
/home/weitianc/clawbox-tiered-study-20260907/five-c40-20260908-v4
```

Frozen execution source: `23b8419` at `/home/weitianc/ClawBox-experiment-five-c40-v4`. Do not delete or update that checkout while its supervisor runs. This attempt uses five recorded rounds, all 13 policies, c40, seed 1, and a common 1800-second per-policy deadline.

The older `/home/weitianc/ClawBox` checkout has uncommitted source changes. Do not reset or pull over them. For the new wrapper commands, use a separate current checkout as described in the installation guide. The documentation and command tests were run in `/home/weitianc/ClawBox-docs-check-20260908-v2`, without changing the active experiment.

From that checkout:

```bash
.venv/bin/python scripts/study-status.py \
  /home/weitianc/clawbox-tiered-study-20260907/five-c40-20260908-v4
```

## Verified results and open work

The first policy, `tool-static-time-oracle-reactive`, passed 40/40 sessions, exact execution-ID joins of 1.0, and zero telemetry loss. Its execution-source suite passed 332 tests.

In v3, `tool-p90-wait-reactive` timed out with 0/40 validated sessions. `tool-static-eager-reactive` reached 30/40 before its 1200-second deadline. A live thread dump during the next resident policy confirmed a lock cycle: admission held the lifecycle lock while waiting for memory, while tool completion needed that lock to release memory. The old supervisor was stopped and its owned VMs cleaned up; its logs and thread dump remain in the v3 directory.

Commit `23b8419` releases a finished tool's reservation before waiting for the lifecycle lock. Idle-state transitions remain serialized with admission. A regression test exercises this exact completion callback. The updated complete unit suite passed 324 tests, including a scheduling-independent concurrency test. The v4 experiment is validating the fix, starting with the previously blocked wait-reactive policy. Not all baseline results are certified yet, and this is not a completed full-trace paper experiment.

Earlier full-trace c1/c4 checks, physical snapshot-placement checks, and failed attempts remain under `/home/weitianc/clawbox-tiered-study-20260907`. Keep those research records.

## Recovery facts

Creation and tool admission previously shared a blocking queue. Existing sessions now take priority, and VM pairs are reserved together. Retained LOCAL cache can also block admission below the hard memory limit. A single asynchronous cgroup reclamation request lets admission recheck actual usage while the kernel works. Host-RSS periodic sampling reads only the discovered VM processes.

A fatal PCIe error caused a host reboot on September 8. The authorized cleanup removed the 269 GiB memory dump, but retained its diagnostic log. This was a hardware failure, not established as a ClawBox software fault.

After reboot, restore the temporary cgroup and tmpfs settings. Check disk space: CubeMaster can reject new VMs at its storage scheduling threshold even when RAM is available.
