# NUMA deployment and the shared memory pool

Follow the [step-by-step guide](self-service.md) to initialize and apply the
host configuration before running an experiment. By default, NUMA nodes 0 and
1 are compute nodes with 36 GiB of local memory capacity each and low/high
watermarks of 28/32 GiB. NUMA node 2 supplies one 128 GiB shared pool. The
default layout needs at least three NUMA nodes; use `host inspect` to check its actual
CPU and memory layout.

For ordinary deployment without shared memory, keep the desired `compute_nodes`
and set `warm: false` in the host YAML. The shared-pool node and capacity are then
unused; `host apply` and `configure` produce a profile and experiment without
shared borrowing or warm snapshots. Resident policies need no snapshot pool.
For disk-backed snapshot experiments use `snapshot_storage: tiered` and a cold
snapshot path matching CubeSandbox. `warm-only` requires an enabled shared pool.
Runtime congestion control and admission support both layouts. Change architecture
separately from policy switches when comparing their effects.

## Configuration and placement

In the host YAML, `compute_nodes` sets the name, NUMA node, allowed CPUs,
memory capacity, and watermarks for each compute node.
`warm_node`, `warm_gib`, and `shared_borrow_percent` describe the one
shared pool. After changing them, run `host check` and `host apply`, then
generate a new experiment YAML with `configure`. The experiment's
`--pool-memory-gib` must equal the sum of the compute nodes' high
watermarks; `configure` normally copies that value from the applied host
profile. Record the full topology when comparing runs, even if two
configurations have the same total capacity.

The default `round_robin` placement assigns sessions to compute nodes in
configuration order. The agent and tool VMs of a session stay on the same
node, and each policy uses the same mapping. Concurrency 1 uses the first
compute node; concurrency 2 covers both. To study a fixed mapping:

```bash
clawbox experiment configure eval.yaml eval-explicit.yaml \
  --concurrency 4 --placement-policy explicit \
  --session-compute-nodes node0,node1,node1,node0
clawbox experiment describe eval-explicit.yaml
sudo -E "$(command -v clawbox)" --output-root "$CLAWBOX_OUTPUT_ROOT" experiment launch \
  eval-explicit.yaml --run-id explicit-01
```

The explicit list must have at least as many entries as the highest
concurrency level; lower levels use its prefix. This is static placement:
sessions are not moved or rebalanced while running. A CPU list restricts
where a VM may execute. It does not set a vCPU quota or reserve those CPUs
from other host processes. The VM template sets its vCPU count.

`template_aware_placement` is an opt-in refinement of the same placement
coordinator. It never bypasses CPU commitment, memory admission, or node HIGH
watermarks. With `template_base_sharing: required`, qualification must first
prove a common large file-backed RAM mapping with `Shared_Clean` pages for every
Tool template. The default is disabled; logical lineage is not physical sharing
evidence.

## Memory behavior and limits

- Admission checks each compute node's local measurements and budget as well
  as one atomic global demand ledger. Another node's free memory is not local
  capacity. AgentCgroup runtime control uses aggregate pressure in the physical
  parent cgroup; it does not run a separate controller on each NUMA node.
- Running VMs temporarily using NUMA node 2 and snapshots stored there share
  one capacity ledger. The system reserves a VM's configured memory before
  changing its memory binding. The combined live reservation is capped by
  `shared_borrow_percent`.
- Snapshot-aware residency uses measured WARM/SSD restore cost, per-case reuse
  probability, owned delta allocation and pool pressure to order the existing
  WARM-to-SSD path. External template Base bytes are a reported dependency and
  are not double-counted as WARM allocation. COLD relocation materializes that
  Base before releasing WARM. Live borrow and snapshot reservations still use
  one atomic capacity ledger.
- Joint scheduling uses LOCAL HIGH/LOW hysteresis. Under pressure it compares
  a safe WARM checkpoint with a live borrow only when explicit empirical
  `memory_action_costs` and provenance are configured; otherwise it keeps the
  deterministic reclaim-then-borrow order. Below LOW and without blocked
  demand, it returns at most one VM whose observed shared residency still fits
  below LOCAL HIGH. Residual shared pages keep their full reservation.
- After VM creation or restoration, the worker verifies its cgroup CPU and
  memory bindings before sending more work. Using the shared pool changes
  memory placement, not CPU placement. Restoration returns the VM to its
  original compute node.
- The local capacity is enforced by a sampled controller, not by a separate
  kernel memory limit for each NUMA node. The parent VM cgroup's
  `memory.max` covers the sum of local capacities plus the global shared
  borrowing limit.
- The supported worker prepares each priority leaf's CPU and memory set before
  creation or restore. Cube routes the VMM into that leaf before RAM allocation;
  the worker verifies vCPU affinity and physical residency afterward.
  Changing `cpuset.mems` does not guarantee migration of all existing pages.
  Physical residency comes from
  `memory.numa_stat`. Memory charges that cannot be attributed to a NUMA
  node are counted conservatively for each node's admission decision but
  only once in the host total.
- The shared-pool tmpfs size is a limit; pages consume memory as they are
  written. Snapshot pages lie outside the parent VM cgroup. Reports keep
  running-VM residency, allocated snapshot pages, and reserved snapshot
  capacity separate.
- This model uses the host's real NUMA distances. It does not simulate an
  inter-node fabric, a cross-host protocol, or configurable link bandwidth
  and latency.

## Reading the results

Before a formal run, `launch` tests runtime control, VM priority placement,
and the required snapshot/restore mechanism; shared-memory borrowing is checked
on each compute node when configured. The formal
concurrent run then records session placement. The report includes
per-node capacity, peak use, high-watermark crossings, borrow/return counts,
released reservations, action service time, and verified bindings.
In each trial JSON file, `performance.compute_nodes` contains placement and
per-node admission data; `session_trace_assignment[].compute_node` identifies the
node assigned to each session.

In `memory-timeseries.csv`, `numa_N_resident_gib` is measured residency
on NUMA node N in GiB, and `unattributed_gib` is memory not assigned to a
NUMA node. An empty cell means that measurement was unavailable. In
multi-node summaries, time above a watermark is summed across nodes; it is
not whole-host elapsed time. Node peaks may occur at different times, so
their sum is not a simultaneous peak. For CPU scheduling studies, retain
session mappings, CPU lists, NUMA residency over time, and raw PMU records.
Each arm also records kernel `node*/numastat` deltas in
`memory.numa_allocation_event_deltas`; their unit is allocation-event pages.
They are not interconnect traffic bytes. Until a platform-specific host
uncore PMU event is qualified, `memory.cross_numa_traffic_bytes` remains
`null` with an explicit reason rather than being inferred from residency,
allocation counters, or guest generic cache events.
