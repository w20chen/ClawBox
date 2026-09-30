# Execution and measurement contracts

This reference defines behavior that implementations and experiment reports must
preserve. Configuration and commands are in the [command reference](guide.md);
host setup is in [the self-service workflow](self-service.md) and
[installation](installation.md).

## Agent and tool isolation

Each session owns a Runtime VM and a Tool VM. OpenClaw runs in Runtime; the mutable
workspace and tool processes reside in Tool. The worker controls admission and
VM lifecycle through standalone CubeSandbox. Tool commands use native SSH.

For a managed command, the ordering is:

1. Identify the command and obtain its resource estimate from ClawTune.
2. Check observed host usage, existing reservations, the new reservation, and safety headroom.
3. Restore Tool if necessary, resolve its current SSH endpoint, and verify identity.
4. Start SSH and execute the command once.
5. Collect completion and telemetry, then release the command reservation.

Use one execution ID across admission, SSH, completion, and measurements. Retries
must not duplicate execution. After restoration, obtain a new endpoint from
CubeSandbox; cached host/port values are not authoritative.

### Tool-level PMU scope

The Tool bridge arms ClawTune's shared `perf_event_open` collector against the
guest-local gated root PID before releasing the payload. It records cycles,
instructions, LLC read accesses, and LLC read misses in counting mode and then
embeds `pmu_profile_v1` in the execution's cgroup resource artifact. There is no
ClawBox fork of the collector: Cube images copy the implementation directly
from the sibling ClawTune build context.

Each active Tool uses one four-FD inherited event group, independent of guest
vCPU count. `TOOL_MAX_CONCURRENCY` is also the per-VM PMU active-group ceiling;
the experiment worker uses one. Multiple Runtime/Tool VM pairs are bounded by
the host's existing session/VM admission. Guest running ratios expose
multiplexing visible to the guest, but do not prove absence of host-side vPMU
contention. Unsupported events, absent vPMU/capabilities, budget exhaustion,
and low running ratios produce explicit unavailable/partial/multiplexed
coverage and never alter Tool exit status.

ARM64 uses Linux's `PERF_TYPE_HW_CACHE` last-level read mappings. Generic cache
misses and HiSilicon `hisi_l3c` uncore counts are never relabeled as task-level
LLC misses. A PMU profile is usable only when the execution ID matches, the four requested
events are supported, coverage is complete and each running ratio is 100%.
Missing or degraded profiles must not fail the Tool command or train PMU targets;
CPU and memory accounting remain available. Inspect retained `pmu-profile-*.json`
artifacts and coverage status on the target guest kernel before using LLC results.
Full field details live in the sibling ClawTune `docs/pmu-profiling.md` and
`contracts/pmu-profile.schema.json`.

## Reservations and physical memory

The public ablation has three versions. A is calibrated fixed tool-memory
overcommit (`tool-static-resident`); A+B replaces the fixed estimate with a
command-specific P50 (`tool-p50-resident`); A+B+C adds pressure-triggered,
wait-aware WARM checkpoint and reactive restore (`tool-p50-wait-reactive`). Other
catalog recipes are retained only as deprecated research history.

Reservations determine whether work may start; they do not resize guest RAM.
Lifetime capacity claims and incremental command reservations are distinct:
already-resident memory must not be counted again as an incremental allocation.
All compared policies use the configured emergency free-memory guard.
The checkpoint/restore operation headroom is charged only to snapshot policies;
resident policies never perform those operations and retain the full LOCAL budget.
While a Runtime/Tool pair is created, its configured VM capacities are reserved
until physical sampling reflects the new residents. The create gate also leaves
room for one call from an already runnable session under that arm's admission
policy: full capacity for
`tool_full`, the configured fixed amount for `tool_static`, and the larger of
the frozen artifact maximum or the filesystem fixed amount for frozen predicted
admission. It must not silently apply full-capacity headroom to a calibrated
static or frozen predicted arm. The first pair has no older session to protect,
so it does not reserve this extra call headroom.

ClawTune measures memory above the environment's pre-call baseline and predicts
its peak for each Tool call. ClawBox reserves the selected P50 extra-memory
estimate, rounded up to MiB. Host physical memory measures density and
reclamation; the estimate does not guarantee every command fits.
Record static fallbacks for file operations separately from command predictions.

## Checkpoint and restore

A checkpoint must not interrupt active Tool SSH. In managed agent experiments,
both VMs may be saved while the model request is outstanding. The model gateway
retains the pending response until Runtime is restored. Tool may remain saved
until its next command. Early restoration applies to Runtime; Tool restoration
still occurs at command admission.

The response-ready event, delayed checkpoint, and restore request must be
serialized so that a completed response cannot cause a late checkpoint. A timed
out admission must not hold locks needed by command completion to release memory.

## Three storage locations

| Name in configuration/results | Physical meaning |
| --- | --- |
| LOCAL | Normal live-VM memory on the selected NUMA node |
| SHARED-LIVE | A running VM rebound to the shared NUMA node as the hard-watermark OOM safety path |
| WARM | Saved, non-executing VM state in tmpfs on the shared NUMA node |
| COLD | Saved VM state on SSD |

During LOCAL-to-WARM copying, source RAM and destination pages coexist and count
against their respective budgets. Incremental restore maps the immutable base
and delta ranges privately and loads pages on demand; writes use copy-on-write.
The running VM still references its WARM generation, which remains charged to
WARM and cannot be spilled until those references are retired. Restore-ready
latency excludes subsequent page faults and must be reported alongside first-tool
latency. WARM-to-COLD spill copies the complete base/delta dependency chain and releases the
memory-backed copy. COLD restoration must distinguish actual device I/O from
page-cache hits. Logical snapshot bytes and physical device I/O are different
measurements.

In `tiered` storage, a VM snapshot too large for WARM goes to COLD, and
eligible older WARM generations may spill there. In `warm-only` storage, a
capacity failure propagates; there is no disk fallback. Model transport
invalidation occurs at Runtime checkpointing, not before a Tool checkpoint
that may fail.

LOCAL cgroup usage includes charged guest RAM, VM overhead, and retained cache.
Checkpoint/restore headroom is inside the configured LOCAL capacity. WARM
preallocation occurs outside that cgroup, with separate capacity accounting;
incremental checkpoints allocate only the written ranges. Admission still reserves
the VM RAM size plus 256 MiB, then commits actual allocated layer bytes.
Requested cache reclamation is not credited as freed memory until measured.
Restore and spill operations must serialize ownership of each saved generation.

Each compute node has its own LOCAL capacity and LOW/HIGH watermarks. Sessions
bind to a configured node; admission and pressure decisions use that node's
physical usage and reservations. The default host profile assigns NUMA 0 and 1
to compute and NUMA 2 to a single SHARED-LIVE/WARM pool, but these IDs and
capacities are editable. Live borrow reserves the VM's configured capacity
before its memory binding changes, not merely its current RSS, so later guest
growth is covered. The global live-borrow cap is the configured fraction of
the shared pool; live and WARM reservations share one ledger. During a
checkpoint, borrowed source RAM and destination pages coexist and both count.

The parent VM cgroup's `memory.max` covers the sum of LOCAL capacities plus
the global live-borrow cap. It is not an independent hard limit for each NUMA
node. Per-node `memory.numa_stat` drives the LOCAL control state and reports
physical residency. The controller accounts unattributed kernel charge
conservatively for per-node admission but counts it once in host totals.
Binding a VM does not instantly migrate every existing physical page.
See [supernode.md](supernode.md) for placement and report interpretation.

The supported wait-aware policy uses model-response waiting periods to select
safe checkpoint candidates under pressure. Replay supplies the observed wait
timing. Report the effective LOCAL and shared capacities for every run.

This is a single-host NUMA approximation of a supernode and tiered storage. It does not measure a
CXL fabric, cross-host contention, ownership transfer, or failure recovery. Report
host topology and measured transfer costs; do not claim absolute multi-host
speedups from these measurements.

## Replay and evidence

Replay requires the same agent configuration and initial guest environment used
when recording the trace. The public CLI has no record command.
The gateway supplies model responses in order and preserves recorded model wait
separately from policy-induced response-release delay. OpenClaw executes tools
normally. Actual tool outputs are retained, not compared with recorded text or
rewritten using task-specific rules.

Keep workload, clean workspace, templates, task assignment, arrival schedule,
seed, and resource scope fixed when comparing policies. Report actual completed
sessions, task validation, model-step delivery, execution-ID joins, telemetry loss,
duplicate commands, OOMs, and VM leaks before performance metrics. Include
throughput, completion time, admission wait, host mean/peak memory and memory
integral, prediction error/fallback rate, reclaimed bytes, and transition costs.
Keep failed and interrupted attempts separate from completed measurements.
