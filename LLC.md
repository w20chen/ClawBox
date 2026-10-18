# ClawBox: Post-Evaluation LLC-Aware CPU Placement Extension

## Objective

After completing the currently running ClawBox evaluation, independently implement and experimentally evaluate LLC-aware CPU placement as an **optional, incremental optimization**.

The primary objective remains maximizing successful Agent throughput (Agents/s and Steps/s), while reporting Agent completion latency, tail latency, and success rate.

This is an extension of the existing system, **not a redesign of the current experiments**. Preserve all completed results and avoid repeating the existing admission, checkpoint, shared-memory, NUMA, and runtime-control evaluation matrix.

You should implement the mechanisms, run real experiments on Kunpeng, diagnose problems, improve the implementation when justified, and produce reproducible results. Do not stop at writing a proposal.

## 1. Finish the existing evaluation first

The ongoing experiment campaign includes:

- Real SWE-ReBench/SQLGlot trace replay at c320 and c640.
- The existing P0/P1/P2 evaluation sequence and conditionally required P3 experiments.
- Frozen admission, NUMA, checkpoint, borrowing, and runtime-control ablations.
- A supervised execution/cleanup workflow that must remain intact.

The existing remote study is under:

`/home/weitianc/clawbox-c1280-20261017/`

Before introducing CPU-placement changes:

1. Inspect the actual supervisor state, active runs, continuation scripts, and result directories.
2. Allow the existing evaluation sequence to finish through its established gates, including verification of terminal results and cleanup.
3. Do not modify code, host configuration, CubeSandbox settings, or shared cgroup state used by an active run.
4. Preserve all historical results, including negative, invalid, excluded, and timeout outcomes.
5. Do not restart already completed experiments merely because CPU-placement support is being added.
6. Only begin new CPU-placement experiments after the existing campaign has reached a clean terminal state.

Use a separate Git revision, experiment specifications, run IDs, and result directories for the extension.

## 2. Preserve experimentally validated resource configurations

**Do not invent new memory budgets, concurrency limits, VM sizes, or snapshot settings.**

Read the actual completed experiment specifications, their frozen effective configurations, and the applied host profile.

In particular, inspect the existing Full configuration (`F-S384`, or its actual latest valid equivalent), including:

- NUMA 0/1 compute-node assignment and CPU masks.
- LOCAL LOW/HIGH/HARD limits.
- NUMA 2 WARM/shared-pool capacity and borrowing limit.
- Runtime and Tool VM vCPU and memory sizes.
- Admission predictions and frozen calibration artifacts.
- Idle checkpointing, snapshot format, and memory reclamation.
- Runtime pressure-control mechanisms and memory action costs.
- VM creation concurrency, model-wait replay, deadlines, and arrival schedule.

For context, the latest tracking plan used:

- c320: LOCAL LOW/HIGH = 128/160 GiB.
- c640: LOCAL LOW/HIGH = 256/320 GiB.
- Physical LOCAL HARD = 928 GiB.
- WARM/shared pool = 384 GiB on NUMA 2.
- Maximum live borrowing = 192 GiB.
- Runtime VM = 2 vCPU / 2 GiB; Tool VM = 2 vCPU / 4 GiB.
- Real trace timing preserved at `time_scale=1.0`.

These values are **references, not new hard-coded defaults**. Use the final validated configuration and retain its density-specific memory settings. If the actual completed configuration differs, document and use the actual one.

Do not revert to the smaller memory budgets found in starter examples.

## 3. Implement CPU placement as a separate optional mechanism

Study the existing ClawBox implementation, especially:

- `clawbox/experiments/numa_placement.py`
- `clawbox/experiments/priority.py`
- `clawbox/experiments/worker.py`
- `clawbox/experiments/spec.py`
- `clawbox/experiments/design.py`

Also inspect the reference implementation:

https://github.com/amor0404/llc-placement/tree/main/src/llc_placement

Reuse its useful topology and placement ideas, but adapt them correctly to CubeSandbox VMs rather than copying the Docker-specific implementation.

Introduce three CPU placement modes:

### C0 — `numa_only` (baseline)

Preserve the existing ClawBox behavior exactly.

- The existing NUMA placement policy chooses the compute node.
- Linux schedules VM threads within that node's allowed CPU set.
- No additional LLC-specific binding or runtime restriction.
- This must remain the default for existing configurations.

### C1 — `llc_static`

Implement topology-aware placement within the selected NUMA node.

- Discover physical CPU, SMT, cluster, LLC, and NUMA topology using host sysfs.
- Prefer spreading independent VM workloads across LLC domains to avoid cache interference.
- Prefer keeping a VM's multiple vCPU threads within one LLC when feasible.
- Balance placement based on available CPU capacity and existing assignments.
- Permit CPU sharing and Linux time-slice scheduling within the selected CPU set.
- Avoid permanently pinning a multi-vCPU VM to one physical CPU.
- Never cross the selected compute NUMA node merely to obtain a different LLC.

The mechanism should account for VM-level CPU demand and oversubscription rather than assuming that every VM owns dedicated physical cores.

### C2 — `llc_adaptive`

Extend C1 with contention-aware CPU-affinity adjustment.

- Start with LLC-local placement when practical.
- Observe actual CPU utilization, scheduling wait/pressure, and per-domain load.
- Detect cases where a restricted LLC is overloaded while another permitted LLC has spare capacity.
- Expand a VM's permitted CPU set when this is likely to reduce scheduling delay.
- Preserve LLC locality when no useful alternative capacity exists.
- Use hysteresis and cooldown to prevent frequent affinity oscillations.
- Prefer safe lifecycle or tool-execution boundaries for reconfiguration.
- Fall back to the original NUMA CPU set when the finer-grained policy cannot make a safe placement decision.

Do not interpret mere vCPU overcommit as proof of CPU saturation.

Avoid introducing a complicated predictive model unless existing measurements show that it is needed.

## 4. Integrate without disturbing existing ClawBox mechanisms

Keep the CPU-placement policy subordinate to the existing NUMA placement decision.

The conceptual hierarchy is:

1. Existing ClawBox logic selects the compute NUMA node.
2. The optional CPU-placement policy selects an allowed CPU subset within that node.
3. Linux continues scheduling the VM's vCPU threads within that subset.

Preserve the existing cgroup v2 and CubeSandbox preallocation/verification mechanism.

Pay particular attention to:

- `cpuset.cpus` versus `cpuset.mems`.
- Existing CPU weights and quotas.
- Runtime and Tool VM lifecycle.
- Pause, checkpoint, resume, and restore.
- Shared-memory borrowing and return.
- cgroup tombstone semantics and post-restore affinity verification.
- Atomic placement accounting and cleanup after failures.

CPU placement must not accidentally reset memory bindings, invalidate shared-pool reservations, or interfere with checkpoint/restore behavior.

Placement leases/accounting must remain consistent across concurrent VM creation and destruction.

Do not alter existing VM vCPU counts or introduce CPU quota changes as part of the optimization.

Expose the new modes through the existing experiment configuration system, preferably as an additional setting inside the existing placement dimension rather than creating a new architectural framework.

Old experiment YAML files must retain their original behavior.

Add focused unit, integration, and real-VM tests before formal measurements.

## 5. Fair experimental comparisons

Use a valid existing Full configuration as the common foundation.

Compare:

| Arm | CPU placement | Other ClawBox mechanisms |
|---|---|---|
| C0 | Existing NUMA-only placement | Unchanged |
| C1 | Static LLC-aware placement | Identical to C0 |
| C2 | Adaptive LLC-aware placement | Identical to C0 |

All three arms must use:

- Identical workloads, trace versions, model-wait timing, and validation.
- Identical VM templates and vCPU/memory specifications.
- Identical LOCAL/WARM budgets, checkpoint, borrowing, and admission policies.
- Identical physical CPU availability and NUMA memory configuration.
- Identical arrival schedule, timeout semantics, and creation-concurrency limits.

**Only the CPU-placement mechanism should differ within a matched comparison.**

For causal isolation, record and compare actual NUMA placement. If necessary, add a small diagnostic experiment with frozen NUMA assignments, separate from the end-to-end system comparison.

Do not reuse a historical Full result as the sole causal baseline after changing the implementation. Run a fresh, matched C0 control using the new code revision, while keeping historical Full results as contextual evidence.

## 6. Choose a small, meaningful experiment matrix

Do not repeat the earlier 20-arm memory/NUMA/admission evaluation.

Start by examining the existing Full-run CPU measurements:

- Average and peak active CPU equivalents.
- Actual runnable demand and scheduler wait.
- Per-NUMA CPU utilization and imbalance.
- CPU migrations and LLC/PMU measurements, if supported and valid.
- Time spent in CPU-intensive Tool phases versus model waiting.

Determine whether the existing CPU pool is already sufficiently contended for LLC placement to matter.

### Scenario A: Existing full CPU pool

Run C0, C1, and C2 using the previously validated resource configuration.

Prefer one representative real-trace density first, selected from prior resource-pressure evidence. Add the second density only if the first results or observed CPU behavior justify it.

This measures whether CPU placement gives an incremental benefit without artificially introducing CPU scarcity.

### Scenario B: CPU-constrained deployment (conditional)

If existing experiments show little CPU pressure, investigate a smaller shared CPU pool as an additional operating regime.

Select its size from the actual hardware topology and a short, independent CPU-pressure calibration. Do not hard-code a particular core count.

All three arms must use the same restricted CPU pool.

Ensure the restriction applies to actual CubeSandbox VMM/vCPU threads, including the baseline, rather than merely limiting the ClawBox controller process.

Preserve the original LOCAL/WARM memory configuration wherever feasible, so that the CPU experiment does not silently change memory-pressure mechanisms.

Account for the existing `placement_cpu_overcommit` admission ceiling. A smaller CPU pool must not accidentally introduce different session-admission policies between arms. If the commitment ceiling must be adjusted, use and document the same value in every matched arm.

Do not change Tool vCPU counts, model-wait timing, or memory budgets simply to manufacture a favorable result.

The goal is a realistic CPU-overcommitted deployment, not an artificially disadvantaged baseline.

If implementing an equal CPU-pool restriction safely would require invasive changes to the running system, keep it separate and do not compromise the primary experiment.

## 7. Evaluation and autonomous optimization

Use real ClawBox/CubeSandbox replay, not placement simulations as the final performance evidence.

Primary metrics:

- Successful Agents/s.
- Agent success rate.
- Mean and P95/P99 Agent completion latency.
- Offered-to-completion latency.
- Overall experiment makespan.

Secondary metrics:

- Host CPU equivalents consumed.
- CPU scheduler wait and pressure.
- CPU migrations and context switches.
- LLC misses and IPC when PMU measurements are reliable.
- CPU quota throttling, if present.
- NUMA placement and actual memory residency.
- CPU-placement decision and rebalancing overhead.
- VM creation, checkpoint/restore, borrowing, and admission activity.
- Infrastructure failures and cleanup correctness.

Report CPU efficiency separately from overall throughput.

Start with one valid run per setting for screening. Interleave and repeat the most relevant matched comparisons if a meaningful benefit or regression appears, to separate actual effects from host/time variability.

You may improve placement heuristics or tune parameters after diagnostic experiments. However:

- Use a separate pilot for parameter selection.
- Freeze the final policy and evaluation settings before confirmatory comparisons.
- Do not select or discard experiments based on whether they favor LLC placement.
- Preserve negative results and report cases where NUMA-only scheduling is better.
- Do not claim an improvement based solely on reduced cache misses if successful Agent throughput does not improve.

Prioritize configurations in which CPU-placement mechanisms actually activate and have a plausible opportunity to improve performance, while retaining the unconstrained baseline to establish the operating boundary.

## 8. Required deliverables

Complete the implementation and provide:

1. Source-code changes with tests and documentation.
2. New optional CPU-placement configuration and reproducible experiment specs.
3. Hardware topology and verified effective CPU masks.
4. A small but valid C0/C1/C2 comparison, including a CPU-contention scenario if safely feasible.
5. A summary of throughput, latency, success rate, CPU contention, and placement overhead.
6. An explanation of where the gain originates and where the optimization does not help.
7. Exact run IDs, commit hashes, resource configuration, raw result locations, and cleanup status.

Keep the original ClawBox evaluation intact.

**Execution priority:** Finish existing experiments → verify cleanup → inspect existing CPU bottlenecks → implement LLC placement → test → run minimal matched comparisons → improve if justified → report.

The objective is to obtain an additional, scientifically defensible CPU-placement optimization without invalidating or unnecessarily repeating the extensive experiments already completed.


## Additional Clarifications: CPU Budget Scaling, C0/C2 Priorities, and C1 Ablation

The following clarifications refine the original experimental plan and take precedence where the earlier C0/C1/C2 experiment matrix conflicts with the priorities below.

### 1. Interpret Scenarios A and B as CPU Budget Scaling

Scenarios A and B are not fundamentally different workloads or optimization objectives. They represent different **CPU resource budgets and potentially different levels of CPU contention** under otherwise identical ClawBox configurations.

- **Scenario A — Original CPU budget:** Use the previously validated full CPU pool. This establishes whether LLC-aware placement provides an incremental benefit under the existing deployment configuration.
- **Scenario B — Restricted CPU budget:** Reduce the available physical CPU pool while preserving the workload, VM specifications, memory configuration, and other ClawBox mechanisms. This investigates whether CPU placement becomes more valuable when runnable VM threads compete for fewer physical CPUs.

Treat these scenarios as points along a CPU-budget scaling dimension, rather than two unrelated experiments.

Importantly, CPU oversubscription does not necessarily imply actual CPU contention. Measure runnable demand, scheduler waiting, CPU PSI, and CPU utilization to establish the actual operating conditions.

Start with the original CPU budget and one carefully calibrated restricted budget. Additional CPU-budget levels are optional and should be introduced only if the initial results suggest a meaningful trend.

When restricting CPUs:

- Apply the identical restriction to all compared methods, including the baseline.
- Ensure the restriction affects actual CubeSandbox VMM/vCPU threads, not merely the ClawBox controller.
- Use the same physical CPU pool and corresponding CPU/LLC topology for each matched comparison.
- Prefer a representative subset that retains meaningful LLC placement choices. Record any changes in the number of available LLC domains.
- Avoid introducing CPU quotas, changing VM vCPU counts, or modifying memory budgets.
- Preserve the existing admission semantics. If the CPU commitment ceiling requires adjustment, apply the same calibrated setting to every method.

The purpose is to characterize the performance impact of CPU placement across realistic CPU resource conditions, not to manufacture an artificial advantage for the optimized method.

### 2. Clarify C0: A Strong NUMA-Only Baseline

**C0 is the existing ClawBox NUMA-aware placement with Linux's default CPU scheduling inside the permitted CPU pool.**

C0 must preserve the original NUMA selection, lifecycle management, memory placement, and all other ClawBox mechanisms.

In particular:

- The existing NUMA policy decides the compute NUMA node.
- VM threads can use the full permitted CPU set within that selected NUMA node.
- Linux retains responsibility for CPU scheduling, load balancing, and thread migration.
- No LLC-specific CPU subsets or additional affinity restrictions are imposed.
- Under Scenario B, the permitted CPU set is simply intersected with the common experimental CPU budget. C0 must not retain access to CPUs unavailable to C2.

C0 must not be deliberately weakened through narrower affinities, lower CPU quotas, or different admission limits.

This is especially important because Linux's unrestricted scheduling may already outperform overly restrictive LLC-local placement under heavy CPU contention.

### 3. Clarify C2: The Primary LLC-Aware Optimization

**C2 is the complete optimization proposed for evaluation. It combines topology-aware initial placement with adaptive runtime affinity management.**

Its initial placement should use the actual hardware LLC topology to improve locality and reduce interference where feasible.

However, LLC locality is a preference, not a permanent constraint.

During execution, C2 should:

1. Observe actual CPU demand, runnable pressure, scheduling delays, and load imbalance across LLC domains.
2. Detect when an LLC-local CPU subset is causing unnecessary scheduling delays while other CPUs within the permitted NUMA node have usable capacity.
3. Expand or adjust the VM's CPU affinity when the expected scheduling benefit justifies sacrificing some LLC locality.
4. Avoid unnecessary changes when the original placement is working well.
5. Use hysteresis, cooldown, and measured feedback to control adjustment overhead.
6. Fall back to the full permitted NUMA CPU set when finer-grained placement becomes counterproductive or cannot be safely determined.

C2 must never obtain additional physical CPU capacity unavailable to C0, cross the selected NUMA boundary solely for LLC optimization, or modify memory bindings as a side effect.

Use the actual cache topology reported by the host. Do not assume that hardware clusters necessarily correspond to independent LLC domains.

The expected benefit of C2 should come from improved locality, reduced cache interference, or more effective use of available CPU capacity—not from assigning more resources to the optimized method.

**C2 is not guaranteed to outperform C0 or C1.** Under low contention, it may behave similarly to static placement. Under uniform heavy contention, it may have no useful opportunity to rebalance. Its overhead and possible regressions must be measured.

### 4. Reduce the Main Experiment Matrix to C0 vs. C2

Prioritize the following matched comparisons:

| CPU resource condition | Baseline | Optimized |
|---|---|---|
| A: Original CPU budget | C0 | C2 |
| B: Restricted CPU budget | C0 | C2 |

These four configurations are the primary experiments.

The central questions are:

- Does the complete LLC-aware mechanism improve successful Agent throughput over the existing NUMA-only system?
- How does its benefit or regression change as CPU resources become more constrained?
- Are the observed differences explained by LLC behavior, CPU scheduling contention, load balancing, or optimization overhead?

Keep the original full-budget scenario even if its improvement is small or nonexistent. It establishes the operating boundary of the optimization.

Start with one run per configuration for screening. Repeat only the most informative comparisons when necessary for reliable conclusions.

### 5. Reposition C1 as an Optional Mechanism Ablation

**C1 is not a separate primary optimization target. Its main purpose is to isolate the contribution of runtime adaptation in C2.**

Conceptually:

- C1 = LLC-aware initial placement without runtime affinity adaptation.
- C2 = The same initial placement plus adaptive runtime affinity adjustment.

Therefore, prefer implementing C1 as `adaptive=false` within the shared LLC-aware placement implementation, rather than maintaining two independent scheduling frameworks.

C1 can be deferred until the primary C0/C2 comparisons have completed.

If C2 demonstrates meaningful improvement or regression, add one targeted C1 experiment under the most informative CPU-contention condition. Reuse the matching C0 and C2 results.

This comparison establishes whether runtime adaptation contributes beyond static LLC-aware placement.

If time is limited or C2 provides no meaningful system-level benefit, it is acceptable to postpone formal C1 evaluation. In that case, avoid making separate performance claims about the adaptation component.

Functional verification of any shared static-placement logic is still required before running C2.

### 6. Execution Priority

The revised priority is:

1. Complete and clean up the existing ClawBox evaluation.
2. Inspect actual hardware topology and existing CPU-pressure measurements.
3. Implement and validate C0-compatible behavior and the complete C2 mechanism.
4. Run the smallest valid C0/C2 comparison under the original CPU budget.
5. Calibrate one restricted CPU budget and perform the corresponding C0/C2 comparison.
6. Diagnose benefits, regressions, and actual adaptation activity.
7. Add the C1 static-placement ablation only if it provides useful explanatory evidence.
8. Expand CPU-budget levels or repeat experiments only when justified by the results.

Preserve all existing experimental results. Do not reopen the original admission, memory, checkpoint, NUMA, or runtime-control evaluation matrix.

**The goal is a small, rigorous incremental evaluation showing whether LLC-aware adaptive CPU placement improves Agent throughput, under which CPU-resource conditions it works, and whether its complexity is justified.**