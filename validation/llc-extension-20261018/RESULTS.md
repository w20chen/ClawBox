# Topology-aware CPU placement screening results

## Verdict

The implementation works through the supported `clawbox experiment` path, and
both the initial affinity and adaptive relaxation mechanisms were exercised on
real CubeSandbox VMs. The two matched c8 runs do **not** establish a stable
performance win:

- Run 1, ordered C2 then C0, measured **+14.39% Agents/min** and **-12.54% mean
  JCT** for C2.
- Run 2, ordered C0 then C2, measured **-5.34% Agents/min** and **+5.43% mean
  JCT** for C2.
- The arithmetic mean of the two paired effects is +4.52% Agents/min and -3.55%
  mean JCT, but the observed range crosses zero. It is a screening summary, not
  evidence of a stable speedup.

The useful positive result is narrower: cluster-level vCPU affinity can produce
a material benefit in the observed low-pressure run, and the adaptive policy
correctly removes the restriction when measured CPU pressure becomes high. The
performance effect is regime-dependent in this small sample.

## Frozen comparison

Both runs used the unmodified SQLGlot recorded Agent trace at `time_scale=1.0`,
burst arrival, eight Agents, one 2-vCPU Runtime VM and one 2-vCPU Tool VM per
Agent, the same templates, validation, memory budgets, admission, snapshot,
borrowing, and runtime-control settings.

The first run used random seed 20261018. The repeat was frozen before the first
run's C0 result was known and changed only the experiment ID and random seed to
20261019, reversing arm order. The executable code is identical between the two
remote revisions.

- Run 1 spec digest: `56b5d20f25b4a311917c01cdffd4c95f85672190ab01a64f58c0232e0cb6171e`
- Run 1 remote revision: `2e002bc77cb249f5318a5f7b59c4a61ebe3f7ab9`
- Run 2 spec digest: `150c8faad46c4646d58a9689dd979b256b7b493b17499f534d84cbf0767584e7`
- Run 2 remote revision: `d61f59666b82abf4832f1e114d70074f1a2fda28`; its only
  changes from the Run 1 revision are the reverse-order specification and its
  pre-run contract

| Run and order | Arm | Agents/min | Mean JCT s | JCT P95 s | Window s | Mean Tool latency s | Mean CPU cores | Assign / expand |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Run 1, C2 -> C0 | C0 | 0.7451 | 641.35 | 643.70 | 644.21 | 6.553 | 7.139 | 0 / 0 |
| Run 1, C2 -> C0 | C2 | 0.8523 | 560.91 | 562.76 | 563.19 | 5.299 | 6.311 | 16 / 0 |
| Run 2, C0 -> C2 | C0 | 0.7234 | 661.04 | 662.96 | 663.50 | 6.603 | 7.292 | 0 / 0 |
| Run 2, C0 -> C2 | C2 | 0.6848 | 696.96 | 699.95 | 700.95 | 7.320 | 7.229 | 16 / 16 |

| C2 relative to matched C0 | Run 1 | Run 2 | Pair mean |
|---|---:|---:|---:|
| Agents/min | +14.39% | -5.34% | +4.52% |
| Mean JCT reduction | +12.54% | -5.43% | +3.55% |
| JCT P95 reduction | +12.57% | -5.58% | +3.50% |
| Workload-window reduction | +12.58% | -5.64% | +3.47% |
| Mean Tool-latency reduction | +19.14% | -10.86% | +4.14% |

Positive numbers in the reduction rows mean C2 was lower/faster. The raw JSON
values and formulas are in `paired-analysis.json` and `analyze-results.py`.

## Correctness and mechanism evidence

All four formal arms succeeded: 32/32 offered Agent sessions completed, produced
the same validated output hash, and passed the real repository validation.
There were no failed sessions, OOM kills, LOCAL HIGH crossings, CPU throttling,
or native-tool telemetry loss. Each arm reported verified cleanup.

In Run 1, all 16 C2 VMs received an independent four-physical-core initial
mask. The 32 `vcpu0`/`vcpu1` threads all reported the exact selected mask. VM
cgroups and helper threads retained the complete selected-NUMA CPU set, and all
16 VM memory-node masks matched their selected compute NUMA node. This verifies
that the implementation changes only vCPU affinity and preserves the existing
NUMA memory placement.

Run 1 never met the adaptive threshold: its 16 VMs remained cluster-local for
the full run. Run 2 exercised the other branch. At about 167-169 seconds, all 16
VMs had two consecutive CPU PSI observations at or above the frozen 0.10
threshold. Their observed PSI ratios were 0.105-0.198 and their selected-cluster
utilizations were 88.3%-93.8%. C2 expanded every vCPU mask once to the complete
selected-NUMA CPU set, and the post-write masks were verified. There were no
fallbacks caused by missing telemetry or affinity errors.

This proves activation and safe relaxation. It does not isolate the performance
contribution of adaptation because no matched C1 static arm was run. The Run 2
regression is consistent with the initial restricted interval imposing a cost
before expansion, but that causal explanation needs a C1/C2 diagnostic.

## Topology and claim boundary

Kunpeng exposes one reported L3 sharing domain per NUMA node in the tested CPU
pool, with ten smaller eight-logical-CPU/four-physical-core clusters per node.
The experiment therefore tests cluster/core affinity inside one LLC, not a
choice between independent LLCs. It cannot support an LLC-slice locality claim.

The earlier verified static PMU experiment remains context: 48 trials and 192
four-event, 100%-running PMU records produced a maximum placement gain of 5.35%,
while historical LLC-load rate failed to predict gain (Pearson -0.050, Spearman
0.200). The present real-VM runs do not overturn that negative classifier
result and did not add a qualified host uncore PMU measurement.

## Implementation and validation

C0 remains the existing implementation and the default:
`topology_cpu_placement: off`. C1 and C2 are optional settings in the same
worker and placement path; no second experiment runner was added.

The change adds topology discovery, physical-core cluster grouping, measured
candidate-group load, exact vCPU-thread affinity verification, and one-way
CPU-PSI expansion. Focused local tests passed 147 cases, the full local suite
passed, and the remote topology/design/placement suite passed 115 tests with 7
skips. Both specifications passed public validation and real-VM qualification.

After the runs, no experiment worker, supervisor, QEMU process, or per-run
priority cgroup remained. Both local archives have the same SHA-256 as their
remote source archive:

- Run 1: `0a40b5ebf56a211d8ead79c4799677c1c726f00fd58697cbf0850082eef12ae0`
- Run 2: `fa0d220eeb8c9778a405b5881e92db05b359ffdd0fcc34c8000541c56d30fbb9`

## Practical conclusion

Keep C0 as the baseline and keep C2 optional. The current evidence supports a
paper claim that the mechanism is feasible, auditable, and responsive to real
pressure, plus a positive low-pressure screening result. It does not support a
general throughput-improvement claim.

The smallest useful follow-up is a frozen pressure-regime study with a matched
C1/C2 arm and more paired repetitions. It should test whether earlier expansion
removes the Run 2 regression while preserving Run 1's low-pressure benefit.
Changing the threshold now and reporting only the favorable regime would be
post-hoc selection.
