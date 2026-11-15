# Cross-tier snapshot v1 integration handoff

## Scope

This branch integrates the reviewed cross-tier incremental snapshot work onto
the team ClawBox baseline:

- ClawBox target: `441005e9c9eb88fca09ceb7795070f8a65af3d64`
- Delivery branch: `feat/cross-tier-snapshot-v1`
- Original ClawBox research head:
  `0ee476abb8606646e3f7a19c823ce0519d06e50a`
- Original CubeSandbox research head:
  `8dba7ed8d8414aeae88ba44e3230777290e12407`

The integration keeps the new main snapshot scheduling, base cache, peer
storage domain, and ARM VGIC work. It adds only the missing cross-tier pieces
and resolves overlapping lifecycle/VMM paths rather than replacing the newer
main implementation.

## Delivered behavior

ClawBox includes:

- managed cross-tier RAM layer metadata and reporting;
- compressed WARM accounting, expansion budgeting, retry/reuse, and storage
  observations across expansion stages;
- runtime page-materialization reservation;
- explicit crash rollback through the public lifecycle API;
- VMA-budget-aware lineage restore with proactive copy and `ENOMEM` fallback.

CubeSandbox includes, through the reviewed patch set:

- cross-tier COLD/WARM incremental inheritance and managed layer takeover;
- managed layer final-reference GC and safe old-layer reclamation;
- crash rollback record persistence and lifecycle;
- API, protobuf, Cubelet, SDK, and DB migration support;
- fail-closed crash rollback when runtime state cannot be determined.
- checkpoint phase writers `record` and `record_details` used by lineage
  restore/compression metrics.

The patchset is `deploy/cubesandbox/cross-tier-managed-lineage.patch`.

```text
SHA256 929f0d28d94e01c0e29d9a9c687c25978dd1503981d97f94197b96a3461d7fdc
```

## Source mapping

ClawBox research commits retained in the delivery:

- `dcc5f65` managed cross-tier snapshot layers;
- `1a0f775`, `f54e4bf`, `f7a9c7e` compressed snapshot accounting/hardening;
- `7325ab2`, `93a2af2`, `d0769ee` expansion budget/retry/overlap;
- `324bfd0` expansion-stage storage observations;
- `5a1cd5c` runtime page-materialization reservation;
- `fb4aa9c` automatic crash rollback lifecycle;
- `434e949` stable manifest self-allocation accounting.

CubeSandbox research commits retained:

- `ea5375e`, `f5243a4`, `9d46e2f`, `863bd29` managed lineage and GC;
- `3b9fb35`, `a907274`, `75e4a85`, `51f204f`, `648db50`, `4e69a84`,
  `a0ff8f0`, `8dba7ed` crash rollback and fail-closed runtime probing.
- `2c81e4a` supplies the `checkpoint_metrics::record/record_details`
  dependency used by the integrated VMM paths.

Research-only hooks not delivered:

- forced-COLD pressure boundary commits `7714e51` and `0ee476a`;
- forced shim/component pin experiments;
- lineage publish failpoints/hold and their test-only controls.

## Build

Use a new empty source directory; the preparation script refuses to overwrite
an unrelated checkout.

```bash
export CUBE_SOURCE_DIR=/path/to/new/empty/cubesandbox-source
bash deploy/cubesandbox/prepare-semantic-source.sh
```

The script clones official CubeSandbox `v0.7.0`, applies the ordered ClawBox
patch set, verifies the pinned upstream pagemap fix, applies cross-tier last,
and writes `.clawbox-patchset.sha256`.

Expected patchset identity:

```text
v0.7.0 123760db7ec5de6b131602f5f772305f3ef66a31ac145bd337645a6b7fcab60e
```

Then build a standalone release without replacing live services:

```bash
bash scripts/build-cubesandbox-release.sh \
  --assets /path/to/verified/assets \
  --source "$CUBE_SOURCE_DIR"
```

Do not copy x86 binaries or local template IDs to ARM64. Rebuild Cubelet,
CubeMaster, CubeAPI, shim, cube-runtime, the native Zstd page helper, and the
Python SDK for the target architecture.

## Database migrations

Apply the migration matching the configured database:

- `CubeDB/migrate/migrations/mysql/20261114160000_crash_rollback_record.sql`
- `CubeDB/migrate/migrations/postgres/20261114160000_crash_rollback_record.sql`

Apply it before enabling crash rollback. A schema rollback is not implicit:
new records and snapshot metadata may remain even after binaries are replaced.

## Runtime service configuration

Use systemd drop-ins for runtime switches. Shell exports from an experiment
session are not sufficient because Cubelet launches the shim/VMM from the
Cubelet service environment.

Cubelet service environment required for cross-tier incremental snapshots:

```text
CLAWBOX_CROSS_TIER_LINEAGE=1
CLAWBOX_KVM_DIRTY_TRACKING=1
CUBE_RESTORE_PRIVATE_COPY=0
CLAWBOX_WARM_SNAPSHOT_ROOT=/absolute/path/to/warm
CLAWBOX_COLD_SNAPSHOT_ROOT=/absolute/path/to/cold
```

Enable crash rollback only after the matching migration has completed:

```text
CLAWBOX_CRASH_ROLLBACK=1
```

Set this switch on both CubeMaster and Cubelet. CubeMaster gates the
transaction/rollback API; Cubelet gates the sealed pause package and rollback
manifest handling. Keep both services on the same value during rollout.

The shim and VMM inherit the Cubelet environment. Confirm the live process,
not only the unit file:

```bash
tr '\0' '\n' < /proc/<shim-real-pid>/environ |
  grep -E 'CLAWBOX_CROSS_TIER_LINEAGE|CLAWBOX_KVM_DIRTY_TRACKING|CUBE_RESTORE_PRIVATE_COPY|CUBE_RESTORE_MAP_RESERVE|CUBE_RESTORE_LINEAGE_COPY'
```

Build-time variables such as `CUBE_SOURCE_DIR` are not runtime controls.

Optional restore protection:

```text
CUBE_RESTORE_MAP_RESERVE=4096
CUBE_RESTORE_LINEAGE_COPY=0
```

- `CUBE_RESTORE_MAP_RESERVE` reserves additional VMAs before deciding whether
  to mmap lineage layers or copy them into the anonymous guest mapping.
- Setting `CUBE_RESTORE_LINEAGE_COPY=1` forces copy mode for qualification.
- `ENOMEM` during lineage mmap automatically switches the remaining ranges to
  pread copy.

Cross-tier is opt-in. Do not include experimental forced-COLD settings in a
production unit file.

## Storage roots and environment propagation

- WARM and COLD roots must be absolute paths, exist before startup, and be
  writable by Cubelet and the shim/VMM processes that create packages.
- WARM normally stays on the configured tmpfs; COLD normally stays on the
  block-backed storage domain. Do not silently place both on one filesystem
  while claiming cross-filesystem coverage.
- Managed RAM layers are stored below `<root>/layers`; snapshot package roots
  remain below `<root>/<sandbox-id>`.
- `CLAWBOX_WARM_SNAPSHOT_ROOT` and `CLAWBOX_COLD_SNAPSHOT_ROOT` must propagate
  through Cubelet to the shim. If they are absent, managed-root inference only
  accepts the documented `<tier-root>/<sandbox>/<memory>.layers` layout and
  fails closed otherwise.
- Apply the DB migration before setting the crash-rollback switch. Restart
  Cubelet after changing inherited switches/roots, then verify process
  environments before starting workloads.

## VMA limits

Restore chooses between per-range `mmap` and copying into the existing
anonymous guest mapping.

- The estimate is `maps_before + effective_ranges + CUBE_RESTORE_MAP_RESERVE`.
- The default reserve is `4096` VMAs.
- If the estimate reaches `/proc/sys/vm/max_map_count`, copy mode is selected
  proactively.
- If mmap still returns `ENOMEM`, all remaining ranges switch to pread copy.
- `CUBE_RESTORE_LINEAGE_COPY=1` forces copy mode for qualification.
- Record `memory.restore_lineage_budget_decision`,
  `memory.restore_lineage_maps_before`, `memory.restore_lineage_maps`, and
  `memory.restore_lineage_copy`.

The 4096 reserve is a safety default, not a proof that 65k effective ranges
fit on every kernel. ARM64 must be evaluated with its actual page size and
`vm.max_map_count`. Do not increase kernel sysctl limits or claim ARM capacity
from x86 results without a separate qualification.

Optional checkpoint telemetry:

```text
CLAWBOX_CHECKPOINT_LOG=/absolute/path/to/checkpoint-metrics.jsonl
CLAWBOX_CHECKPOINT_RANGE_LOG=1
```

`record` and `record_details` are no-ops unless the aggregate log path is set.
Per-range logging is independently gated because it adds substantial I/O.

## Minimum acceptance

1. Start a Tool/Runtime pair with cross-tier enabled.
2. Checkpoint both VMs into COLD and restore them.
3. Complete a tool operation and create the next WARM generation.
4. Assert the WARM successor reports `full_base=false`.
5. Repeat for at least two Tool/Runtime generations.
6. Run the case canonical command and compare guest/business state before and
   after restore.
7. Verify no duplicate runtime exists and cleanup removes sandboxes, shims,
   temporary markers, and capacity reservations.
8. Record `maps_before`, effective ranges, chosen mmap/copy mode, fallback
   reason, and maps after restore on ARM64.

The reviewed x86 T1 passed with 38/38 model responses, 121/121 tool
completions, canonical `2 passed`, and Tool/Runtime COLD-to-WARM chains whose
successors were incremental. This is x86 evidence only.

## Validation performed for this review

- Python compile of `clawbox` passed.
- Cross-tier/compression/pool/reporting tests: 51 passed.
- Crash rollback plus remaining `test_cube`/memory selection: passed after
  adapting `prepare_cgroup` to the latest lifecycle API.
- New-main snapshot scheduling, peer, and base-cache selection: passed except
  one upstream test whose 85% disk check depends on the host filesystem.
- Two OpenClaw tests could not run as the non-root review user because they
  inspect root-only `/run/clawbox/base-cache.lock`.
- `hypervisor` KVM lib/bins check passed with `cargo check`.
- `CubeAPI` locked check passed with `cargo check`.
- CubeMaster Go tests passed for `pkg/crashrollback`, `pkg/pausesnap`, and
  `pkg/service/sandbox`.
- Cubelet managed-layer, incremental-memory, reclaim, pause, and crash
  rollback tests passed.
- The complete Cubelet package also ran, but one pre-existing concurrent probe
  test failed on Ping timeout (66.7% success versus its 80% threshold); this
  is unrelated to the delivered paths.
- CubeSandbox patch chain was prepared from zero and prepared a second time
  successfully; both runs ended with `git diff --check` clean.

## Known limits

- With cross-tier enabled, WARM-to-COLD retains durable shared layers and
  materializes only inherited tmpfs ranges on SSD. Missing parents or invalid
  dirty-tracking history still require a full base. Validate zero updates,
  durable-layer inode sharing, and restore after removing the WARM parent.
- VMA pressure is bounded proactively, but the mmap/copy decision must be
  measured on ARM64 using the target kernel and page size.
- S1 forced/low-budget copy, ARM64 qualification, low-concurrency smoke, and
  any c24/c32 rerun remain separate acceptance work.
- Replay/tool divergence observed in x86 trace runs still requires test-side
  attribution.

## Rollback

1. Stop new experiments and drain active sandboxes.
2. Disable the cross-tier switch.
3. Roll back binaries only with the matching database and artifact inventory.
4. Preserve COLD/WARM layers, crash-rollback rows, and pending markers until a
   compatible reader has inventoried them.
5. Do not delete snapshot data or destructively downgrade the schema merely
   because the switch is off. The old binaries may not understand new
   sidecars, managed layer identities, or rollback records.

## Team workflow

- Keep `main` read-only; integrate and review only on
  `feat/cross-tier-snapshot-v1`.
- Anchor reviews to the target SHA and patchset digest above.
- Change the patch only through an ordered, reviewable diff; do not export a
  full research-branch tree.
- Store large logs, images, and result sets outside git and reference their
  team-accessible location.
- Push/PR/merge and deployment require explicit team approval after this
  handoff is reviewed.
