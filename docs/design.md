# Execution and measurement model

This page defines the semantics needed to interpret experiments. Use the
[walkthrough](guide.md) for commands, the [reference](reference.md) for settings,
and the [results guide](results.md) for artifacts and timing attribution.

## Sessions and Tool execution

Each session owns a **Runtime VM** running OpenClaw and a **Tool VM** containing
the mutable workspace. Templates can be reused, but sessions have separate live
VMs, workspaces, replay positions, and ownership.

For each command, the worker identifies its reservation, restores the Tool VM
if necessary, admits additional memory growth when that policy is enabled,
and resolves the current endpoint. It verifies identity, executes the command, collects
telemetry, and releases the reservation after completion. Execution IDs join
admission, SSH execution, progress, completion, and measurements. A restore can
change the endpoint without changing the command's identity.

Preparation and actual VM creation have separate bounded queues. Tool admission
callbacks run concurrently; they share brief atomic accounting operations.
Memory admission, lifecycle transfer, dependency installation, and creation
limits are distinct controls.

## Prediction and measurement

ClawTune supplies command predictions and guest measurements. Host cgroups
provide physical consumption and NUMA residency. The Runtime image contains
its own predictor, immutable bootstrap seed, and prepared startup cache;
updating host Python files does not update that image. Runtime's optional local
Tool eBPF monitor is disabled because commands execute in the separate Tool VM.
The Tool collector measures those commands.

Host admission supports three sources:

| Source | Prediction and host-memory conversion |
| --- | --- |
| Cold-start KB | P50 Lattice memory-extra, then Lattice RSS proxy, ToolKB memory-extra, or tool-class memory-extra. The selected signal is used directly with scale 1 and offset 0; this is not measured guest-to-host calibration. |
| Trained runtime KB | Frozen LatticeKB/ToolKB queried for the incoming command, with the training artifact's measured guest-to-host mapping |
| Frozen command table | Precomputed predictions for covered commands, with the same artifact calibration |

Training derives the host-growth mapping from paired guest and host samples,
using an upper-quantile ratio and residual. It requires successful validated
runs and retains training provenance. Current ClawTune training/seed policies can
impute missing labels as zero and accept partial or approximate native labels.
A zero prediction is therefore not proof that a command consumes no memory.

Prediction coverage and accuracy are different. Coverage means a query found a
usable prediction. Assess duration and memory accuracy against matched execution
measurements on held-out tasks; P50 estimates are neither upper bounds nor
guarantees. Unavailable result measurements must remain distinct from zeros
introduced by a training policy.

## Capacity and admission

| Quantity | Meaning |
| --- | --- |
| Configured VM memory | Guest-visible capacity |
| Host cgroup charge | Observed physical consumption plus host overhead |
| Predicted growth | Estimated additional command demand |
| Admission reservation | Growth committed but not yet visible in samples |
| `memory.max` | Enforced host cgroup limit |

Creation and restore reserve configured capacity until measurements account for
growth. Tool admission reserves additional growth without counting resident
pages again. Each compute domain has a LOCAL budget; one atomic ledger accounts
for shared-pool claims. A live borrower reserves configured VM capacity so future
guest growth remains covered. Snapshots and live borrowing share that budget.

By default, reservations at or below **4 MiB** bypass memory-ledger accounting.
This includes eligible fixed-budget filesystem and maintenance calls. Calls
remain active and measured; the threshold is not a cap on actual consumption.
Setting `small_tool_admission_bypass_mib: 0` disables this exception.

The automatic **LOW routing fast path** is separate: an already resident Tool
VM whose demand fits below LOW can avoid unnecessary routing work. Ordinary
reservation accounting still applies unless the small-reservation bypass also
qualifies. Neither path skips execution or final validation.

## Runtime pressure control

Pressure control is independent of admission and idle checkpointing. It samples
VM cgroups, CPU pressure, command progress, and release history. Memory
protection and CPU release boost require their configured evidence and
contention conditions. Guest command completion alone does not prove host
memory release.

CPU boost changes cgroup weight, not vCPU count or affinity. Memory protection
changes reclaim priority without pinning the guest working set. Required PSI
interfaces are checked during qualification. Controls reset after completion,
timeout, loss of pressure, or VM replacement.

## CPU placement and NUMA memory

Managed placement assigns both session VMs to one compute node. Round-robin,
explicit, and load-aware placement are available. Load-aware placement considers
CPU use, scheduling pressure, offered vCPU commitments, memory residency, and
outstanding claims. It does not measure memory bandwidth.

CPU masks restrict eligible cores; they do not reserve them or change vCPU
quotas. The `c0` Tool policy uses the full assigned pool. `c2` can narrow an
eligible call to one LLC cluster and expands again on pressure or completion.
Short, filesystem, and maintenance calls keep the full pool.

Changing `cpuset.mems` sets allocation policy and can request migration, but
does not establish complete physical migration. Residency comes from
`memory.numa_stat`; unattributed charge is conservatively included once in
host accounting. NUMA allocation-event counters count pages, not remote traffic
bytes. This is a single-host NUMA experiment, not a simulated cross-host fabric.

## Checkpointing and shared memory

Checkpointing does not interrupt active Tool commands. During an outstanding
model request, idle reclaim can checkpoint the Tool VM or both VMs. A pending
replay response is held until Runtime is restored; Tool can remain checkpointed
until its next command.

| Location | Contents |
| --- | --- |
| Compute-node memory | Running VM pages on the assigned NUMA node |
| Shared NUMA memory | Live VM pages temporarily rebound under pressure |
| WARM tmpfs | In-memory checkpoint files on the shared node |
| COLD storage | Checkpoint files on the configured disk filesystem |

Source RAM and destination pages coexist during checkpoint copy. WARM uses an
initial conservative reservation, then actual allocated snapshot bytes.
`warm-only` has no disk fallback. `tiered` can place new snapshots on disk or
spill eligible WARM generations.

Incremental CoW snapshots can reference an immutable template RAM base. A logical
reference alone does not prove physical sharing. Template-aware qualification
checks matching large file-backed mappings in two VMMs, supported by
`Shared_Clean` and PSS. Deduplicate layer device/inode identities when computing
storage totals.

## PMU measurements

The Tool collector records cycles, instructions, LLC read accesses, and LLC read
misses. Collection begins before command execution is released. An LLC profile
is usable only when its execution ID matches, all four events are supported,
kernel coverage is present, and each counter ran for 100% of enabled time.

On ARM64, Linux LLC read events are distinct from generic cache misses and
HiSilicon `hisi_l3c` uncore events. Missing or multiplexed PMU data can leave CPU
and memory observations usable, but cannot support LLC-model training.
