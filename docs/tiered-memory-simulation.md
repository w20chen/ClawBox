# NUMA-backed CXL/UB memory-pool approximation

The study models **local execution memory → a shared memory-pool tier → SSD**.
On kunpeng, NUMA0 represents LOCAL, NUMA1-backed tmpfs represents WARM, and
SSD-backed snapshots represent COLD. This is a single-host approximation of
the storage/migration hierarchy in a CXL/UB multi-host design, not a measurement
of a real CXL/UB fabric or a complete multi-host simulator.

## What a transition means

- LOCAL executes the VM on CPUs associated with NUMA0, with guest RAM on NUMA0.
- LOCAL → WARM writes a snapshot directly into NUMA1-backed tmpfs, then destroys
  the live VM. During copying, source RAM and destination pages coexist and
  belong to their respective budgets; neither copy is free.
- WARM → LOCAL restores independent guest RAM on NUMA0 before retiring the
  WARM snapshot. Keeping a lazy file mapping into NUMA1 would instead model
  continued remote-memory access, which is not this experiment's transition.
- WARM → COLD transfers the snapshot to SSD and releases the WARM generation.
- COLD → LOCAL reads the snapshot into independent NUMA0 RAM. A common,
  snapshot-scoped cache protocol must prevent RAM cache hits from being
  misreported as SSD reads. Physical device I/O and logical snapshot bytes are
  different quantities and must be reported separately.

## Capacity and measurements

The proposed formal configuration is LOCAL 64 GiB and WARM 64 GiB. The 8 GiB
checkpoint/restore headroom is **inside**, not additional to, LOCAL. The
standalone VM cgroup enforces LOCAL and includes its charged guest RAM, VM
overhead, and retained file cache. Admission uses absolute cgroup usage, not
a host-wide baseline subtraction. NUMA placement is checked separately.

Cubelet preallocates WARM tmpfs pages outside the LOCAL cgroup before the VM
writes them. The mount binds those pages to NUMA1; its size limit and the
snapshot reservation/commit ledger bound WARM. Backend-service overhead must
be reported separately rather than described as guest RAM or pool payload.

Gate evidence must establish correct guest state, NUMA placement, released
snapshot backing, separate page charging, capacity conservation, and serialized
restore/spill of each generation. Performance runs follow these gates, not
the reverse. See [current execution evidence](tiered-oracle-execution-status.md).

## Limits on paper claims

Measured costs include this machine's NUMA interconnect, host memory copying,
snapshot implementation, and SSD. They do not establish CXL/UB bandwidth or
latency, remote-host contention, cross-host ownership transfer, fabric topology,
coherence behavior, or failure recovery. No synthetic fabric delay or bandwidth
limit is currently applied. Report the host topology and measured transfer
costs, and describe results as policy behavior under this NUMA-backed
approximation. Do not extrapolate absolute multi-host speedups from these runs.

The two added policies have extra WARM capacity compared with the eleven
WARM-disabled baselines. The requested 13-arm experiment does not include
capacity-matched controls; improvements cannot be attributed solely to policy
quality independently of the additional memory tier.
