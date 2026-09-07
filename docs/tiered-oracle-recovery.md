# Recover and resume the standalone tiered oracle study

ClawBox uses standalone CubeSandbox. Kubernetes is not a prerequisite, runtime,
or recovery path. The earlier [Kubernetes recovery attempt](history/tiered-oracle-kubernetes-recovery.md)
is historical evidence only. Do not execute its continuation commands.

The agreed study is a complete selected-rec-a c1 replay, c1/c4 tier and identity
gates, then all 13 baselines at c40 once each. Do not run c8/c60 or substitute
the small historical smoke trace. No formal c40 result has been established.

## Inspect the host

```bash
ssh weitianc@193.124.7.2
cd /path/to/ClawBox
python3 scripts/cube-host-doctor.py \
  --warm /data/cubelet/clawbox-tiered-20260907/warm \
  --output "$HOME/host-$(date -u +%Y%m%dT%H%M%SZ).json"
```

The doctor is read-only, records individual command failures, and refuses to
overwrite evidence. Exit 2 means prerequisites are missing. An inventory with
no blockers still does not certify replay, physical placement, or capacity.
It does not read credential files or print process command-line arguments.

Record the boot ID before and after any reboot. The earlier September 7 host
kernel suffered mount-namespace soft lockups (`mntput_no_expire`); do not confuse
these with the separately fixed guest DAX fault. Preserve the kernel journal
before recovery. An SSH disconnect alone does not prove a reboot.

```bash
cat /proc/sys/kernel/random/boot_id
journalctl -k -b --no-pager > "$HOME/kernel-before-recovery.log"
# Only if host recovery is required; enter sudo's password in your terminal:
sudo reboot
```

## Establish standalone services

Follow [CubeSandbox setup](cubesandbox-setup.md) using one versioned source and
release. Existing `/usr/local/services/cubetoolbox` files do not prove standalone
services are installed: the earlier deployment also wrote there. Inventory
current users of `/data/cubelet` and the toolbox before installation. Preserve
their configuration and data, and stop conflicting ClawBox/CubeSandbox services
before the standalone installer replaces binaries. Never run two Cubelets
against the same storage/network state.

Use actual systemd listeners to populate the protected machine environment;
historical 30030/30080 ports and old template IDs are not standalone defaults.
Verify template image digests and the frozen guest kernel against the new catalog.
Re-use build/image caches after checking provenance.

## Gates before c40

1. Create, execute, stop, and clean up one sandbox through standalone CubeSandbox.
2. Pass native Runtime-to-Tool SSH identity and endpoint refresh at c1 and c4.
3. Pass the full frozen rec-a replay, final task validation, and execution joins.
4. Prove direct LOCAL→WARM→LOCAL and LOCAL→WARM→COLD→LOCAL transitions,
   WARM overflow/LRU, restore/spill and response-ready races, and no owned leaks.
5. Prove experiment-scoped LOCAL enforcement, WARM capacity and actual NUMA
   placement, retained backing accounting, and the common COLD cache protocol.
6. Freeze resource settings, provenance, input hashes, measured storage needs,
   randomized policy order, and timeouts before comparing policy outcomes.

The complete contract and authoritative trace hash are in the
[handoff](AGENT-HANDOFF-tiered-oracle.md). Admission-only NUMA sampling is not a
hard memory limit. Mount options alone are not proof of physical placement.

## Keep evidence

Use unique attempt IDs and retain infrastructure failures and genuine policy
outcomes separately. Archive on Linux before SCP to preserve long filenames.
Compare the downloaded archive's SHA256 with `sha256sum` on Linux. Keep raw
requests, credentials, database backups, and deployment environments private;
publish only reviewed, redacted provenance and measured results.
