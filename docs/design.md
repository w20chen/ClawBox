# Execution and measurement contracts

This reference defines behavior that implementations and experiment reports must
preserve. Configuration and commands are in the [user guide](guide.md); host setup
is in [installation](installation.md).

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
LLC misses. Full field and validation details live in the sibling ClawTune
`docs/pmu-profiling.md` and `contracts/pmu-profile.schema.json`.

## Reservations and physical memory

Reservations determine whether work may start; they do not resize guest RAM.
Lifetime capacity claims and incremental command reservations are distinct:
already-resident memory must not be counted again as an incremental allocation.
All compared policies use the configured emergency free-memory guard.

Guest command memory trains demand estimates. Host physical memory measures
density and reclamation. Converting a guest estimate into an expected host
increment requires calibration. P90 means the estimated 90th percentile of
demand, not a guarantee that every command fits. Keep prediction artifacts frozen
and independent of the test workload unless explicitly studying online learning.
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
| LOCAL | Memory where a VM executes, normally a selected NUMA node |
| WARM | Saved VM state in tmpfs on another NUMA node; the VM is not executing there |
| COLD | Saved VM state on SSD |

During LOCAL-to-WARM copying, source RAM and destination pages coexist and count
against their respective budgets. Restore copies state into independent LOCAL
guest RAM before retiring the WARM generation. WARM-to-COLD spill releases the
memory-backed copy. COLD restoration must distinguish actual device I/O from
page-cache hits. Logical snapshot bytes and physical device I/O are different
measurements.

If WARM is disabled or a VM snapshot cannot fit its total capacity, that snapshot
goes directly to COLD. This supports the SSD-only ablation without changing the
wait-based placement policy. Model transport invalidation occurs at Runtime
checkpointing, not before a Tool checkpoint that may fail.

LOCAL cgroup usage includes charged guest RAM, VM overhead, and retained cache.
Checkpoint/restore headroom is inside the configured LOCAL capacity. WARM
preallocation occurs outside that cgroup, with separate capacity accounting.
Requested cache reclamation is not credited as freed memory until measured.
Restore and spill operations must serialize ownership of each saved generation.

The current tiered presets require replay timing. One chooses least-recently-used
candidates under pressure, using actual remaining wait for filtering and
placement; another places state according to actual wait duration. Neither is an
online policy without future information. Non-tiered presets disable WARM in the
existing planner. Report effective capacities rather than assuming all policies
have equal total memory.

This is a single-host NUMA approximation of tiered storage. It does not measure a
CXL fabric, cross-host contention, ownership transfer, or failure recovery. Report
host topology and measured transfer costs; do not claim absolute multi-host
speedups from these measurements.

## Replay and evidence

Record and replay use the same agent configuration and initial guest environment.
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
