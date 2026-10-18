# ClawBox: Post-Evaluation Topology-Aware CPU Placement Extension

## Objective and scope

Implement and evaluate an optional CPU-placement optimization within the existing `clawbox experiment` workflow, using real CubeSandbox replay on Kunpeng.

The primary objective is successful Agent throughput (Agents/s). Report success rate, completion latency, and makespan alongside it. Tool progress and CPU efficiency explain results; they do not substitute for improved successful Agent throughput.

Preserve the existing admission, checkpoint, shared-memory, NUMA, and runtime-control evaluation. Do not repeat its full matrix or restart completed experiments because CPU placement is being added. Keep historical positive, negative, invalid, excluded, interrupted, and timeout results.

This is a small paper-project extension. Implement only what is needed for experiment correctness and a reproducible comparison. Do not build a second scheduler framework or experiment execution path.

The main comparison is C0 (existing placement) versus C2 (topology-aware initial placement with adaptive affinity). C1 disables adaptation in the same implementation and is an optional mechanism ablation.

## 1. Close out the existing campaign and qualify the baseline

Before modifying code or configuration used on Kunpeng:

1. Inspect actual supervisors, active runs, continuation scripts, frozen configurations, and result directories.
2. Respect the campaign's existing gates and any explicit stop decision. Establish whether it completed, failed, or was intentionally stopped; do not treat a stopped supervisor as successful evaluation.
3. Verify cleanup of the relevant run resources before new experiments. Do not change code, CubeSandbox settings, host configuration, or shared cgroups used by an active run.
4. Preserve results and outstanding limitations. Closing out the campaign does not require completing every previously proposed arm or reopening the old matrix.
5. Use a separate Git revision, specifications, run IDs, and result directories for this extension.

Do not assume that a configuration named Full or `F-S384` already has a valid real-trace performance baseline. Read its effective configuration and actual outcomes. Functional qualification, clean termination, and successful workload completion are different facts.

Choose one representative real-trace condition for a small pilot. Prefer a density and measurement window that yield enough completed Agents to assess throughput and latency without infrastructure failures dominating the comparison. Select and freeze these settings using independent pilot evidence, before confirmatory comparisons.

A zero-completion C0 is not automatically invalid:

- If both C0 and C2 complete zero Agents, successful Agents/s cannot distinguish them in that window. Report tool progress separately and diagnose the limitation.
- If C0 completes zero and C2 completes Agents under a valid matched comparison, report absolute throughput and completion counts. Do not compute a finite percentage speedup over zero.
- Distinguish infrastructure failures from workload timeouts and policy-induced delays before interpreting either case.

Do not silently shorten or replace the trace, accelerate model waits, or promote Steps/s to the primary objective to obtain a favorable result. Any pilot change to density or window must have a stated purpose and be identical within the resulting comparison.

## 2. Preserve the resource configuration

Use the actual frozen Full configuration and applied host profile as the starting point. Do not copy stale planning numbers or small starter-example budgets as new defaults.

Verify and retain:

- Compute NUMA assignments and permitted CPU masks.
- Per-node and aggregate LOCAL capacity and LOW/HIGH/HARD limits, with their scopes and units.
- WARM/shared-pool placement and capacity, and the live borrowing limit.
- Runtime and Tool VM templates, vCPU counts, and memory sizes.
- Prediction/calibration artifacts and admission settings.
- Idle checkpoint policy, snapshot mechanism, memory reclamation, and pressure control.
- VM creation concurrency, arrival schedule, deadlines, and model-wait replay timing.

Keep real trace timing at `time_scale=1.0` for this study. Preserve density-specific memory settings. If an inherited setting lacks valid evidence, identify that limitation rather than calling it validated.

Do not change VM vCPU counts, CPU weights, CPU quotas, or memory budgets as part of the placement optimization. Necessary baseline corrections must be completed and frozen before the matched C0/C2 comparison; do not change them between arms.

## 3. Establish the actual optimization target

Discover online CPUs, physical cores, SMT siblings, cluster membership, cache levels and sharing sets, NUMA membership, and the effective permitted CPU pool. Intersect every candidate mask with that pool.

The Kunpeng topology inspected for this plan reports:

- NUMA 0 CPUs `0-79` share one L3 domain; the other NUMA nodes similarly have one reported L3 domain each.
- Smaller cluster groups exist, such as `0-7` and `8-15`.
- SMT siblings such as `0-1` share a physical core and its reported L2; this L2 sharing set is smaller than the cluster.

Revalidate topology before execution. These examples are not hard-coded placement defaults.

**On this topology, there is no choice between independent LLC domains inside one selected NUMA node.** Using all permitted CPUs in that node's sole LLC gives the C0 pool. Selecting a smaller set tests cluster/core affinity within the same LLC.

Therefore:

- For Kunpeng, formulate the initial hypothesis as NUMA-local cluster/core placement with adaptive relaxation.
- Do not equate a cluster with an independent LLC, a shared L2 domain, or an isolated cache capacity allocation.
- Claims about LLC slice locality require additional hardware evidence or targeted measurements. Cache-miss changes alone do not establish that mechanism.
- Do not create a "same NUMA, different LLC" control when the topology has no such sets.
- If no meaningful topology-based choice or plausible scheduling benefit exists, document that boundary and stop or defer the extension before building an elaborate policy.

Potential benefits include reduced interference, fewer costly migrations, or better use of available CPU capacity. These are hypotheses to test. CPU scheduling contention is not required for cache interference, and vCPU overcommit does not establish scheduling contention.

## 4. Define a small placement policy

### C0: Existing placement with the extension disabled

Keep the existing NUMA policy, including `load_aware` where configured. Do not introduce a replacement NUMA-selection path named `numa_only`.

VM threads retain the full permitted CPU pool inside the selected compute NUMA node. Linux performs scheduling, balancing, and migration within that pool. Under the restricted-budget scenario, the common experimental CPU budget also constrains C0.

Supported experiment configurations that omit the new optional setting retain their original behavior. This does not impose compatibility requirements on deprecated workflows.

### Shared exec-call placement for C1 and C2

Before implementation, specify the following short policy contract. Choose the simplest mechanism supported by topology and pilot evidence; record and freeze it before formal measurements.

| Decision | Required definition |
|---|---|
| Placement unit | One active `agent-tool` `exec` call. Runtime, filesystem operations, backend maintenance, and unknown operations keep C0 behavior. Concurrent eligible exec calls in one Tool VM share its active assignment; the final completion restores the full C0 mask. |
| Initial allowed set | Selection of cluster/core groups, mask size, SMT handling, and behavior when a complete group is unavailable. |
| Ranking | Measured load and assignment information used to choose a group, deterministic tie-breaking, and unknown-observation handling. |
| Sharing | How concurrent VMs share CPUs without assuming dedicated cores or interpreting every assigned vCPU as active demand. |
| Lifecycle | Assignment ownership across creation, model waiting, pause, checkpoint, restore, failure, and destruction. |

Keep each multi-vCPU VM on multiple physical cores when feasible. Do not unintentionally use two SMT siblings as if they were two independent physical cores. Account for the VMM/helper-thread scope when choosing and reporting masks.

Runtime and Tool belonging to one Agent does not prove useful cache sharing between them. Do not assume that co-location is beneficial without evidence, and do not add an Agent-pair co-location experiment unless it is needed to support a specific claim.

Maintain atomic assignment accounting, but do not add a per-cluster/LLC hard admission ceiling as part of this extension. Assignment counts are bookkeeping inputs, not measured CPU utilization. Preserve the existing session-admission mechanism.

### C2: Adaptive affinity

C2 applies the shared placement immediately before an `agent-tool` `exec` command is admitted,
keeps the VM at the configured 2 vCPUs, and adjusts only those Tool vCPU thread
affinities while the call is active. Completion, failure, or a lost admission
response restores the full selected-NUMA C0 mask. Filesystem and maintenance
operations never acquire a C2 assignment. Short exec calls may still be bound;
duration prediction is outside this version. Its contract must define:

- Sampling interval, averaging window, and observation freshness requirements.
- A usable scheduling-wait or CPU-pressure signal, plus CPU utilization and load of candidate CPU groups.
- The pressure condition for expansion and the target mask.
- Adjustment is a one-way relaxation for the active assignment; it does not contract before release.
- Adjustment timing during long Tool calls as well as lifecycle boundaries. Waiting until a delayed call finishes cannot relieve that call's scheduling delay.
- Fallback conditions and how the new effective mask is verified and recorded.

Do not build a predictive model unless simpler feedback is demonstrably insufficient. Do not move work across its selected compute NUMA node or outside the common CPU budget.

Preserve locality when it is useful, but allow relaxation to the full permitted NUMA CPU pool when observations are unavailable or finer placement becomes counterproductive. Record the reason and time spent in fallback. Deliberate policy fallback is valid behavior; affinity write or verification failures must be reported as failures, not silently relabeled as fallback.

### C1: Optional adaptation ablation

C1 uses exactly C2's per-call initial placement with adaptation disabled. It is
not a separate scheduling implementation or a required primary experiment.
The first public implementation exposes only C0 and C2; add C1 to the same
switch only if a real-VM pilot shows that adaptation activates and the paper
needs to attribute that contribution.

Verify the shared initial-placement logic before C2 measurements. Run a formal C1 comparison only when it helps explain a meaningful result or is needed for a separate claim about adaptation.

## 5. Integrate with VM binding and memory lifecycle

Read the active callers and shared code before editing:

- `clawbox/experiments/numa_placement.py`
- `clawbox/experiments/priority.py`
- `clawbox/experiments/numa_borrow.py`
- `clawbox/experiments/pressure.py`
- `clawbox/experiments/worker.py`
- `clawbox/experiments/topology.py`
- `clawbox/experiments/spec.py` and `design.py`

Inspect existing placement/PMU helpers in `placement.py`, `placement_docker.py`, and `pointer_chase_runner.py`, plus the [reference implementation](https://github.com/amor0404/llc-placement/tree/main/src/llc_placement). Reuse suitable helpers, not an independent Docker/native execution path for the final evidence. The pointer-chase runner uses native workers and fixed CPU selections; it is not a ready-made real-VM pilot. Existing PMU validators also have collector and record-format assumptions.

### Resolve CPU-mask ownership

The current binding paths have a concrete conflict with narrower per-VM cgroups:

- `priority.prepare()` writes the saved session mask before creation/restore; `priority.verify()` checks the expected cgroup mask.
- `SandboxNumaBorrower.pin_local()` writes its configured CPU mask after creation/restore if the effective mask differs.
- The worker constructs each borrower with the compute domain's configured `node.cpus`.
- Borrow and return call `_move()`, which requires that same CPU mask through `_verify_cpus()` before changing memory placement.

Simply narrowing `cpuset.cpus` therefore risks being overwritten or failing borrow/return validation. Choose one ownership model and keep it consistent:

| Approach | Required integration |
|---|---|
| Thread affinity | Keep the VM cgroup at the common NUMA CPU pool; apply the narrower mask to explicitly identified vCPU/VMM threads. Handle new/replaced threads and restore. Add verification against the policy's selected mask. |
| Per-VM cgroup subset | Make priority preparation, borrower checks, and lifecycle restoration use the current per-VM mask consistently. A domain-wide borrower cannot use one mutable mask for concurrently different VM assignments. |

Neither approach is automatically simpler. Select based on the actual lifecycle hooks and smallest sufficient change.

The existing thread check only establishes that `Cpus_allowed_list` is contained in the cgroup mask. It does not establish that the finer policy took effect. Verify the intended allowed set for each targeted thread and record any creation/restore interval before it is enforced.

Affinity decisions must preserve `cpuset.mems`, memory reservations, and borrowing semantics. Borrow/return must not accidentally reset CPU placement; CPU adaptation must not reset memory placement. Verify after relevant transitions and reapply only where needed, never by blindly invoking a memory-binding hook.

Preserve cgroup preallocation, tombstone handling, CPU weights/quotas, and failure cleanup. Keep desired masks, effective masks, assignment accounting, and VM generation identity consistent during concurrent lifecycle operations.

## 6. Small real-VM pilot and necessary observations

Use existing measurements first, then a small real CubeSandbox pilot before c320/c640-scale runs. The pilot should establish:

1. C0 preserves the existing behavior and performs no topology discovery, sampling, CPU admission, or affinity change.
2. Per-call placement, completion restoration, and C2 adjustments actually reach the Tool VM vCPU threads.
3. Creation, restore, borrow/return, and cleanup preserve CPU and memory policy.
4. At least one usable wait/pressure signal and candidate-group CPU-load measurements support C2 decisions.
5. Tool execution, model waiting, admission, and VM lifecycle costs can be distinguished well enough to select a useful operating condition.

Check signal availability explicitly. The existing `CpuMonitor` reads scheduling wait only when `sched_schedstats` is enabled; missing values are not zero pressure. Existing VM-level PSI collection may be reusable, but does not by itself provide every candidate group's load. Any monitoring prerequisite or setting change must be documented and applied consistently to C0 and C2 outside active runs.

Record the minimum needed for decisions and interpretation: per-group CPU use, per-VM CPU time and wait/pressure where available, assignment counts, effective masks, adjustment reasons, and overhead. Do not make complete working-set estimation, migration counters, or PMU collection prerequisites for a first valid system comparison.

Use PMU data only with verified event semantics, workload/thread scope, and running/enabled coverage. Store raw counters and coverage; unsupported or unreliable measurements remain unavailable, not zero. Optional microbenchmarks can test a hardware hypothesis, but neither positive nor negative native pointer-chase results establish real-VM system benefit.

If the representative real-VM pilot gives no useful signal or plausible opportunity, stopping or deferring is acceptable. Report the tested boundary and evidence; do not assert that all adaptive placement is ineffective because one static microbenchmark was negative.

## 7. Fair comparisons and CPU budgets

Run a fresh matched C0 using the same revision as C2. Historical Full results remain context, not the sole causal baseline.

Within each matched comparison, hold constant workloads and trace hashes, validation, templates, vCPU/memory specifications, LOCAL/WARM budgets, admission rules, snapshot/borrowing/control policies, arrival schedule, timeouts, creation limits, and available physical CPU pool.

Only the optional CPU-placement mechanism should differ. Keep monitoring settings comparable and include policy-specific observation/decision costs in the optimization's overhead.

Record actual NUMA assignments and memory residency. They may change indirectly through feedback under an unchanged NUMA policy. If this obscures an important mechanism claim, add a small diagnostic with frozen assignments; do not change the main system comparison to force identical outcomes.

### A: Original CPU budget

Start with one representative real-trace density and the inherited CPU pool. Retain this condition even if the result is small or negative; it establishes the deployment boundary.

### B: One calibrated restricted CPU budget

Treat B as a second point on the CPU-budget dimension, not a new workload. Select one budget through independent topology and pressure calibration. Run it when a safe, meaningful matched restriction is feasible; otherwise state why it was deferred.

Prefer the existing spec-level `compute_nodes[].cpus` configuration if its enforcement is verified on actual VMM/vCPU threads. Restrict all compared methods equally. A controller-only affinity change is insufficient.

Record physical-core count, logical-CPU count, SMT sibling selection, available clusters, and reported LLC domains. Reducing physical cores and disabling SMT siblings are different changes. Preserve the SMT selection rule across budgets when feasible, and retain meaningful placement choices.

The existing CPU commitment ceiling is:

`number of configured logical CPUs × placement_cpu_overcommit`

Reducing the pool without changing this parameter also reduces the commitment ceiling. If the intended A/B interpretation requires a constant commitment ceiling, calibrate and freeze the corresponding parameter; report both the ceiling and whether it actually bound admission. If the ceiling changes, do not describe A/B as an isolated CPU-budget effect.

Within each budget, C0 and C2 must have identical admission rules and commitment ceilings. **Do not force identical actual admitted-session counts or admission timelines.** Faster completion can legitimately release resources earlier and admit more work; that is part of the system effect.

Preserve memory budgets, VM vCPU counts, model waits, and CPU weights/quotas. Restricting VM CPU masks does not automatically isolate controller/Cubelet or unrelated host work. Record their activity and keep their configuration consistent; do not silently add service isolation or alter unrelated workloads.

## 8. Minimal matrix and claim-specific diagnostics

| CPU budget | Baseline | Optimized |
|---|---|---|
| A: Original | C0 | C2 |
| B: Calibrated restricted, when feasible | C0 | C2 |

Start with one valid run per setting for screening. Use independent pilot data for parameter selection and freeze the policy, workload, window, and settings before confirmatory comparisons.

Interleave and repeat selected comparisons when needed to support a benefit or regression beyond host/time variability. Do not require a full four-arm matrix or a fixed three-repeat campaign by default. A single null run supports only "no benefit observed in this run," not equivalence.

A fair C0/C2 comparison can establish the effect of the **complete strategy**. It cannot alone identify the contribution of cache locality, mask size, or runtime adaptation.

Add only the diagnostic required by the intended claim:

- **Adaptation:** compare C1 with matching C2, reusing the appropriate C0 context. C1 and C2 share initial placement and all other settings.
- **Topology selection:** compare equal-size CPU masks with matched physical-core/SMT resources and comparable load, varying topology selection within the permitted NUMA node. On Kunpeng this can be a cluster-layout or topology-blind selection diagnostic, not a fictional cross-LLC control.
- **Dynamic topology contribution:** if needed, use the same adaptation rules with topology-blind selection. A static pinned control alone does not control C2's time-varying mask behavior.

C1/C2 similarity does not prove adaptation is universally useless; first check its activation. C1 below C2 can show that adaptation mitigates static-placement costs, but C0/C2 still determines whether the complete extension improves on the existing system.

Do not add a Runtime/Tool co-location matrix, more CPU budgets, or additional densities without a specific unresolved question. Preserve all runs and negative results; do not select or discard outcomes based on whether they favor the optimization.

## 9. Metrics and interpretation

Freeze event definitions, observation windows, deadline semantics, and success validation before formal comparisons.

Report:

- **Successful Agents/s:** validated successful Agent completions divided by the declared measurement window. Include offered, admitted, completed, failed, timed-out, and unfinished counts.
- **Success rate:** give its denominator explicitly, including offered requests when reporting end-to-end service success.
- **Latency:** mean and P95/P99 where sample counts support them; distinguish execution-start-to-completion, offered-to-completion, and cleanup completion. Report the completion subset and unfinished fraction; do not silently omit censored requests from interpretation.
- **Makespan:** state the start/end definition. Do not describe a deadline-truncated observation as the completed batch's makespan.
- **Tool progress:** distinguish logical trace calls, underlying command completions, and successful steps. Do not count maintenance commands as Agent work or label all command completions successful Steps/s.
- **CPU efficiency:** CPU time per successful Agent, plus total measured CPU time and Tool CPU/wall time. With no successes, the per-success quantity is undefined.
- **Mechanism activity:** pressure/load, effective mask occupancy over time, adjustment/fallback activity and overhead, NUMA residency, admission, creation, checkpoint/restore, and borrow/return activity.
- **Optional diagnostics:** reliable LLC accesses/misses or MPKI, IPC, migrations, context switches, and quota throttling. Identify the scope of every CPU metric; VM cgroup consumption is not total host consumption.

Fixed arrival rates, model waits, memory admission, or infrastructure costs can limit Agents/s even if Tool execution becomes faster. Report local acceleration and system throughput separately. Neither lower cache misses nor higher Steps/s alone establishes improved successful Agent throughput.

## 10. Configuration, validation, and deliverables

Expose the extension through the existing experiment configuration, defaulting to disabled. Keep NUMA selection and optional finer CPU placement distinct. Freeze the exact public fields during implementation; mode labels in this plan are not claims that those options already exist.

Every public policy parameter must be editable and documented with units, defaults, prerequisites, and the effects of missing observations. Explain which changes require host apply, image rebuild, retraining, or a new run ID. Update the scenario walkthrough and option reference with the implemented public change.

Use focused tests for C0 masks, assignment concurrency, adaptation/fallback, actual affinity verification, lifecycle interactions, memory-binding preservation, and cleanup. Validate the public path on Kunpeng, including fresh-shell use, repeat execution, and relevant failure paths. If installation changes, exercise them in isolation; never reinstall a shared host merely for validation.

Keep verification artifacts separate from user documentation. Distinguish unit/syntax/help checks, real-VM integration, and formal performance evidence. Do not describe an existing-host run as proof of fresh installation.

Deliver:

1. The minimal policy contract, implemented configuration, source changes, focused tests, and usage documentation.
2. Reproducible specs, topology, SMT selection, common CPU budgets, and verified effective masks.
3. The small C0/C2 comparison, with B and claim-specific diagnostics only where justified and feasible.
4. Throughput, latency, success, CPU efficiency/pressure, activation, and overhead results with their uncertainty and limitations.
5. Run IDs, commit and artifact hashes, frozen configurations, raw result locations, and cleanup status.

If feasibility or pilot evidence stops the extension, deliver the evidence and the explicit unimplemented/untested scope instead of manufacturing a completed evaluation.

**Execution priority:** Close out the existing campaign and verify cleanup → confirm the actual topology hypothesis and baseline condition → define and implement the smallest policy with consistent binding ownership and usable feedback → validate with a small real-VM pilot → freeze settings → run minimal C0/C2 comparisons → add only necessary diagnostics/repeats → report.
