# LLC placement pilot contract

This file freezes the screening comparison before either arm is run.

- Spec: `pilot-c8-real.yaml`
- Semantic spec digest: `56b5d20f25b4a311917c01cdffd4c95f85672190ab01a64f58c0232e0cb6171e`
- Kunpeng implementation digest: `ee665caf178ddcec91624115975b1019a9ebb118a395643d27c0803a981567f8`
- Workload: unmodified SQLGlot real Agent replay and validation command
- Density: 8 offered Agents, burst arrival, one repetition
- VM shape: 2-vCPU Runtime plus 2-vCPU Tool per Agent
- Available CPU pool: NUMA 0 CPUs 0-79 and NUMA 1 CPUs 80-159
- C0: existing load-aware NUMA placement, full selected-NUMA CPU mask; the
  common monitor records candidate-cluster CPU utilization without changing it
- C2: the same placement plus independent Runtime/Tool vCPU affinity to one
  four-physical-core Kunpeng cluster; after two consecutive CPU PSI samples at
  or above 0.10, the affected VM expands once to the full selected-NUMA mask;
  assignment count is the first ranking key and measured cluster utilization
  breaks equal-count choices, with a deterministic CPU-list tie break
- Frozen controls: templates, workload and trace, memory budgets, admission,
  borrowing, snapshotting, runtime control, arrival, validation, and timeouts

The arm order is randomized from seed 20261018. Qualification must verify the
exact implementation and both effective arms before the run. Primary screening
outputs are valid Agent count, successful Agents/s, makespan, and Agent JCT.
Topology assignment, actual target-thread affinity, adaptation events, and CPU
pressure are required mechanism checks. A single run can identify a candidate
benefit or regression; it cannot establish equivalence or a stable speedup.

At freeze time, Kunpeng SSH and the public Cube endpoints were unreachable.
No C0 or C2 measurement had started.
