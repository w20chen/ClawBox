# Execution and measurement model

This page defines the semantics needed to interpret an experiment. See the
[experiment guide](guide.md) for commands and configuration.

## Session execution

Each session owns an agent VM and a tool VM. OpenClaw runs in the agent VM; the
tool VM contains the mutable workspace and executes commands over SSH. Templates
may repeat, but live VM state, trace position, ownership, and workspaces are not
shared between sessions.

For an admitted tool command, the worker:

1. Chooses a fixed reservation or a frozen ClawTune prediction when admission
   is enabled.
2. Checks measured memory, outstanding growth claims, the new reservation, and
   safety margins.
3. Restores the tool VM when necessary and resolves its current endpoint.
4. Verifies identity, executes the command once, and collects telemetry.
5. Releases the command reservation after completion.

An execution ID joins admission, SSH execution, tool progress, completion, and
measurements. Restore may change the endpoint. Retries must not execute the same
command twice.

## Capacity and admission

These quantities are deliberately separate:

- Configured VM memory is guest-visible capacity.
- Host cgroup charge is observed physical consumption plus host overhead.
- A prediction estimates command growth; it is not a limit.
- An admission reservation represents growth not yet visible in measurements.
- `memory.max` is an enforced cgroup limit, not an expected working set.

VM creation and restore reserve configured capacity until measurements reflect
the growth. Tool reservations cover calibrated additional host growth and must
not count resident pages again. Unknown predictions or missing samples remain
unavailable; they are never treated as zero.

Every compute domain enforces its LOCAL budget, while one atomic ledger covers
shared-pool reservations. A VM using shared memory reserves its configured
capacity so later guest growth remains covered. Snapshot reservations and live
borrowing consume the same shared ledger.

## Runtime pressure control

Runtime control is independent of tool admission and idle checkpointing. It
samples the host VM cgroup, CPU pressure, command progress, and release history.
Memory protection and CPU release boost act only when their configured evidence
and contention conditions hold. They do not infer host release from guest command
completion or from shared-memory residency.

CPU boost changes cgroup weight, not vCPU count or affinity. Memory protection
affects host reclaim priority; it does not pin a guest process's working set.
Missing required PSI interfaces fail qualification instead of silently disabling
the mechanism. All controls are reset after completion, timeout, loss of pressure,
or VM replacement.

## Placement and NUMA

A managed session assigns both VMs to one compute node. Placement may be
round-robin, explicit, or load-aware. Load-aware placement combines measured CPU
use, scheduling pressure when available, offered vCPU commitments, memory
residency, and outstanding claims. It does not use memory bandwidth as an input.

CPU lists restrict where vCPUs may run; they do not reserve cores or change the
template vCPU quota. The optional `c2` policy may temporarily narrow an eligible
tool call to one last-level-cache (LLC) cluster, but it restores the full NUMA CPU
list when the call ends or CPU pressure requires expansion.

Changing `cpuset.mems` requests a new allocation policy and may migrate pages,
but it does not prove complete migration. Physical residency comes from cgroup
`memory.numa_stat`. Unattributed memory is counted conservatively for admission
and only once in host totals.

This is a single-host NUMA model. It uses the host's real distances and does not
simulate a cross-host fabric, remote failures, or configurable link bandwidth.

## Checkpointing and the shared pool

Checkpointing never interrupts an active tool command. During a real model wait,
the worker may checkpoint the tool VM or both VMs. A pending replay response is
held until the agent VM is restored; the tool VM may remain checkpointed until
the next command.

| Location | Meaning |
| --- | --- |
| Compute-node memory | Running VM pages normally allocated on its assigned NUMA node |
| Shared NUMA memory | Pages of a running VM temporarily rebound under pressure |
| WARM tmpfs | In-memory checkpoint files allocated on the shared node |
| COLD storage | Checkpoint files on the configured disk filesystem |

During checkpoint copy, source RAM and destination pages coexist. WARM capacity
is charged by actual allocated snapshot bytes after an initial conservative
reservation. `warm-only` has no disk fallback. `tiered` may place a new snapshot
on disk or spill an eligible WARM generation.

Incremental CoW snapshots may reference an immutable template RAM base. A logical
base reference is not proof of physical sharing. Template-aware placement is
qualified only when two VMMs expose the same large file-backed mapping with
supporting `Shared_Clean` and PSS evidence. When aggregating storage, deduplicate
layer device/inode identities instead of summing every lineage's referenced
bytes.

## PMU measurements

The Tool VM's ClawTune collector records cycles, instructions, LLC read accesses,
and LLC read misses for each command. Collection starts before execution is
released. A PMU profile is usable only when its execution ID matches, all four
events are supported, kernel coverage is present, and every counter ran for 100%
of its enabled time.

On ARM64, use Linux last-level-cache read events for command-level LLC analysis.
Generic cache misses and HiSilicon `hisi_l3c` uncore events are different
measurements. Missing or multiplexed PMU data may still leave CPU and memory
measurements usable, but it must not train an LLC model.

## Evidence and comparisons

Replay preserves recorded model waits while executing tools normally. Keep the
workload, starting workspace, templates, case assignment, arrival schedule,
random seed, hardware scope, and evidence fixed across policy arms.

Before claiming a performance improvement, verify completion counts, final task
validation, execution-ID joins, telemetry coverage, OOMs, timeouts, cleanup, and
arm order. Report throughput and completion latency with repeat uncertainty.
Admission wait, tool time, memory, placement, checkpoint cost, and PMU records
are explanatory evidence, not substitutes for the end-to-end result.

Memory samples split the admission queue by `create`, `restore`, and
`tool_admission`, and report unique queued tasks and their resident VMs separately.
The queue excludes tasks still waiting to enter resource preparation; reconstruct
that queue from the `sandbox_prepare_gate_*` events. Prepared sessions wait for
actual creation in `sandbox_create_gate_wait_started`, `sandbox_create_gate_acquired`,
and `sandbox_create_gate_released`. Both stages are independently bounded by
`execution.sandbox_create_concurrency`; a memory-blocked preparation holds no
create slot. A task owns two VMs, but may have zero,
one, or two resident while queued. Last-observed blockers and oldest wait age
describe outstanding waits, including waits that never finish.

When `execution.dependency_install_concurrency` is set, package downloads have a
separate arm-wide queue. `dependency_install_gate_wait_started`,
`dependency_install_gate_acquired`, and `dependency_install_gate_released` mark
each transition. The periodic queue activity snapshot reports waiting and active
operations/tasks plus the oldest wait. A permit remains active until the Tool's
SSH command has been reaped, not merely until memory admission succeeds.

`tool_admitted.control_total_seconds` covers the worker admission callback,
including any dependency-install wait, lifecycle-lock wait, memory admission,
restore, routing, and bookkeeping.
`admission_blocked_seconds` covers memory admission only. Neither includes the
HTTP transport before the callback; use the guest/control journal for that.

Startup `native_ssh_warmup_finished` records submission, worker start and worker
finish on the host monotonic clock, plus each attempt's command duration, exit
status or exception type, identity match, and retry sleep. It is emitted even
when warmup fails. Startup `native_ssh_identity_ready.service_seconds` spans
submission to join observation; use its separate queue, execution, and blocking
join fields for attribution. All durations are seconds.

Create/restore lifecycle `stage_seconds` includes `pin_local_s` and its
`pin_local_vm_lock_wait_seconds`, `pin_local_discovery_seconds`, and
`pin_local_placement_service_seconds` components. Discovery is included in
placement service. Same-VM operations serialize; unrelated VM placement checks
do not wait for a borrower's migration or reclaim to finish.

`tool_admitted.memory_baseline_timing` separates the admission-condition wait
and hold during claim lookup, the kernel memory sample, and the wait and hold
when publishing that sample. It also records total wall and calling-thread CPU
seconds and whether a new baseline was bound. These components refine the
existing `control_stage_seconds.memory_baseline` interval; do not add them to it.
