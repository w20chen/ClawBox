# ClawBox: Tool-Call Cluster Affinity Follow-up Plan

## 0. Purpose and execution rule

Evaluate the current optional Tool-call CPU-affinity policy through the supported
`clawbox experiment` workflow on the Kunpeng host. The primary outcome is
validated successful Agent throughput. Latency, CPU pressure, effective masks,
and CPU consumption explain the result; they do not replace Agent throughput.

This is a paper experiment, not a production-hardening project. Run the smallest
experiment set that can answer the main question. Fix and rerun only when a
problem can change correctness, policy activation, resource equivalence, or the
reported conclusion. Do not repeat a clean completed run after documentation,
reporting, cosmetic, or otherwise non-causal changes.

Repository root on the local workstation:

`C:\Users\29068\Desktop\ClawBox`

Remote host and isolated checkout used by the existing LLC work:

- SSH target: `kunpeng`
- Checkout: `/home/weitianc/clawbox-llc-20261018`
- Result root: `/home/weitianc/clawbox-tiered-study-20260907/raw-standalone`

Before running anything, record the actual local and remote Git revisions and
verify that no experiment supervisor, worker, owned QEMU process, or per-run
cgroup remains active. Do not assume the revisions named in historical results
are still HEAD.

## 1. Current implementation and version boundary

### 1.1 Policy implemented at current HEAD

The current public option is `placement.tool_call_cpu_policy`:

- `c0` is the default. Runtime and Tool VM threads retain the complete CPU mask
  of the selected compute NUMA node and Linux schedules them within that pool.
- `c2` applies only to active `agent-tool` `exec` calls. It selects a complete
  cluster/core group inside the Tool VM's selected NUMA node and narrows the
  Tool vCPU-thread affinity for the duration of the eligible call.
- Filesystem operations, backend maintenance, and unknown operations keep C0
  behavior.
- Concurrent eligible calls in one Tool VM share that VM's active assignment.
  The final completion, failure, or abandoned admission restores the full C0
  NUMA mask.
- C2 observes Tool-VM CPU PSI. After the configured number of consecutive high
  samples, it performs a one-way relaxation to the complete selected-NUMA mask
  for that active assignment. It does not contract again before release.

This is affinity, not exclusive CPU ownership. C2 does not reserve physical
cores, create an exclusive lease, or prevent another VM from using the same
cluster. It also does not change VM vCPU count, `cpu.max`, `cpu.weight`, memory
placement, or the common physical CPU budget.

Relevant implementation files:

- `clawbox/experiments/priority.py`
- `clawbox/experiments/worker.py`
- `clawbox/experiments/spec.py`
- `clawbox/experiments/design.py`
- `clawbox/experiments/topology.py`

The current public implementation exposes C0 and C2. C1 is not a required
up-front implementation. If a later mechanism diagnostic is justified, C1 must
reuse exactly C2's per-call initial assignment and disable only PSI relaxation.

### 1.2 Historical result boundary

The two archived runs under
`validation/llc-extension-20261018/runs/` used the older
`topology_cpu_placement=cluster_adaptive` implementation. The current HEAD uses
`tool_call_cpu_policy=c2`. Therefore the archived performance values are
historical pilot evidence, not repetitions of the current policy and not part
of the new current-HEAD effect estimate.

Historical pilot results:

| Run order | C2 Agents/min versus matched C0 | C2 mean-JCT change | Expansion |
|---|---:|---:|---:|
| C2 then C0 | +14.39% | -12.54% | 0 |
| C0 then C2 | -5.34% | +5.43% | all recorded assignments expanded |

The signs disagree. These runs establish that topology affinity can have a
material, regime-dependent effect and that PSI relaxation can activate. They do
not establish a stable speedup for either the old or current implementation.

Use the archived data for workload selection, CPU-budget calibration, and
measurement definitions only. Preserve all positive and negative results.

## 2. Facts already established for the frozen trace

Use the recorded SQLGlot Agent trace without shortening or changing replay
timing:

- Trace:
  `/home/weitianc/clawbox-high-density-followup-20261017/setup-source/examples/traces/sqlglot-4528-workspace-replay-v2.jsonl`
- Repository: `tobymao/sqlglot`
- Base commit: `bc68289d4d368b29241e56b8f0aefc36db65ad47`
- Replay `time_scale`: `1.0`
- Arrival: burst
- Concurrency: 8 Agents
- Runtime VM: 2 vCPU, 2048 MiB
- Tool VM: 2 vCPU, 4096 MiB
- One Runtime VM plus one Tool VM per Agent: 4 configured vCPU per Agent,
  32 configured vCPU across eight Agents

Copy all template IDs, immutable image digests, validation commands, memory
budgets, admission settings, checkpoint policy, borrowing policy, runtime
control, timeouts, and creation limits from the frozen pilot specification:

`validation/llc-extension-20261018/pilot-c8-real.yaml`

That file contains old placement field names. Do not launch it unchanged with
current HEAD. Create new run specifications using the current public
`tool_call_cpu_policy` fields while preserving all unrelated effective values.

### 2.1 Observed CPU demand in the 160-logical-CPU pool

The two historical C0 arms measured:

| Metric | C0 run 1 | C0 run 2 |
|---|---:|---:|
| Experiment cgroup mean CPU | 7.139 cores | 7.292 cores |
| Mean utilization of 160 logical CPUs | 4.46% | 4.56% |
| Maximum approximately one-second interval | 16.616 cores | 16.712 cores |
| Peak interval utilization of 160 logical CPUs | 10.39% | 10.45% |
| Mean per-call Tool CPU utilization | 1.015 cores | 0.992 cores |
| Maximum call-duration average | 1.897 cores | 1.751 cores |
| Peak concurrent Agent Tool calls | 8 | 8 |
| CPU throttling | 0 | 0 |

`peak_interval_cores` is an average over the sampling interval, not an
instantaneous microburst peak. CPU-core values are logical-CPU-time equivalents
and must not be relabeled as physical-core occupancy.

The 160-CPU condition has no evidence of global CPU-capacity contention. In the
old adaptive run, however, selected clusters reached about 88.3%-93.8%
utilization while the full pool remained lightly utilized. The relevant current
hypothesis is therefore local pressure, migration, or cache/scheduling behavior
created or changed by affinity, not exhaustion of the whole host CPU pool.

The Kunpeng topology used by these runs has one reported L3 sharing domain per
selected NUMA node. Smaller eight-logical-CPU/four-physical-core cluster groups
exist inside that L3 domain, with SMT sibling pairs such as CPUs `0-1`.
Consequently this experiment tests cluster/core affinity within one LLC. It
must not be described as selecting among independent LLCs or allocating an LLC
slice.

## 3. Research questions and claim boundary

Answer these questions in order:

1. Does the current per-call C2 implementation execute correctly and measurably
   change the intended Tool vCPU-thread masks without changing memory placement
   or other CPU controls?
2. At a restricted but still topology-meaningful CPU budget, does C2 improve
   validated Agents/min or JCT relative to C0?
3. At the original 160-logical-CPU budget, is the effect absent, beneficial, or
   harmful when global CPU pressure is low?
4. If PSI relaxation activates and the C0/C2 result is material, is one small
   C1 or threshold diagnostic necessary to explain the mechanism?

Do not claim:

- exclusive core allocation;
- a general LLC-placement benefit;
- cache causality from misses alone;
- stable speedup from a single matched pair;
- equivalence from a null result;
- current-policy validation from the older prototype runs.

The earlier static PMU experiment remains negative classifier evidence: 48
clean trials and 192 qualified four-event records had a maximum placement gain
of 5.35%, while historical LLC-load rate did not predict gain (Pearson -0.050,
Spearman 0.200). Do not rerun that matrix for this follow-up. If a new cache
causality claim is attempted, it requires qualified raw PMU records and a
separate claim-specific design.

## 4. Frozen CPU budgets

Use exactly two budgets. Do not add densities or additional CPU budgets unless
one of these configurations is invalid on the current host.

| Budget | NUMA 0 CPUs | NUMA 1 CPUs | Logical CPUs | Physical cores | Role |
|---|---|---|---:|---:|---|
| A: original | `0-79` | `80-159` | 160 | 80 | Low-global-pressure boundary |
| B: restricted | `0-15` | `80-95` | 32 | 16 | Primary contention/local-sharing test |

Budget B preserves two complete cluster groups in each selected NUMA node. It
is small enough to reduce the large observed headroom, while retaining a real
topology choice. A smaller pool with only one group per NUMA would remove the
placement decision and is outside this plan.

Keep the existing SMT rule represented by these masks. Record logical CPUs,
physical cores, SMT siblings, cluster groups, and the effective cgroup and vCPU
thread masks in every run.

Keep `placement_cpu_overcommit=32` and all session-admission rules identical
between C0 and C2. At c8 this commitment ceiling is expected not to bind; verify
and record that fact. Configured capacity, admitted vCPUs, measured CPU use, and
affinity masks are different quantities and must be reported separately.

## 5. Minimal experiment matrix

### 5.1 One-time qualification gate

Before the formal arms, run the current public validation and one small real-VM
qualification through the normal launcher. Verify only facts that can invalidate
the study:

- current C0 leaves the full selected-NUMA mask unchanged;
- current C2 applies to eligible Tool `exec` calls only;
- vCPU-thread effective affinity equals the selected group while active;
- completion/failure restores the full mask;
- `cpuset.mems`, NUMA residency, borrowing, checkpoint/restore, and cleanup
  remain valid;
- PSI and selected-group load are either measured or explicitly unavailable;
- no owned VM, worker, supervisor, or experiment cgroup remains afterward.

Run focused tests once locally and once in the isolated remote checkout. Do not
run the full suite repeatedly. Do not rebuild images, reinstall the host, or
retrain the memory predictor unless an input used by the experiment actually
changed.

### 5.2 Mandatory performance arms

Run the following four arms first, using new experiment IDs and the same current
revision:

| Priority | CPU budget | Arms | Purpose |
|---:|---|---|---|
| 1 | B: 32 logical CPUs | C0 and C2 | Main opportunity test |
| 2 | A: 160 logical CPUs | C0 and C2 | Low-pressure boundary |

Use the existing default C2 settings for the main result:

- CPU PSI ratio: `0.10`
- consecutive samples: `2`
- sampling interval: `1 second`

Do not tune the threshold after seeing throughput and then relabel the winning
setting as the primary result.

### 5.3 Conditional repetition

Avoid routine repetition. Add exactly one reverse-order matched pair for a
budget only when the first pair can materially affect the conclusion, for
example:

- absolute Agents/min or mean-JCT difference is at least about 5%;
- throughput and latency disagree;
- C2 activation differs from the intended mechanism;
- the result would otherwise be presented as a positive or negative paper
  finding rather than a one-run boundary observation.

Use the opposite arm order in the repeat. If the two effects agree in direction
and mechanism activity, report a replicated screening result with its exact
scope. If their signs disagree, report the result as unstable and stop; do not
keep adding repetitions until a preferred sign appears.

If the first pair differs by less than about 5%, all sessions succeed, and no
mechanism anomaly appears, report that no material effect was observed in the
screen and do not repeat it merely to shrink uncertainty.

### 5.4 Conditional mechanism diagnostic

Do not implement C1 or run a threshold sweep by default. Add at most one of the
following diagnostics only if C2 activation is necessary to explain a material
C0/C2 result:

1. **C1 diagnostic:** same per-call initial cluster assignment as C2, with PSI
   relaxation disabled. Prefer this when C2 expands during important long calls.
2. **One threshold sensitivity point:** keep sample count and interval fixed;
   use `0.05` if the default never expands under sustained local pressure, or
   `0.20` if it expands immediately without a meaningful restricted interval.

The diagnostic is explanatory. It must not replace the frozen `0.10` primary
result or be selected because its throughput is better. Do not sweep mask size,
threshold, sample count, density, and CPU budget together.

## 6. Fairness and measurements

Within each matched comparison, hold constant:

- trace bytes and hash, replay timing, prompts, validation, and output checks;
- Runtime and Tool templates, image digests, vCPU and memory sizes;
- memory admission artifact and scale, LOCAL/WARM budgets, borrowing, runtime
  control, checkpoint policy, and lifecycle rules;
- concurrency, arrival schedule, creation limit, timeouts, and result window;
- available CPU pool, NUMA selection policy, CPU weights, and CPU quotas.

Only the optional Tool-call CPU-affinity policy may differ. Policy-specific
monitoring and decision overhead is part of C2's measured cost.

Report at minimum:

- successful Agents/min, offered/completed/failed counts, and success rate;
- mean and P95 Agent JCT and Tool latency;
- workload window and makespan definition;
- experiment cgroup mean cores and `peak_interval_cores` with sampling interval;
- utilization relative to the permitted logical CPU pool;
- Tool-call CPU utilization distribution and Agent Tool-call concurrency;
- per-NUMA and selected-cluster utilization where available;
- CPU PSI samples, threshold streaks, assignment and expansion counts, and
  expansion timing within affected calls;
- effective vCPU-thread masks before assignment, after assignment, after
  expansion, and after release;
- `nr_throttled`, `throttled_usec`, CPU time per successful Agent, and policy
  overhead;
- NUMA memory residency, admission waits, checkpoint/restore, borrowing, OOM,
  telemetry loss, validation, and cleanup status.

Do not interpret the approximately one-second CPU peak as an instantaneous
peak. Do not use assignment counts as CPU demand. Do not infer sensitivity from
historical LLC-load rate alone.

## 7. Failure, bug-fix, and rerun rules

A run is invalid and may be fixed and rerun when any of these affect the arm:

- wrong trace, template, image, CPU budget, or effective configuration;
- validation failure, missing sessions, infrastructure failure, or timeout that
  prevents the intended comparison;
- C2 affinity write/verification failure or mask leakage outside the common
  budget;
- missing required result metadata, corrupted measurement, telemetry loss that
  prevents mechanism interpretation, or incomplete cleanup;
- unequal non-policy settings between matched arms.

The following do not justify rerunning a clean completed arm unless evidence
shows they changed the measured result:

- cosmetic logs, documentation wording, report formatting, or extra diagnostics;
- an unused legacy-path problem;
- a warning in a metric not used for the conclusion;
- a later code cleanup with no behavioral change to the frozen execution path;
- desire for a more favorable sign.

When a non-critical issue exists, record it as a limitation and continue. Keep
all valid positive, negative, null, interrupted, and excluded results. Never
silently weaken policy enforcement or relabel a failed affinity operation as a
fallback.

## 8. Supported execution path

Use only the existing CLI and CubeSandbox workflow. Do not create a second
runner.

Before each run:

1. Inspect `git status`, record the commit and diff, and freeze the spec.
2. Verify host topology and that configured CPU masks are online and permitted.
3. Verify no prior LLC run process, owned VM, perf process, or run cgroup remains.
4. Run `bash scripts/clawbox experiment validate <spec> --inputs` from a fresh
   shell.
5. Launch with a unique run ID using
   `bash scripts/clawbox --sudo experiment launch <spec> --run-id <run-id>`.
6. Generate the standard report with
   `bash scripts/clawbox --sudo experiment report <run-id>`.
7. Archive the frozen spec, arm JSON, relevant event records, report, hashes,
   revision, and cleanup evidence before starting the next arm set.

Do not run overlapping trials on the same CPU or LLC domains. Parallel trials
are allowed only with verified disjoint CPU and LLC domains; otherwise run them
sequentially.

## 9. Decision and deliverables

The smallest acceptable completion consists of:

1. current-revision qualification evidence;
2. one matched current C0/C2 pair at B=32 logical CPUs;
3. one matched current C0/C2 pair at A=160 logical CPUs;
4. only the conditional reverse pair or single diagnostic justified by Section
   5;
5. an analysis that preserves paired signs, mechanism activation, uncertainty,
   limitations, and all negative results.

A single pair may establish that a mechanism executed and show an observed
effect in that run. It does not establish stable speedup. Two opposite-order
pairs with the same direction support a replicated screening claim, not a
universal claim. Opposite signs mean the effect is unstable under the tested
conditions; stop instead of accumulating favorable retries.

If only Budget B improves, scope the conclusion to the restricted CPU regime.
If Budget A is null or negative, treat that as the expected low-pressure
boundary. If neither budget shows a material and repeatable benefit, retain C0
as default and report the tested boundary rather than extending the matrix.

Store auditable verification artifacts separately from user documentation.
Final reporting must include run IDs, commit and artifact hashes, complete
effective configurations, raw result locations, validation and cleanup status,
and a concise statement of what remains untested.
