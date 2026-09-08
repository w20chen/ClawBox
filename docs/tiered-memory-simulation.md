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

The current formal configuration is LOCAL 160 GiB and WARM 64 GiB. The 8 GiB
checkpoint/restore headroom is **inside**, not additional to, LOCAL. The
standalone VM cgroup enforces LOCAL and includes its charged guest RAM, VM
overhead, and retained file cache. Admission uses absolute cgroup usage, not
a host-wide baseline subtraction. NUMA placement is checked separately.

Cubelet preallocates WARM tmpfs pages outside the LOCAL cgroup before the VM
writes them. The mount binds those pages to NUMA1; its size limit and the
snapshot reservation/commit ledger bound WARM. Backend-service overhead must
be reported separately rather than described as guest RAM or pool payload.

The September 8 full-trace configuration supersedes the 64 GiB prefix pilot.
Each agent has a 2 GiB Runtime VM and a 4 GiB Tool VM. The successful c4
resident/no-eviction gate peaked at 26.65 GiB LOCAL for four pairs, or about
6.66 GiB per pair including charged overhead and cache. Linear extrapolation
suggests about 267 GiB for 40 pairs; it is a sizing estimate, not a c40 result.
160 GiB leaves meaningful pressure (240 GiB provisioned guest RAM alone) while
allowing roughly two resident waves after headroom, targeting approximately
20–30 minutes based on the 8.8-minute c4 trace time. Contention and transitions
can make runs longer. A 3600-second safety deadline avoids censoring at the
30-minute target; incomplete arms remain explicitly invalid comparisons.

All 13 policies use identical LOCAL capacity, CPU/NUMA placement, guest sizes,
full 27-round trace and original timing. No per-policy capacity tuning is used.
The two tiered policies retain 64 GiB WARM; the capacity-confounding limitation
below still applies. Earlier 64 GiB runs are pilot evidence, not mixed into
the new comparison.

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
