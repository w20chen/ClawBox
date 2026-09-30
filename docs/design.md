# Execution and measurement

This page describes what the supported experiment measures and the limits
of its single-host NUMA model. For commands and fields, see the
[configuration reference](guide.md); for host setup, see the
[step-by-step guide](self-service.md).

## Agent and tool VMs

Each session has an agent VM running OpenClaw and a tool VM containing
the mutable workspace and tool processes. The experiment worker controls
admission and the VM lifecycle through standalone CubeSandbox. It runs
tool commands over SSH. For each managed command, it:

1. Obtains a resource estimate from ClawTune.
2. Checks measured memory use, existing reservations, the new reservation,
   and the configured safety margin.
3. Restores the tool VM if needed and resolves its current SSH endpoint.
4. Verifies the endpoint and executes the command once.
5. Collects the result and telemetry, then releases the reservation.

An execution ID links admission, SSH execution, completion, and
measurements. A retry must not execute the same command twice. After
restoration, CubeSandbox may assign a different endpoint, so the worker
must resolve it again.

## CPU performance counters

The tool VM uses ClawTune's `perf_event_open` collector to measure
cycles, instructions, last-level cache (LLC) read accesses, and LLC
read misses for a command. Collection starts before the command is
released to run. The profile is stored with the command's resource
record. The agent and tool images use the same ClawTune collector
source.

Each active tool command uses one group of four counter descriptors.
The per-VM limit is `TOOL_MAX_CONCURRENCY`; the experiment worker
runs one such command per tool VM. A guest running ratio shows
multiplexing visible inside the guest, but cannot rule out contention
in the host's virtual PMU. Missing capabilities, unsupported events,
or low counter running time leave the PMU result unavailable or
degraded without changing the tool command's exit status.

On ARM64, the collector uses Linux last-level cache read events.
Generic cache-miss events and HiSilicon `hisi_l3c` uncore events
must not be reported as the command's LLC read misses. A profile is
usable for PMU-based analysis only when its execution ID matches,
all four events are supported, kernel coverage is present, and each
counter ran for 100% of its enabled time. Missing or degraded profiles
must not train PMU predictions; CPU and memory measurements can
still be used. Inspect `pmu-profile-*.json` and its coverage fields
on the target guest kernel before making an LLC claim. The sibling
ClawTune repository defines the detailed PMU schema.

## Memory admission

The supported policies reserve either a calibrated fixed amount of
extra memory for each tool command, a frozen per-command P50 estimate,
or that P50 estimate plus checkpointing while the agent waits for a
model response. See [supported policies](guide.md#supported-comparison-policies)
for their CLI names.

A reservation controls when work may start; it does not change the
guest's configured RAM. Configured VM capacity, already resident
memory, and extra command memory are different quantities.
Admission must not count resident pages again as new allocation.
All policies use the configured host available-memory safety floor.
Only checkpointing policies reserve additional capacity for
checkpoint and restore operations.

During creation of an agent/tool VM pair, admission reserves their
configured capacities until host sampling observes the new residents.
For later pairs, it also leaves room for one call from a session that
is already runnable: the fixed policy uses its configured command
reservation, and the predicted policies use the larger of the frozen
prediction maximum and the fixed reservation for non-command tools.
The first pair has no older session to protect. Applying full guest
capacity in place of these calibrated command reservations would
change the compared policies.

ClawTune measures additional command memory relative to the
environment immediately before the call. ClawBox rounds the selected
P50 estimate up to MiB for admission. Prediction is not a guarantee
that the command fits. Record fixed reservations for file operations
separately from per-command predictions.

## Checkpointing and storage

A checkpoint cannot interrupt an active tool command. While an agent
waits for a model response, the worker can checkpoint its agent and
tool VMs. The model gateway holds a pending response until the agent
VM is restored. The tool VM may remain checkpointed until its next
command. The worker coordinates response arrival, checkpointing, and
restoration so a completed response does not trigger a late checkpoint.

| Memory or storage location | Meaning |
| --- | --- |
| Compute-node memory | Running VM pages normally placed on the assigned NUMA node |
| Shared-pool memory for running VMs | A running VM whose memory allocation is rebound to the shared NUMA node under pressure |
| Shared-pool tmpfs | Checkpoint files held in memory on the shared NUMA node |
| Disk | Checkpoint files on an SSD or other configured disk filesystem |

While copying a VM from compute-node memory to the shared-pool tmpfs,
both source RAM and destination pages consume capacity. An incremental
checkpoint uses a base image and changed ranges; restore can fault
pages in on demand, and writes use copy-on-write. A running VM may
still reference its memory-backed checkpoint, which cannot be
spilled until those references are released. Report the time until
the VM is ready separately from the latency of its first tool
command, which may include later page faults.

In `tiered` storage, a checkpoint too large for the shared-pool
tmpfs goes to disk, and eligible older checkpoints may spill there.
A spill copies the full dependency chain before releasing its
memory-backed copy. Disk restore measurements must distinguish
device I/O from page-cache hits. `warm-only` storage has no disk
fallback; a capacity failure must be reported as a failure.
The selected `full-copy` or `incremental-cow` mechanism must match
what the run actually used.

Measured compute-node memory includes charged guest RAM, VM
overhead, and retained cache. The configured checkpoint/restore
margin is within the local capacity. The shared-pool tmpfs lies
outside the parent VM cgroup and allocates pages as needed.
Admission initially reserves configured VM RAM plus 256 MiB for a
checkpoint, then records its actual allocated size. Requested cache
reclamation is credited only after a new measurement confirms it.

## Two compute nodes on one host

Each compute node has its own capacity, low/high watermarks, and
admission decisions. The default configuration assigns NUMA 0 and 1
to compute and NUMA 2 to a shared pool; all IDs and capacities are
editable. A running VM using shared-pool memory first reserves its
configured capacity, not just its current resident set, to cover
later guest growth. These running-VM reservations and checkpoint
reservations share one pool-wide ledger.

The parent VM cgroup's `memory.max` covers the sum of local
capacities plus the global borrowing limit. It is not a separate
hard limit per NUMA node. Per-node `memory.numa_stat` measures
physical residency for admission and reporting. Memory charges
that cannot be attributed to a node are counted conservatively
for each node's admission decision but only once in host totals.
Changing a VM's binding does not immediately move every existing
page. See [host configuration](supernode.md) for placement and
result interpretation.

This uses the host's real NUMA distances. It does not model a
cross-host interconnect, its failure modes, or configurable network
bandwidth and latency. Report host topology and measured transfer
costs; do not treat these as absolute multi-host speedups.

## Replay and evidence

Replay requires the agent configuration and starting guest
environment to match those used when the trace was recorded. The
public CLI has no recording command. The model gateway supplies
responses in order and keeps recorded model wait separate from
delays caused by the memory policy. OpenClaw runs the tools
normally; actual outputs are retained rather than replaced by
recorded text.

Keep workload, starting workspace, templates, task assignment,
arrival pattern, random seed, and resource scope fixed when
comparing policies. Check completed sessions, task validation,
execution-ID joins, telemetry loss, duplicate commands, OOMs,
and VM cleanup before interpreting throughput or latency.
Report completion time, admission wait, host memory use,
prediction coverage/error, reclaimed bytes, and checkpoint/
restore costs. Keep failed or interrupted trials distinct from
completed measurements.
