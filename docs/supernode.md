# Two compute nodes and one shared memory pool

Follow the [step-by-step guide](self-service.md) to initialize and apply the
host configuration before running an experiment. By default, NUMA nodes 0 and
1 are compute nodes with 36 GiB of local memory capacity each and low/high
watermarks of 28/32 GiB. NUMA node 2 supplies one 128 GiB shared pool. The
host needs at least three NUMA nodes; use `host inspect` to check its actual
CPU and memory layout.

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
clawbox --output-root "$CLAWBOX_OUTPUT_ROOT" experiment launch \
  eval-explicit.yaml --run-id explicit-01
```

The explicit list must have at least as many entries as the highest
concurrency level; lower levels use its prefix. This is static placement:
sessions are not moved or rebalanced while running. A CPU list restricts
where a VM may execute. It does not set a vCPU quota or reserve those CPUs
from other host processes. The VM template sets its vCPU count.

## Memory behavior and limits

- Each compute node makes its own admission and pressure decisions using
  local memory measurements and its low/high watermarks. Pressure on one node
  blocks new sessions there without treating another node's free memory as
  local capacity.
- Running VMs temporarily using NUMA node 2 and snapshots stored there share
  one capacity ledger. The system reserves a VM's configured memory before
  changing its memory binding. The combined live reservation is capped by
  `shared_borrow_percent`.
- After VM creation or restoration, the worker verifies its cgroup CPU and
  memory bindings before sending more work. Using the shared pool changes
  memory placement, not CPU placement. Restoration returns the VM to its
  original compute node.
- The local capacity is enforced by a sampled controller, not by a separate
  kernel memory limit for each NUMA node. The parent VM cgroup's
  `memory.max` covers the sum of local capacities plus the global shared
  borrowing limit.
- A newly created or restored VM may run briefly before its individual
  binding is applied. The parent cgroup permits the configured compute
  CPUs and the compute and shared NUMA memory nodes during that interval.
  Changing `cpuset.mems` does not guarantee migration of existing pages.
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

Before a formal run, `launch` tests the configured snapshot/restore
mechanism and shared-memory borrowing on each compute node. The formal
concurrent run then records session placement. The report includes
per-node capacity, peak use, high-watermark crossings, and verified bindings.
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
