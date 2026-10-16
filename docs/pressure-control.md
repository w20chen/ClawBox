# Pressure and progress control

Select resource control independently through `policies[].runtime_control`, or
`configure --runtime-control`. New custom policies default to `off`. Existing
named admission/snapshot presets keep their mechanism selections. The alias
`agentcgroup_cgroup` selects the original pinned HIGH/LOW controller.

| Preset / mode | CPU | Memory | Checkpoint coordination |
|---|---|---|---|
| `no_control` | Default 100 | Default low=0, high=max | None |
| `agentcgroup_legacy` | Original grouped shares | Original static HIGH/LOW | None |
| `phase_memcg` | Default | Bounded protection during reclaim | None |
| `release_cpu` | Temporary release-aware weight | Default | None |
| `pressure_lifecycle` | Default | Default | Safe Tool eviction ranking |
| `adaptive_joint` | Release-aware | Bounded protection | Safe Tool eviction ranking |

The six named presets use Tool admission off; lifecycle admission remains the
existing configured policy. For a prediction-admission comparison, explicitly
set the same `admission: predicted` and artifact in each compared policy.
The last two presets explicitly select `snapshot_pause`, `wait_aware_pressure`,
and reactive restore. Selecting a runtime-control mode alone never enables
snapshots, borrowing, migration, or a different placement algorithm.
`pressure_lifecycle` rejects resident-only configuration. `adaptive_joint` can
be used with resident VMs, in which case its lifecycle mechanism is inactive.

All new modes use flat, stable per-VM leaves prepared before guest allocation.
Runtime and Tool keep separate leaves through restore. The controller leaves
cpuset and `memory.max` untouched. Legacy keeps its original hierarchy and
arbitrary HIGH session, so its comparison includes the historical grouping.

The state tracker uses execution and model request IDs. Admission marks READY;
Toolbridge reports START after releasing the guest execution gate and END after
process exit. The Runtime wrapper releases admission only after SSH reap.
CPU action also requires measured CPU use and CPU PSI, and never follows admission alone.
LLM_WAITING requires an outstanding model request with no Tool in flight.
Repeated terminal events and delayed starts cannot reopen a completed ID.
Pre-start/restore preparation marks TRANSITION; verified VMM creation advances
the generation. Missing completion keeps the Tool ineligible for eviction until
normal failure cleanup. A waiting Runtime is never suspended by the new policy.

Capacity release follows this chain: a LOCAL admission deficit selects an idle
Tool with measured LOCAL charge and sufficient predicted idle time; lifecycle
atomically rechecks eligibility and checkpoints it; the leaf's measured charge
drop is recorded; admission retries through its existing condition and queue.
The ranking is LOCAL bytes divided by conservative checkpoint-plus-restore cost.
Costs come from successful lifecycle timings or the existing measured
`resources.memory_action_costs` with provenance. Missing costs cause abstention.
Idle time uses the configured historical model-wait estimate, never an upcoming
trace duration. Supply an estimate from a separate training trace; do not use
`--model-wait-prediction-seconds auto` on the evaluation trace for these modes.
After a release, cooldown prevents immediate repetition. Failure to release the
minimum bytes disables that session as a release candidate for the run.

CPU redistribution follows this chain: a LOCAL blocked request plus actual CPU
PSI selects an active Tool whose same-template, same-command history repeatedly
released transient host charge; a temporary weight increase can shorten the
resource-blocking interval. At least three successful observations are required.
The estimate is the minimum observed transient release, excluding persistent VM
baseline; durations must agree within a factor of two. Estimates expire at the
shortest observed duration, and candidates rank by bytes / remaining seconds.
Borrowed/multi-node memory is excluded from this CPU heuristic. ClawTune's peak
memory prediction alone cannot establish release; unavailable evidence retains
weight 100. Measured release is observational, not proof of throughput benefit:
compare action counts, blocking time and completed Agents/s across repetitions.

Memory protection follows this chain: measured memory PSI plus reclaim scans in
an active Tool selects a bounded resident working-set protection; less reclaim
may reduce stalled execution. The sum of leaf `memory.low` settings is bounded
by the configured budget. Protection is relative within the live-VM parent;
it is not a host-wide memory reservation. Continuing reclaim at the end of the
action window disables further protection for that execution. CPU and memory
controls reset on completion, loss of relevant pressure, timeout, shutdown and
controller error. No `memory.high`, `memory.reclaim`, freezing, or hard-limit
adjustment is used by the new controller: safe avoidable-growth evidence is
not presently available. Existing cache reclamation remains a separate policy.

Configuration under `resources.pressure_control` is editable YAML:

| Field | Default | Meaning |
|---|---:|---|
| `psi_ratio` | 0.10 | Fraction of sampled wall time stalled, (0,1] |
| `exit_hysteresis_seconds` | 1 | Capacity-pressure decay window |
| `cooldown_seconds` | 10 | Minimum interval after bounded control/release |
| `action_window_seconds` | 2 | Maximum continuous CPU/protection action |
| `protection_budget_mib` | 256 | Aggregate leaf memory.low budget; 0 disables |
| `max_boosted_vms` | 1 | Maximum simultaneous boosts, 0–4 |
| `boost_cpu_weight` | 200 | Temporary weight, 100–400; normal=100 |
| `min_release_mib` | 64 | Minimum observed/expected LOCAL or transient release |
| `amortization_factor` | 2 | Predicted idle / checkpoint-plus-restore cost minimum |
| `release_history_samples` | 3 | Successful observations required, 3–8 |

The control clock uses `resources.agentcgroup_poll_interval_seconds` (default
0.5 seconds). The other `agentcgroup_*` thresholds affect legacy only. These
settings require a new run ID, no host apply, retraining or image rebuild. Initial
adoption requires a Tool image built with the progress-enabled Toolbridge
(`clawbox experiment images --profile host.json --role sandbox`, preserving the
workload-specific build recipe). Bootstrap checks this capability explicitly;
older bridges cannot silently run an optimized arm. The updated SSH wrapper is
installed by the existing run bootstrap. Existing host
prerequisites remain: cgroup v2 CPU/memory/cpuset delegation, verified Cube
preallocation routing, and snapshot support when selected. Missing required
files fail the run; missing PSI disables the corresponding optimization and is
reported in controller provenance.

Start from a complete, validated custom experiment YAML and its matching host
profile (do not use a suite-owned recipe to override its fixed mechanisms):

```sh
clawbox experiment configure base.yaml pressure.yaml --profile host.json \
  --baseline no_control --baseline agentcgroup_legacy \
  --baseline phase_memcg --baseline release_cpu \
  --baseline pressure_lifecycle --baseline adaptive_joint \
  --memory-scheduling static --time-scale 1 --repetitions 3
clawbox experiment describe pressure.yaml
clawbox --output-root ./results experiment launch pressure.yaml --run-id pressure-01
```

Review generated policies, saved template identities, snapshot paths/capacities,
LOCAL budgets and CPU lists before launch. `static` allows explicit mixed
resident/snapshot policies without enabling shared borrowing; set shared live
borrowing to zero/null in this comparison. Keep arrival schedule, trace, VM
shape, placement, and memory boundaries identical between arms. Run separate
configurations for low pressure, LOCAL admission saturation, and combined CPU
and memory contention. Establish each regime from measured deficits/PSI; offered
concurrency alone does not establish contention. Preserve recorded waits with
`time_scale: 1`. Do not replace commands with sleeps.

Use normal launch/status/resume/abort commands for interruption recovery. The
supervisor removes only journaled leaves after their VMs exit. A live populated
leaf is never recursively removed. A repeated configure needs a new output or
explicit `--force`; a repeated experiment needs a new run ID or normal resume.

Results include `runtime_memory_control` counters/provenance, per-node
`pressure_release_observations`, and `pressure_control_action` /
`pressure_execution_release` events. Compare completed Agents/s first, Tool
steps/s, Agent P50/P95, admission waits, PSI/reclaim/fault/OOM, resident memory,
release bytes, snapshot/restore time and controller CPU time. A zero-action arm
is a negative or inconclusive optimization result, not evidence of improvement.
Keep validation results and performance tables outside this user guide.
