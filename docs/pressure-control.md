# Pressure and progress control

New experiments use independent `mechanisms.runtime.memory_protection`,
`cpu_release_boost`, and `mechanisms.idle_reclaim.policy` fields. Use
`--recipe runtime` for the four runtime arms, or `--recipe idle-reclaim` for
the three lifecycle arms. The runtime recipe pins proactive memory reclaim off;
use `--recipe memory-reclaim` to compare that control separately. Other mechanism
groups remain common across these comparisons. See [the six-dimension guide](experiments.md).
The mode names below describe the backend and schema-3 records.

Select resource control independently through `policies[].runtime_control`, or
`configure --runtime-control`. New custom policies default to `off`. Existing
named admission/snapshot presets use neutral per-VM groups.

| Mode | CPU | Memory | Checkpoint coordination |
|---|---|---|---|
| `no_control` | Default 100 | Default low=0, high=max | None |
| `phase_memcg` | Default | Bounded protection during reclaim | None |
| `release_cpu` | Temporary release-aware weight | Default | None |

The modes use Tool admission off in their legacy presets; `no_control` remains
an explicit runtime mode but is not a second public baseline because its execution
is identical to `uncontrolled-resident`. Lifecycle admission remains the existing
configured policy. For a prediction-admission comparison, explicitly set the same
`admission: predicted` and artifact in each compared policy.
Idle checkpoint is configured independently with `idle_reclaim.policy: wait`.
LLM-duration prediction, oracle scheduling, and the old `pressure_lifecycle` /
`adaptive_joint` presets are unsupported. Tool memory prediction and CPU release
history are separate mechanisms and remain available.

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
normal failure cleanup.

Idle reclamation requires a real outstanding model request, no active Tool,
and memory pressure. `scope: tool` reclaims Tool only; `agent_and_tool` reclaims
both VMs. All ordinary pressure paths use the configured action selector.
Disabled borrowing is excluded from candidate inspection and cost selection.
WARM capacity for the entire scope is reserved before either VM pauses. A full
WARM-only pool causes backpressure, while actual checkpoint failures propagate.
A generated response removes eligibility immediately; any committed pair pause
finishes before Runtime is restored and the response is delivered. Tool restores
at its next admission. No predicted remaining LLM time is consulted.

CPU redistribution follows this chain: a LOCAL blocked request plus actual CPU
PSI selects an active Tool whose same-template, same-command history repeatedly
released transient host charge; a temporary weight increase can shorten the
resource-blocking interval. At least three successful observations are required.
The estimate is the minimum observed transient release, excluding persistent VM
baseline; durations must agree within a factor of two. History is collected
within each arm run; it is not loaded from other runs. The estimated completion
time uses the median historical duration. Forecasts remain eligible for
`release_forecast_ttl_factor` times the longest historical duration from execution
start (default 3), so a CPU-delayed execution can still receive help. Candidates
rank by release bytes / estimated remaining seconds, with the denominator floored
at one action window. The validity deadline is not a predicted completion time.
Each poll rechecks CPU contention, blocked admission, VM generation, LOCAL placement
and current growth of at least `min_release_mib` above the admission baseline.
Borrowed/multi-node memory is excluded from this CPU heuristic. ClawTune's peak
memory prediction alone cannot establish release; unavailable evidence retains
weight 100. Measured release is observational, not proof of throughput benefit:
compare action counts, blocking time and completed Agents/s across repetitions.
Guest process exit or guest free memory is not evidence of host charge release.
If the VM retains the pages, this heuristic does not treat them as released.

Memory protection follows this chain: measured memory PSI plus reclaim scans in
an active Tool selects a bounded resident working-set protection; less reclaim
may reduce stalled execution. The sum of leaf `memory.low` settings is bounded
by the configured budget. Protection is relative within the live-VM parent;
it is not a host-wide memory reservation or protection of a guest process's
working set. The estimate is `min(memory.current, anon + active_file)` from the
host VM cgroup. Benefit requires host-reclaimable pages and host reclaim stalls;
guest-only memory pressure is not a trigger. LOCAL disables swap, so anonymous
guest RAM is not ordinary swappable host capacity. At the end of each action
window, protection resets and enters cooldown. After cooldown it may be applied
again if reclaim pressure persists; residual pressure alone does not prove that
protection was ineffective. CPU and memory
controls reset on completion, loss of relevant pressure, timeout, shutdown and
controller error. No `memory.high`, `memory.reclaim`, freezing, or hard-limit
adjustment is used by the new controller: safe avoidable-growth evidence is
not presently available. Existing cache reclamation remains a separate policy.

Configuration under `mechanisms.runtime.pressure_control` is editable YAML
(`resources.pressure_control` in backend/schema-3 specifications):

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
| `release_forecast_ttl_factor` | 3 | Finite multiplier >1 of the longest historical duration; forecast validity from execution start |

The control clock uses `resources.runtime_poll_interval_seconds` (default
0.5 seconds). These settings require a new run ID, no host apply, retraining or image rebuild. Initial
adoption requires a Tool image built with the progress-enabled Toolbridge
(`clawbox experiment images --profile host.json --role sandbox`, preserving the
workload-specific build recipe). Bootstrap checks this capability explicitly;
older bridges cannot silently run an optimized arm. The updated SSH wrapper is
installed by the existing run bootstrap. Existing host
prerequisites remain: cgroup v2 CPU/memory/cpuset delegation, verified Cube
preallocation routing, and snapshot support when selected. An enabled memory
protection / CPU boost mechanism requires readable `memory.pressure` /
`cpu.pressure` in the parent and Tool cgroups, respectively. Missing PSI fails
host readiness before VM probes and is checked again at controller start and
Tool registration; it never silently disables a selected mechanism. Baselines
with both controls off do not require PSI. On a kernel built with `CONFIG_PSI=y`
and `CONFIG_PSI_DEFAULT_DISABLED=y`, enable `psi=1` through the host bootloader
configuration, reboot, and verify the cgroup files before launching. This host
change requires a reboot, not an image rebuild or retraining.

Start from a configured schema-4 workload:

```sh
clawbox experiment configure base.yaml pressure.yaml --recipe runtime \
  --time-scale 1 --repetitions 3
# Optional: edit mechanisms.runtime.pressure_control.release_forecast_ttl_factor
# in pressure.yaml; keep the same value across compared arms.
clawbox experiment describe pressure.yaml
clawbox --output-root ./results experiment launch pressure.yaml --run-id pressure-01
```

Keep other dimensions, arrival schedule, templates and physical limits fixed.
For lifecycle comparisons use `--recipe idle-reclaim` and supply independent wait
and checkpoint-cost evidence. Compare low pressure, LOCAL saturation and combined
CPU/memory contention using measured deficits and PSI. Preserve real commands and
recorded waits. CPU boost needs repeated commands with observed host charge
release as well as CPU contention and admission blocking; memory protection
needs host reclaim stalls. PSI availability alone does not establish these
conditions. Zero actions do not establish an optimization benefit.

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
