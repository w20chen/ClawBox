# Experiment configuration, commands, and results

Use the [step-by-step guide](self-service.md) for the full command sequence
and [host configuration](supernode.md) for NUMA and shared-memory settings.
This page explains the experiment YAML, command options, and result files.

## Configuration files and units

| File | Purpose | How to change it |
| --- | --- | --- |
| `~/.config/clawbox/machine.env` | API addresses, guest-reachable host addresses, and source/build paths | Edit and `source` in each shell; the CLI does not load it automatically |
| Host YAML | Desired NUMA layout, capacities, storage, and templates | Edit, then run `host check` and `host apply` |
| `~/.config/clawbox/host.json` | Profile written after a successful `host apply` or `images` update | Read by `configure`; do not edit as the primary host plan |
| Experiment YAML | Workload, VM identity, policies, concurrency, and limits | Use `configure BASE OUTPUT`, or edit and run `validate --inputs` |
| `RUN/experiment.yaml` | Frozen configuration for a started run | Keep it; use a new run ID for changed parameters |

`configure` reads the base YAML, applies values from an existing host
profile, then applies explicit CLI options. `--profile FILE` selects
another profile. If it does not exist, `configure` retains the base
file's relevant values; it does not configure the host. `--force`
permits overwriting the output YAML, not an existing run.

Host capacities use whole GiB. Experiment resource capacities, VM memory,
and reservations use MiB. Result fields ending in `_bytes` use bytes;
1 GiB is 1024 MiB. CLI options ending in `-gib` convert GiB to MiB.
Relative paths resolve from the command's working directory. Use
absolute paths for trace, prediction, and result files when possible.
`--output-root` is a top-level option before `experiment`; it
selects the CLI's result directory, independently of
`output.directory` in the experiment YAML.

## Experiment suites

Use `configure BASE OUTPUT --suite admission` or `--suite numa-snapshot`.
The default is a two-arm matched comparison. `--ablations` adds intermediate
arms; `--no-ablations` restores the main comparison. The base supplies workload,
model and template settings; the applied host profile supplies topology and
budgets. Reusing a generated YAML preserves its suite. Select a new suite to
change the research question, then use a new run ID.

| Suite | Main baseline | Proposed arm | Extra arms with `--ablations` |
| --- | --- | --- | --- |
| `admission` | OS-managed resident VMs, no Tool admission or AgentCgroup | ClawTune prediction + command admission | Fixed allowance and AgentCgroup-only |
| `numa-snapshot` | OS placement and resident VMs, no ClawBox memory scheduling | Load-aware placement, idle snapshot reclaim, template affinity and snapshot value ranking | Load-aware + LRU, template-only, snapshot-only |

The baseline is a deployment reference, not a hand-selected admission policy.
Admission arms use the same OS placement, resident VMs and OS-managed creation.
The NUMA default baseline inherits the host VM domain's CPU/memory placement and
uses the same combined live cgroup limit (LOCAL plus configured live-borrow allowance); it has no
ClawBox borrowing limit, proactive cache reclaim or snapshot scheduling. It
retains the host's cgroup limits, emergency free-memory guard and CubeSandbox
checks. WARM snapshot capacity is not added to that live limit. The managed NUMA arms partition LOCAL and WARM,
using reclaim-then-borrow when a borrow budget exists, otherwise snapshot-only.
Thus the main NUMA comparison evaluates the complete scheduling design; compare
its managed ablations to isolate affinity and snapshot ranking individually.

AgentCgroup is optional: `--runtime-control off` (suite default) or
`--runtime-control agentcgroup_cgroup`, saved as `suite_runtime_control`.
The deployment baseline stays off. In the admission suite, enabling it adds
AgentCgroup to the proposed arm; `--ablations` also includes AgentCgroup without
admission. Custom recipes can set each `policies[].runtime_control` independently.
The CLI option applies to all policies in a custom recipe.
AgentCgroup's public tuning knobs are the polling interval, `memory.high`
event threshold, parent usage ratio, and protection-window duration. Their
defaults are 0.1 seconds, 1 event, 0.85, and 1 second. A less aggressive
follow-up can increase the threshold/ratio or shorten the window. Use a new
run ID; these changes need neither host apply nor an image rebuild.

The optional fixed comparator defaults to measured host-growth P90 from the
validated training artifact; `--static-tool-memory-mib` can tune it. It is not
the default-deployment baseline. Freeze calibration independently of evaluation.
One prediction artifact currently covers one repository and Tool environment;
run separate admission suites for different environments.

The admission suite defaults to `--prediction-mode runtime`: before each Tool
command, the host queries the trained ClawTune Lattice/Tool KB, then maps the
guest prediction to host growth using measured calibration. New commands need
not appear in a precomputed replay list. Keep the matching `NAME.json` and
`NAME.kb.json` from `experiment train` together; the KB hash is verified. Model
state remains frozen during evaluation. `--prediction-mode frozen` retains
exact-command replay predictions. Neither mode invents a zero for missing data;
unavailable predicted admission fails with the model's reason.

Tune `--prediction-reservation-scale` (positive multiplier, default 1) and
`--admission-margin-mib` (MiB, default 256). Reservation is
`ceil(calibrated_host_growth_mib * scale)`, at least 1 MiB. Lower values reduce
conservatism but can increase pressure/OOM; compare completion rate, memory
pressure and queue time together. The physical budget and measured outstanding
growth checks still apply. These edits need a new run ID, not host apply, image
rebuild or retraining. YAML `resources.observe_predictions: true` collects
predictions even with admission off; supply the artifact and prediction mode.

The NUMA suite requires compute nodes, positive WARM capacity, a COLD path,
measured `resources.snapshot_residency_costs` and each case's explicit
`snapshot_reuse_probability`. Those measurements and probabilities remain
editable YAML inputs: the system does not invent them. Template sharing is
qualified automatically. If measured COLD restore is no slower than WARM,
`configure`/`describe` warns that all retention values are zero; retain this as
a negative result rather than claiming a WARM advantage.

If qualification fails during external-Base WARM-to-COLD relocation, inspect
the reported probe `result.json` and `cleanup_errors`. Verify storage capacity
and install the matching Cubelet/Shim/SDK patch set following
[installation](installation.md), then rerun `experiment qualify`. Updating
the Python package does not update the snapshot backend. Do not switch snapshot
mechanisms or bypass qualification to make a failed comparison run.

Suite YAML can omit `policies`; the suite generates them. `describe` and `plan`
show the resolved comparison, and the run freezes the **full** effective
configuration. Conflicting policy edits fail validation. `configure` replaces
the base's policy recipe with the selected suite and prints the result for
review; explicit conflicting mechanism options fail. Multi-node total budgets
are derived from the applied per-node budgets.

For training, smoke tests or a specific diagnostic, use `--baseline` without a
suite. Available recipes are `default-resident`, `fixed-resident`, `runtime-resident`,
`predicted-resident`, `uncontrolled-resident`, `prediction-only-resident`, and
`predicted-wait-snapshot`. Use `default-resident` for ordinary deployment and
training; the other recipes are useful controlled references. Independent custom policy fields remain
available in YAML without `suite`; supported eviction is `none` or
`wait_aware_pressure`, and restore is `none` or `reactive`. Oracle, fixed-delay
and proactive-restore policies are rejected at schema validation.

The unmodified upstream `memcg_controller.py` from commit
`551ca1689b7d2d30db6b8a8613414750103cc68b` is bundled and hash-checked.
Its [provenance and license](../clawbox/experiments/vendor/agentcgroup/PROVENANCE.md)
are included with the source and Python package.
`resources.agentcgroup_source` optionally selects an external copy with that
same hash. This selects the official **cgroup v2 fallback** explicitly.
`agentcgroup_bpf` is rejected until a patched kernel and descendant fault
accounting have been qualified; it never silently selects the fallback.
The upstream sched_ext CPU scheduler is not enabled. HIGH/LOW CPU weights
are proportional to offered session counts and identical across policies,
avoiding an accidental 50/50 CPU split between one HIGH session and all LOW
sessions. These are group shares, not CPU reservations; nested groups and
disjoint CPU masks can still differ from a flat CPU scheduler under contention.

Apply the host budget before launch: `resources.local_memory_cgroup` defaults
to `/sys/fs/cgroup/cube_sandbox/sandbox` and must have finite `memory.max` and
delegated memory, CPU and cpuset controllers. Install the Cubelet/Shim builds
from the complete `prepare-semantic-source.sh` patch set. Run launch, qualify,
abort and destroy as root, preserving the configured environment, for example
`sudo -E "$(command -v clawbox)" --output-root "$CLAWBOX_OUTPUT_ROOT" experiment launch ...`.
The worker writes root-only cgroup capabilities under `/run/clawbox/cgroups`.
Concurrent experiments in the same physical cgroup are rejected.

Each repetition assigns exactly one HIGH session, rotating through session
indices across repetitions. The assignment is identical across policies at
the same concurrency; use a multiple of the concurrency as the repetition
count for balanced priorities. The remaining sessions share one LOW group,
matching the upstream daemon's aggregate LOW layout. Both agent and tool VMs
keep their session's priority during commands, model waits and restore.
Creation and restore enter the leaf before guest RAM allocation. Per-node
CPU/NUMA placement, shared borrowing and snapshot storage remain separate;
the controller uses one physical budget spanning all simulated compute nodes.

The fallback runs every 100 ms, including periods with no active tools. Its
upstream triggers are HIGH `memory.events.high`, parent PSI, or parent usage
at least 85% of `memory.max`. Protection lasts one second: HIGH `memory.low`
becomes 80% of the parent budget and aggregate LOW `memory.high` becomes 50%.
Normal values are 0 and `max`. Tool completion never resets this state.
On kernels without PSI the other two upstream signals remain active and
results report `psi_available: false`. Required control-file failures fail
the arm. These controls throttle growth and influence reclaim; they do not
guarantee host RAM release when a guest frees memory or guarantee no OOM.

Predicted and fixed admission use the same physical-demand ledger. Admission
checks measured charges plus growth not yet observed, the candidate, and
`resources.admission_margin_mib` (default 256 MiB) against the global budget.
It also checks the home-node budget. A shared allocation is credited only after
the live-borrow pool has granted it. Completed calls release pending demand;
retained resident pages remain in the next measurement.

A request that fits proceeds immediately. Excess predicted demand queues before
execution; observed sustained congestion also closes the gate (90% occupancy
with at least 10% PSI stalls or heavy ineffective reclaim), reopening when
committed demand falls to 80% or severe pressure has subsided for one second.
The demand/capacity check still applies after reopening; retained VM backing
alone must not leave the pressure gate closed forever. These are admission heuristics, separate from the
unchanged upstream controller, and do not guarantee no OOM. Requests that cannot
make progress expire at the configured deadline; no oversized request is admitted
merely because the active set is empty. `admission: "off"` bypasses this tool gate
and does not load prediction artifacts unless `observe_predictions` is enabled.
`startup_headroom_mib` defaults to 256 MiB in configured-capacity lifecycle mode;
OS-managed creation has no capacity reservation or measured-budget queue.

`experiment train` emits `clawbox_prediction_v2`: frozen ClawTune guest P50
predictions plus a measured guest-to-host cgroup increment calibration from a
separate validated training run. Host cgroup growth is sampled alongside guest
measurements in every policy. RSS is reported separately. Guest values are never
implicitly treated as host memory. Missing predictions/calibration fail explicitly;
short duration without memory observations does not establish a zero estimate.
Recollect training data when the artifact lacks this measurement contract.

Choose a suite or explicit diagnostic recipes. `--runtime-control` switches
AgentCgroup independently; admission and reclamation mechanisms are selected by
the recipe, with additional tuning available in YAML.

`--static-tool-memory-mib auto` uses the measured 90th percentile of
host cgroup execution growth from a validated training run. The
`--model-wait-prediction-seconds auto` value comes from the current
trace's median model-call duration, scaled by `time_scale`. Both
values and their sources are frozen when the YAML is generated; a
formal run does not train them online.

## Experiment YAML fields

Fields without CLI options can be edited directly. The format requires
`schema_version: 3` and rejects unknown fields. See
[spec.py](../clawbox/experiments/spec.py) for the complete schema. Use
`describe` to inspect the effective configuration; class defaults
do not necessarily match a given base YAML.

| YAML field | Meaning or CLI option |
| --- | --- |
| `suite`, `suite_ablations` | `--suite admission\|numa-snapshot`, `--ablations`; two main arms by default |
| `workload.repetitions` | `--repetitions`; repeats each task/concurrency/policy combination |
| `workload.cases[]` | Explicit tasks with source/repository/trace fields, optional validation, optional complete Tool `sandbox`, and optional `snapshot_reuse_probability` (0–1). Missing case sandbox uses the global `sandbox`. |
| `workload.session_assignment` | `--session-assignment single_case` gives each task separate trials; `round_robin` distributes at least two explicit tasks among sessions in a trial |
| `execution.concurrency_levels` | `--concurrency 1,2,4`; offered sessions, each with two VMs, not the measured number executing simultaneously |
| `execution.placement_policy`, `session_compute_nodes` | `--placement-policy`: `round_robin` (default), `explicit`, `os_default`, or `load_aware`. `--session-compute-nodes` supplies explicit compute-node mapping, separate from task assignment. Runtime and Tool share the selected node. |
| `execution.placement_cpu_overcommit` | YAML field; finite ratio >= 1, default 8 offered vCPUs per compute CPU. Admission counts both VMs for the entire session. This is a commitment ceiling, not measured CPU use. |
| `execution.template_balance_slack` | YAML only; normalized pressure window (default 0.10) within which verified template affinity may break a tie |
| `execution.randomized_order`, `random_seed` | Trial order is randomized by default; `--random-seed` fixes the seed; edit YAML to change the order switch |
| `execution.arrival_schedule`, `stagger_interval_seconds` | `--arrival-schedule burst` or `fixed_stagger`; the latter needs a positive `--stagger-seconds` |
| `execution.arm_timeout_seconds` | `--arm-timeout-seconds`; positive integer limit for the entire trial; cannot be `null` |
| `execution.command_timeout_seconds` | `--command-timeout-seconds`; command execution deadline; admission wait is timed separately but remains within the trial deadline |
| `execution.memory_sample_interval_seconds`, `stabilization_seconds` | YAML only; seconds, defaults 0.2 and 1 respectively |
| `runtime`, `sandbox` | Agent/tool VM templates, image digests, `vcpu`, `memory_mib`, workspace, preflight command, and internet setting |
| `resources.pool_memory_budget_mib` | `--pool-memory-gib`; with multiple compute nodes, must equal the sum of their high watermarks; normally copied from the host profile |
| `resources.emergency_free_memory_mib` | `--emergency-free-memory-gib`; host-wide available-memory safety floor |
| `resources.checkpoint_restore_headroom_mib` | `--checkpoint-headroom-gib`; per-node allowance for snapshot operations, charged only to checkpointing policies |
| `resources.static_tool_memory_mib` | `--static-tool-memory-mib`; per-command reservation for the fixed policy, not VM RAM |
| `resources.non_command_tool_memory_mib` | Fixed reservation for non-command tools; defaults to the static tool reservation when unset |
| `resources.prediction_artifact` | `--prediction-artifact`; frozen prediction file from an independent training run |
| `resources.prediction_mode` | `--prediction-mode runtime\|frozen`; admission suite defaults to runtime, custom recipes to frozen |
| `resources.observe_predictions` | YAML boolean, default false; query predictions without using them for admission |
| `resources.prediction_reservation_scale` | `--prediction-reservation-scale`; positive host-growth multiplier, default 1 |
| `resources.admission_margin_mib` | `--admission-margin-mib`; nonnegative global reserve, default 256 MiB |
| `suite_runtime_control` / `policies[].runtime_control` | `--runtime-control off\|agentcgroup_cgroup`; suites default off; custom policy fields are independent |
| `resources.agentcgroup_poll_interval_seconds` | `--agentcgroup-poll-interval-seconds`; positive seconds, default 0.5 |
| `resources.agentcgroup_event_threshold` | `--agentcgroup-event-threshold`; positive `memory.high` event count, default 1 |
| `resources.agentcgroup_pressure_ratio` | `--agentcgroup-pressure-ratio`; parent current/max ratio in (0,1), default 0.85 |
| `resources.agentcgroup_protection_window_seconds` | `--agentcgroup-protection-window-seconds`; positive seconds, default 1 |
| `policies[].deployment` | `managed` (default) or `os-default`; the latter resolves to OS placement, resident VMs, no admission/controller/cache reclaim, using the applied VM domain |
| `resources.snapshot_mechanism` | `--snapshot-mechanism incremental-cow` or `full-copy` |
| `resources.snapshot_storage` | `--snapshot-storage warm-only` or `tiered`; the latter may use disk, whose path must match the service setting |
| `resources.template_base_sharing` | YAML only for diagnostics; the NUMA suite requires sharing qualification automatically. |
| `resources.snapshot_residency_costs` | Measured WARM/SSD restore seconds per GiB plus provenance; one YAML object |
| `policies[].template_aware_placement` | Generated by the suite; custom YAML requires load-aware placement and verified sharing |
| `policies[].snapshot_aware_residency` | Generated by the suite; custom YAML requires tiered storage, measured costs, and per-case reuse probability |
| `validation.command` | `--validation-command`; final task check executed in the tool VM; required explicitly for training |

To change vCPUs, guest RAM, or writable disk size, register a new VM
template as described in [installation](installation.md#4-register-templates-and-verify-ssh)
and update the host YAML. `configure --runtime-vcpu`,
`--tool-vcpu`, and the memory options do not resize registered templates.
Likewise, changing NUMA nodes, CPU lists, shared-pool size, or disk
paths requires `host apply` before generating a new experiment YAML.

`os_default` inherits the host VM parent's CPU and memory masks without narrowing
each VM to one NUMA node. Unbound allocation cannot enforce a separate live-borrow
budget. The NUMA suite's OS deployment uses the existing parent domain and combines
its LOCAL and live-borrow allowance under the unchanged host cgroup hard limit;
managed arms retain their partition. Custom `os_default` recipes must remove the
live-borrow limit. No arm rewrites the host's physical limits.
`load_aware` chooses a feasible node using normalized measured CPU utilization,
scheduling pressure when available, offered vCPU commitments, resident memory,
and outstanding admission claims. Selection and the existing pair-memory claim
are atomic. Placement remains fixed through restore. Bandwidth is not a placement
input. Scheduling pressure is null when Linux `kernel.sched_schedstats` is disabled;
the scheduler still uses CPU utilization and commitments. Samples and
`session_placement` events distinguish measured usage from commitments.
Changing placement policy or CPU overcommit needs a new run ID, but no image
rebuild, host apply, or prediction retraining when host resources are unchanged.
`template_aware_placement` requires `load_aware` placement; it never switches
the base placement algorithm implicitly. It preserves admission, AgentCgroup, CPU weights and
post-restore placement. A reported template cache hit means a verified
same-template peer existed; it is not an inferred byte saving.

## Workloads and replay traces

The supported end-to-end path uses `agent.driver: openclaw` and
`inference.backend: replay`. Some configuration and worker code accepts
`api`, but the `launch` preflight currently requires replay. The
public CLI has no `record` subcommand.

Replay needs a nonempty task prompt, a model name, and JSONL records
with model requests and responses. The
[short trace](../examples/traces/smoke.jsonl) and
[memory trace](../examples/traces/memory-smoke.jsonl) show schema 6:
a `span_start` has `input`, while its matching `span_end` has
`output`, `duration_ns`, and `status`. `kind: llm` and
`sequence_no` identify model steps. Response `tool_calls` trigger
real tool execution; each `function.arguments` value is a JSON
string and must name a tool supported by the installed OpenClaw version.

```bash
clawbox experiment import-trace /data/source-trace.jsonl --output /data/replay.jsonl \
  --source-workspace /testbed --target-workspace /workspace
clawbox experiment trace /data/replay.jsonl
clawbox experiment validate /data/eval.yaml --inputs
```

`import-trace` converts recognized research traces; it does not install
task dependencies or create an initial workspace. Prepare the
repository, starting revision, and dependencies in the tool VM image.
The importer maps the source image's workspace into OpenClaw's canonical
`/workspace`; defaults match SWE-ReBench (`/testbed`) and the supported
OpenClaw workflow (`/workspace`). Set `sandbox.workspace` to the real repository
directory in the Tool VM image. Changing either import path requires importing a
new trace; changing `sandbox.workspace` requires a new run, not an image rebuild.
`configure --tool-workspace /testbed` sets that field without changing the VM
template or its 2 vCPU/4 GiB rental-sized shape.
For one task, use `configure --trace --prompt --repository --base-commit`
to replace the base input. For multiple tasks, fill in
`workload.cases` in YAML. Each case's `source` must match
`workload.source`. Explicit cases take precedence over
`workload.input`; otherwise that field is read according to source type.
The case-level `sandbox` is a complete Tool VM description, not a partial merge.
Its image must contain the recorded repository, revision, dependencies,
workspace and commands. Qualification executes the first real Tool call for
every case and snapshot round-trips every distinct Tool template on each node.

A complete replay ends with a model response that makes no further
tool request. A positive `inference.configuration.max_model_steps`
runs only a prefix, which is insufficient as a full training run.
`--time-scale` scales recorded model wait; policy-induced delays
are measured separately. Actual tool output is retained rather than
replaced with recorded text.

Training requires a successful completed run using no admission or fixed admission,
complete measurements, final task validation, verified cleanup,
and the same repository identity. Missing observations are not treated
as zero. Prediction loading checks the tool image digest, vCPUs, memory,
and architecture; `validate --inputs` also checks coverage of recorded
commands. Inspect the prediction file's `unavailable` entries and
collect valid guest-memory samples for missing commands.

## Commands and preflight checks

All commands below are subcommands of `clawbox experiment`; each
supports `--help` for its complete arguments.

| Command | Purpose |
| --- | --- |
| `trace FILE`, `validate SPEC --inputs`, `describe SPEC`, `plan SPEC` | Inspect trace, inputs, effective resources, and planned trials without creating VMs |
| `host inspect/init/check/apply` | Inspect topology, create host YAML (`init --detect` proposes settings from this host and machine.env), check prerequisites, apply host settings, and probe a VM |
| `host clean [--apply]` | Preview or delete recognized old crash dumps; defaults: 24 hours and 100 MiB; `--root` selects dedicated crash directories |
| `setup` | Lower-level single-compute-node setup; multi-node configuration uses the host YAML |
| `images` | Rebuild guest integration, register templates, and update the host profile; see installation |
| `doctor SPEC [--probe-vm]` | Check services, resources, templates, and revisions; it may refresh snapshot storage; `--probe-vm` creates, executes in, and deletes a VM |
| `qualify SPEC` | Run the real preflight checks separately; defaults to `SPEC.qualification.json` |
| `launch SPEC --run-id ID [--detach]` | Check inputs and host, run preflight if needed, then start or resume; also available through `configure --launch` |
| `run SPEC`, `resume ID` | Lower-level start/restart requiring an existing valid preflight record |
| `status ID` | Show whether the supervisor is alive, current trial, progress, and final state |
| `report ID`, `collect ID` | Regenerate the report and memory time series, or read the existing `summary.json`; neither reruns the workload |
| `abort ID`, `destroy ID` | Stop or clean up VMs owned by that run while retaining result files |

Preflight probes the required snapshot and shared-memory operations on
each compute node, then executes the first model step under each selected
policy and workload case. That step must request a tool. Its `true` validation command
tests the execution path; the formal trial still uses the task's own
validation. Preflight concurrency defaults to the experiment's maximum.
A record made with a lower `--qualification-concurrency` cannot
cover a higher-concurrency experiment. The record is tied to the full
specification, implementation revision, templates and images, nodes,
policies, concurrency, and the SHA-256 contents of referenced replay traces
and prediction files. `launch` reruns preflight when those change;
`--force-qualify` requests it explicitly. Runtime-control qualification also
requires real kernel activation and LOW throttling probes, including measured
guest allocation growth under the default upstream signals, plus HIGH/LOW
placement of both VM roles, their configured snapshot restores, and a forced
WARM-to-SSD relocation/restore when tiered WARM storage is enabled. A host
allocation probe does not establish guest-to-host free-page reclamation.
When `template_base_sharing: required`, preflight also creates two VMs per
distinct Tool template and stores smaps evidence for a common large mapping,
including `Shared_Clean` and PSS. Without this receipt, template affinity is
not a supported physical-sharing optimization.

### Heterogeneous rollout ablation on the qualified Kunpeng host

[`agent-rl-heterogeneous-ablation-kunpeng.yaml`](../examples/experiments/agent-rl-heterogeneous-ablation-kunpeng.yaml)
is a host-specific three-environment fixture. It fixes task assignment, arrival
schedule, templates, NUMA budgets and seed, and selects the NUMA suite with five
policy combinations: OS default, load-aware LRU, placement only, residency only, and full.
Use `configure --suite numa-snapshot --no-ablations` for the two main arms. Its
template IDs and measured restore-cost provenance are not portable; replace
them with values created and qualified on the target host.

```bash
clawbox experiment validate examples/experiments/agent-rl-heterogeneous-ablation-kunpeng.yaml --inputs
clawbox experiment plan examples/experiments/agent-rl-heterogeneous-ablation-kunpeng.yaml
sudo -E "$(command -v clawbox)" --output-root /data/clawbox-results experiment qualify \
  examples/experiments/agent-rl-heterogeneous-ablation-kunpeng.yaml
sudo -E "$(command -v clawbox)" --output-root /data/clawbox-results experiment launch \
  examples/experiments/agent-rl-heterogeneous-ablation-kunpeng.yaml \
  --run-id agent-rl-heterogeneous-ablation-20261016
clawbox --output-root /data/clawbox-results experiment report \
  agent-rl-heterogeneous-ablation-20261016
```

For a sensitivity series, copy the YAML, change exactly one of environment
count, `execution.concurrency_levels`, per-case reuse probabilities, or
`resources.warm_memory_capacity_mib`, validate and qualify the copy, and use a
new run ID. A lower logical WARM limit needs no host apply if it fits the
already-applied mount; changing the mount, NUMA topology or hard host capacity
does. Preserve the same case order, templates, seed and other limits. Report
failed trials and raw per-arm data; do not turn a warm-cache calibration whose
SSD mean is equal or faster into a claimed residency benefit.

`--detach` backgrounds the formal run after preflight has finished.
A run ID belongs to one frozen specification. Calling `launch` again
returns an active run's status, returns an existing successful report,
or resumes an interrupted run. On resume, only trials with a matching
specification, completed outcome, and verified cleanup are skipped. OOM,
timeout, and admission failures remain recorded outcomes and do not stop the
remaining trials or get retried on resume. Infrastructure and validation failures
stop the run; repair the cause before resuming.
Use a new run ID for changed parameters. After reboot, reapply the host
YAML before launching the same specification and run ID. Keep ownership
and completion records intact.

## Reading the results

The runtime-control table separates HIGH and LOW completion counts and JCT.
Check LOW completion as well as HIGH latency before claiming a benefit.
Cross-repetition averages include successful arms with the same case mix,
concurrency, complete policy and resource configuration; failed arms remain
visible in the per-arm report.

| Path within the run directory | Contents |
| --- | --- |
| `run-state.json` | Supervisor state, process identity, trial progress, and cleanup; a dead process marked running appears as orphaned |
| `experiment.yaml`, `qualification.json` | Frozen specification and preflight record |
| `arms/*.json`, `arms/*.complete` | Full trial results and verified completion markers |
| `summary.json`, `summary.csv`, `summary.md` | Completed-trial summary |
| `report.md`, `memory-timeseries.csv` | Comparison report and sampled memory time series |
| `attempts/ATTEMPT/ARM/` | Raw events, logs, model gateway data, telemetry, VM ownership, and lifecycle evidence |

Before comparing performance, check completed-session counts, task
validation, execution-ID/telemetry joins, lost events, OOMs, and cleanup.
`starting` only means that background startup was requested;
`succeeded` is the successful final state. Failed and incomplete
trials are not successful performance samples.

Session completion time and cumulative admission wait are different
measures; the latter sums waits and may exceed wall-clock duration.
Measured local memory includes guest RAM, VM overhead, and retained
cache, so it need not fall to zero immediately after VM cleanup.
Configured VM capacity, command reservation, P50 prediction, actual
resident memory, allocated snapshot pages, and shared-pool accounting
are separate quantities. See [per-node result fields](supernode.md#reading-the-results).

Crossing a high watermark is a controller metric, not automatically a
failed trial or evidence of prediction error. Policy OOMs are failure outcomes;
`correctness.experiment_cgroup_oom_kill_events` identifies kills in the experiment
domain even when the resulting VM error is an RPC failure. OOMs elsewhere on the
host invalidate the trial. OOMs and timeouts may prevent final telemetry delivery:
retain those failed outcomes, but do not impute missing measurements.
Shared-pool capacity violations, failed final validation, unexplained telemetry
loss, control failures, or unverified cleanup invalidate a comparison. `n/a` for prediction error or
coverage means the observation is missing, not that error was zero.
For a performance claim, keep workload, topology, VM identities,
arrival pattern, and random seed comparable; use independent
training data, multiple repetitions, and the raw failed samples.

Cross-repetition performance means are conditional on successful trials; the report
also retains all offered trials and priority completion counts. Do not discard
policy OOMs or timeouts when comparing completion rates. P95 is computed from raw
completed session durations, not from an average of per-trial percentiles; fewer
than 20 samples are reported as unavailable. The bootstrap interval resamples
whole trials and requires at least five trials. Report completion rates alongside
conditional latency, and retain the task mix and priority rotation.

## NUMA memory scheduling

For diagnostics, `configure --memory-scheduling` sets `resources.memory_scheduling`. It is independent
of `execution.placement_policy`. Keep the same compute CPUs, local budgets, shared
capacity, admission settings, VM templates and traces across the memory comparison.

| Value | Behavior | Required reclamation |
| --- | --- | --- |
| `static` (default) | Existing HIGH/LOW control; borrow at the HARD guard | Existing policy unchanged |
| `local_only` | Local admission and waiting; no live borrowing or checkpoints | `resident` |
| `snapshot_only` | Reclaim safe idle VMs; no live borrowing | `snapshot_pause` |
| `joint` | At HIGH or blocked demand, choose between a safe idle-VM WARM checkpoint and live borrowing, otherwise wait; below LOW, return a safe amount of live shared residency to LOCAL | `snapshot_pause` |

Joint scheduling uses the deterministic reclaim-then-borrow fallback when
`memory_action_costs` is absent. With that object configured, it compares the
empirically measured cost per configured VM GiB for the available borrow and WARM
checkpoint candidates. The checkpoint estimate includes the configured expected
restore probability. These rates are workload- and host-specific; the controller
does not turn raw NUMA latency or bandwidth into an application slowdown. Snapshot-only
and joint prefer WARM when configured, with COLD fallback in `tiered` storage;
`warm-only` still rejects insufficient WARM capacity. Restore keeps the original
compute node. A retained CoW snapshot remains charged until its references end.

`snapshot_only` retains its configured WARM capacity even when
`shared_memory_borrow_limit_mib` is unset. A positive WARM capacity requires
`warm_snapshot_root`; `tiered` also requires `cold_snapshot_root` for capacity
fallback, including with `full-copy`. Qualification uses the same default snapshot
tier and transfer limits as the worker. NUMA LOW/HIGH hysteresis and safe idle
reclamation operate independently of whether live borrowing is enabled.

Editable resource fields (units and defaults):

| Field | Default | Meaning |
| --- | --- | --- |
| `pool_transfer_concurrency` | `1` | Maximum simultaneous checkpoint, restore, spill and memory-rebind calls across both compute nodes |
| `pool_transfer_timeout_seconds` | `120` | Seconds to wait for a shared-pool transfer slot or post-destroy borrowed-page/cgroup convergence; timeout fails cleanup without releasing the reservation |
| `borrow_cooldown_seconds` | `5` | Minimum interval between successful joint-policy borrows per compute node |
| `return_cooldown_seconds` | `5` | Minimum seconds after the last borrow or return before one VM can be rebound to LOCAL |
| `memory_action_costs.provenance` | unset | Required source artifact or experiment ID for calibrated joint decisions |
| `memory_action_costs.live_borrow_service_seconds_per_gib` | unset | Measured cpuset rebind/migration service seconds per configured VM GiB |
| `memory_action_costs.live_borrow_runtime_penalty_seconds_per_gib` | unset | Paired-workload remote-placement penalty seconds per configured VM GiB; use zero only for a measured zero-result |
| `memory_action_costs.warm_checkpoint_seconds_per_gib` | unset | Measured WARM checkpoint service seconds per configured VM GiB |
| `memory_action_costs.warm_restore_seconds_per_gib` | unset | Measured WARM restore service seconds per configured VM GiB |
| `memory_action_costs.cold_checkpoint_seconds_per_gib` | unset | Measured COLD checkpoint service seconds per configured VM GiB; required for `tiered` calibrated decisions |
| `memory_action_costs.cold_restore_seconds_per_gib` | unset | Measured COLD restore service seconds per configured VM GiB; required for `tiered` calibrated decisions |
| `memory_action_costs.expected_restore_probability` | unset | Explicit probability in `[0,1]` used only in the expected restore-cost term |
| `memory_action_costs.remote_access_latency_ns` | unset | Optional measured random-access latency retained as context; it is not converted to workload time |
| `memory_action_costs.remote_stream_bandwidth_mib_per_second` | unset | Optional measured effective remote STREAM bandwidth retained as context; it is not converted to workload time |

These changes need a new run ID and qualification, without host apply, image rebuild
or retraining. Topology/budget changes still require host apply and configure.
There is no validated memory-bandwidth input: transfer concurrency is a limit on
operations, not measured bandwidth. CPU utilization and scheduler wait fraction are
reported separately; per-NUMA CPU PSI is unavailable and is not replaced with zero.
For a calibrated `tiered` decision, a reclaim that fits current uncommitted WARM
capacity is costed and requested as WARM. Otherwise it is costed and requested as
COLD, preserving existing WARM generations instead of hiding an LRU spill inside a
WARM estimate. Concurrent capacity changes can still make Cube fall back to COLD;
the lifecycle event records the destination that actually completed.

`memory_scheduling_decision` events record the action, reason, cost-model name,
provenance, candidate sizes, cost estimates, physical effect and shared ledger.
`shared_memory_borrowed` records the target VM's before/after physical residency and
rebind duration. Net residency changes include faults and frees; `migrated_bytes`
is unavailable rather than inferred from those differences. `shared_memory_returned`
records the low-watermark eligibility observation and the actual return result. A
return is attempted only if the VM's observed shared residency fits below LOCAL HIGH;
it keeps
the full live reservation if shared pages remain. Cleanup fails with a remedy if
processes or shared pages remain; do not reuse that capacity before reclamation is
verified. Failed checkpoint replies retain in-flight reservations until cleanup.

Live-borrow qualification also writes a unique guest `/dev/shm` file of at most
128 MiB (one sixteenth of configured Tool RAM, capped at 128 MiB). The Tool image
needs `dd` and at least 5 MiB free tmpfs space. The probe also caps the allocation
at free space minus 4 MiB, fails before writing if that space is unavailable, and
records the actual allocated MiB. The probe VM is destroyed afterward.
Before rebind, after rebind, after allocation and after return, qualification saves
the VM cgroup's residency and allowed CPU/memory sets, `/proc/PID/numa_maps`, and
`numastat -p` when installed on the host. A missing `numastat` is reported explicitly;
install the host's numactl package for that additional view. Net resident growth
does not identify individual newly faulted or migrated pages.

For placement comparisons, use `offered_to_completion_*_seconds` alongside the
existing Agent execution JCT. The former includes placement, creation and admission
waiting from the scheduled arrival. `placement_wait_total_seconds` excludes the
configured stagger delay. Observed Tool concurrency comes from completed operation
intervals and includes admitted SSH backend maintenance. The separate
`performance.observed_agent_tool_concurrency` excludes backend maintenance using
the execution scope recorded by the existing SSH admission path. Cross-VM overlap
requires synchronized Runtime VM wall clocks.

`memory.numa_allocation_event_deltas` contains physical host kernel NUMA
allocation counters in pages. These are diagnostics, not cross-NUMA traffic.
The supported workflow does not currently have a qualified Kunpeng host uncore
event that converts remote transactions to bytes, so
`memory.cross_numa_traffic_bytes` is explicitly `null` and
`memory.cross_numa_traffic_measurement` explains the missing measurement. Do not
substitute cgroup residency, `numa_miss`, or guest LLC counters for traffic bytes.

## OS memory control and lifecycle admission

`resources.lifecycle_admission` defaults to `configured-capacity`: creation
reserves the configured Runtime+Tool capacity until both VMs are ready; restore
reserves the restored VM capacity. These are outstanding demand promises, not
resident RAM. Observed growth is deducted from a promise before it is released.

Set `resources.lifecycle_admission: os-managed` and
`resources.proactive_cache_reclaim: false` for the guarded OS baseline. This
removes the configured-capacity creation reservation, including NUMA placement,
and disables ClawBox's proactive `memory.reclaim`. It requires resident policies
and no live shared-memory borrowing. `deployment: os-default` resolves these
settings per arm. The creation concurrency limit, host emergency free floor and
cgroup hard limit still apply; the configured-capacity and measured-budget
creation queues do not. This is a guarded OS
baseline, not an unconstrained host; CubeSandbox may impose an additional capacity
check that this setting does not bypass. The NUMA suite compares ordinary OS
placement against the complete managed design, with matched managed ablations.

`proactive_cache_reclaim` defaults to `true` in configured-capacity mode. The
request size is `file - shmem`; the kernel chooses the pages reclaimed.
`memory.max` can lead to OOM and is not a successful-completion guarantee.
Swap policy is read from the host cgroup, not enabled by this setting. A swap
comparison requires a separately configured and verified host condition for all
arms. No claim about guest free-page reporting is inferred from guest `free`.

Both settings are editable YAML fields and require a new run ID, but no image
rebuild or retraining. Changing host limits or NUMA layout requires `host apply`;
changing templates or the measured prediction environment requires retraining.
Do not substitute a startup percentile for a capacity guarantee without separate
concurrent-start and restore/fault-growth validation.

`memory_sample.cgroup_memory` records raw `memory.stat`, `memory.events`,
`memory.swap.current`, effective limits and PSI. Missing optional files are null.
`file` includes `shmem`; do not add them. PSI `total` is cumulative microseconds;
use differences over the sample interval. `memory.peak` covers the cgroup lifetime,
not just this run; use the sampled run peak for arm comparisons. Host
`MemTotal - MemAvailable` is an availability estimate, not VM RSS. Lifecycle
observations also include the cgroup counters. Sampling failures fail the run.

`offered_provisioned_capacity_exceeds_policy_budget` describes offered configured
capacity only; it does not establish concurrent residency or physical pressure.
Cube runtime ratios are null with `status: unverified` when the API does not expose
them. Inspect the effective Cubelet configuration before attributing a creation
bottleneck to ClawBox.

cgroup v2 `cpuset.mems` changes allocation policy and requests existing-page
migration, which can be incomplete. Borrow candidates are ranked by observed
LOCAL residency. Before/after residency differences include faults and frees and
are not migration traffic. Shared-pool capacity reservations still cover future
VM growth; do not add their full size again to observed shared pages as if both
were physical usage. Returning allocation to LOCAL does not release a reservation
while observed shared pages remain.
