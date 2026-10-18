# Experiment configuration, commands, and results

Use the [step-by-step guide](self-service.md) for the full command sequence
and [host configuration](supernode.md) for NUMA and shared-memory settings.
This page explains the experiment YAML, command options, and result files.

`experiment configure --fixed-methods --recipe FAMILY` freezes a controlled study
and resets unrelated policy switches, including switch values supplied with `--set`.
Default: disabled. Supported families: admission, runtime, memory-reclaim, placement,
idle-reclaim, shared-memory, coordination, checkpoint (also requires `--checkpoint-axis`).
It retains numeric tuning, host settings, workload and measurement settings.
See the [fixed method table](experiments.md#配方与受控比较) for exact backgrounds and prerequisites.
Runtime and memory-reclaim require a fixed Tool allowance in MiB or an independent training
artifact for calibration. Idle-reclaim requires independently measured idle reclaim costs.
Missing prerequisites fail validation; fixed methods do not support diagnostic command-table
predictions or integrated/custom recipes. Inspect `describe` before running with a new run ID.

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

## Six-dimension experiments

New experiments use schema 4. See [the experiment guide](experiments.md) for the
six dimensions, recipes, prerequisites, units, defaults and command examples.
Use `experiment recipes`, `configure --recipe`, `configure --set`, `describe`
and `experiment schema`. Existing launch/qualify/train/resume/report commands
execute the resolved design through the same worker.

`comparison.arms[].override` accepts dotted mechanism paths only; environment and
workload remain common. Recipes generate explicit arms; their labels never change
execution. `describe --json` includes effective resources and requirements per arm.
Snapshot/borrow qualification covers each distinct effective configuration.

The sections below describe worker fields and measurement semantics also used
by existing schema-3 results. In schema 4, physical resource fields live under
`environment.resources`; decision parameters belong to six `mechanisms` objects;
prediction and empirical costs belong to `evidence`; sampling and final validation
belong to `measurement`. The public JSON Schema is the field catalog.
Old suites and baseline names are not mixed with schema-4 recipes.
The legacy `experiment baselines` output labels each preset with its comparison
family. Compare presets only within a shared family; the flat catalog is not a
single controlled study. The duplicate `no_control` preset was removed;
`uncontrolled-resident` is the common neutral managed control. Coordinated
release presets require measured `resources.idle_reclaim_costs` before a run.
Selecting multiple legacy `--baseline` values with no common family is rejected;
use a schema-4 recipe or an explicit custom design for a multi-axis study.

Schema 4 separates deployment (`mechanisms.deployment.mode`, managed by default)
from tool admission. The os-default deployment requires OS placement and all
managed optimization controls off. `mechanisms.coordination.action_selection`
chooses between reclaim-first and measured-cost borrowing/reclamation; the
`coordination` recipe compares these with common pressure borrowing and idle reclaim.
`conditions.prediction_source=kb` is common to all arms; `command-table` is for
diagnostics and cannot be varied by arm override. Set it using
`--set conditions.prediction_source=command-table` for a separate diagnostic run.
`train-p50` adds an exact-command model only after at least two validated Cube
memory observations for that command. Diagnostic selection tries lattice evidence,
then the exact command, Tool, and Tool-class evidence in that order. Changing the
training observations requires retraining and a new immutable prediction artifact.

Template affinity automatically requires sharing qualification. There is no
schema-4 sharing-enable switch or restore-timing selector: restore is reactive.
There is also no independent eager/lazy loading switch; the backend snapshot
mechanism controls loading, and WARM accounting remains retained conservatively.
Use `runtime.proactive_memory_reclaim` for cgroup memory.reclaim requests, which
do not guarantee selection of cache pages. The runtime recipe fixes this off;
the memory-reclaim recipe compares off/on separately. Changed configurations need
new run IDs; these configuration changes do not require host apply or image rebuild.

The common environment is validated before arm overrides, including NUMA sums,
watermarks and shared-pool borrow limits. Every arm uses the same physical live
boundary. CPU release boost requires managed placement and a LOCAL NUMA node.
Runtime memory protection and CPU boost require the corresponding host cgroup
PSI files; missing required PSI fails host readiness before VM probes. With a
PSI-capable kernel disabled by default, enable `psi=1` in the boot configuration
and reboot before launch. Baselines with both mechanisms off need no PSI.
Set `mechanisms.runtime.pressure_control.release_forecast_ttl_factor` (default 3,
finite multiplier >1) to bound CPU forecast validity by the longest historical
duration. This YAML change requires a new run ID, not host apply, image rebuild
or retraining. See [runtime prerequisites and tuning](pressure-control.md) for
host-charge release evidence and memory-protection scope.
With idle reclaim off, checkpoint encoding, lazy restore, storage and residency
are inactive, as reported by `describe`. Borrowing alone needs no snapshot paths.
Selecting the checkpoint recipe requires idle reclaim to be enabled explicitly.

Dimensions have dependencies: `runtime.pressure_control` also supplies release
ranking thresholds, and `checkpoint.pool_transfer_*` also governs shared-memory
transfers. Hold these parameters fixed in comparisons of the affected mechanisms.
Recipes reject incompatible inherited settings rather than changing other dimensions.

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
| `mechanisms.placement.tool_call_cpu_policy` | YAML only; `c0` (default) preserves the 8c555ae CPU behavior: the VM keeps the full selected-NUMA CPU mask, Linux schedules it, and no topology monitor runs. `c2` changes only Tool vCPU affinity while a Tool call is active. It selects one complete `cluster_cpus_list` mask, keeps one SMT representative per physical core (for example, `0-7` becomes `0,2,4,6`), and never subdivides that cluster by VM vCPU count. The vCPU count is only the minimum independent-core capacity required of a candidate. Readable Linux cache/cluster/SMT topology and at least two complete clusters in the selected NUMA pool are required; otherwise the arm fails. Completion/failure restores the full NUMA mask. Requires a new run ID; no host apply, image rebuild, or retraining. |
| `mechanisms.placement.tool_call_cpu_pressure_ratio`, `tool_call_cpu_pressure_samples`, `tool_call_cpu_cooldown_seconds` | YAML only; C2 CPU PSI stall ratio (default 0.10), consecutive samples (default 2), and cooldown seconds (default 5), sampled at `measurement.memory_sample_interval_seconds`. Sustained pressure expands the active call to the full selected-NUMA CPU pool; missing PSI also fails open and is recorded. C0 does not sample this signal. |
| `execution.randomized_order`, `random_seed` | Trial order is randomized by default; `--random-seed` fixes the seed; edit YAML to change the order switch |
| `execution.arrival_schedule`, `stagger_interval_seconds` | `--arrival-schedule burst` or `fixed_stagger`; the latter needs a positive `--stagger-seconds` |
| `execution.arm_timeout_seconds` | `--arm-timeout-seconds`; positive integer limit for the entire trial; cannot be `null` |
| `execution.arm_finalization_timeout_seconds` | YAML only; seconds, default 300. Extra supervisor grace for cleanup and writing partial results after the trial deadline; excluded from workload time and not available for Agent work. |
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
| `suite_runtime_control` / `policies[].runtime_control` | `--runtime-control off\|no_control\|phase_memcg\|release_cpu`; suites default off; custom policy fields are independent |
| `resources.pressure_control` | Editable [pressure policy fields and units](pressure-control.md); requires a new run ID |
| `resources.runtime_poll_interval_seconds` | `--runtime-poll-interval-seconds`; positive seconds, default 0.5 |
| `policies[].deployment` | `managed` (default) or `os-default`; the latter resolves to OS placement, resident VMs, no admission/controller/cache reclaim, using the applied VM domain |
| `resources.snapshot_mechanism` | `--snapshot-mechanism incremental-cow` or `full-copy` |
| `resources.snapshot_storage` | `--snapshot-storage warm-only` or `tiered`; the latter may use disk, whose path must match the service setting |
| `resources.template_base_sharing` | YAML only for diagnostics; the NUMA suite requires sharing qualification automatically. |
| `resources.snapshot_residency_costs` | Measured WARM/SSD restore seconds per GiB plus provenance; one YAML object |
| `policies[].template_aware_placement` | Generated by the suite; custom YAML requires load-aware placement and verified sharing |
| `policies[].snapshot_aware_residency` | Generated by the suite; custom YAML requires tiered storage, measured costs, and per-case reuse probability |
| `validation.command` | `--validation-command`; final task check executed in the tool VM; required explicitly for training |

With `incremental-cow` and `tiered`, COLD migration automatically reuses an
immutable template RAM base by an owned hardlink when both paths share a
filesystem; otherwise it copies the base. Missing bases and link errors other
than a cross-filesystem boundary fail the migration. `full-copy` behavior is
unchanged. Use the same backend and filesystem layout across compared arms.
Inspect `transferred_bytes`, `reused_base_allocated_bytes`, and
`layer_allocations` in snapshot metadata; deduplicate device/inode identities
for total physical storage instead of summing per-lineage referenced bytes.
This host-side fix requires rebuilding/installing the patched Cubelet during
an idle maintenance window, a new qualification and run ID for snapshot
comparisons. It requires no guest image rebuild or ClawTune retraining when
their identities and measurement semantics are unchanged. Existing source
checkouts with an older patch-set stamp must be preserved; prepare a new source
directory using the installation procedure.

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
the base placement algorithm implicitly. It preserves admission, runtime controls, CPU weights and
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

In schema 4, `mechanisms.deployment.mode=managed` uses configured-capacity admission: creation
reserves the configured Runtime+Tool capacity until both VMs are ready; restore
reserves the restored VM capacity. These are outstanding demand promises, not
resident RAM. Observed growth is deducted from a promise before it is released.

Select `mechanisms.deployment.mode=os-default`, `placement.policy=os_default`
and disable tool admission, runtime controls, idle reclaim and borrowing for the guarded OS baseline. This
removes the configured-capacity creation reservation, including NUMA placement,
and disables ClawBox's proactive `memory.reclaim`. It requires resident policies
and no live shared-memory borrowing. `deployment: os-default` resolves these
settings per arm. The creation concurrency limit, host emergency free floor and
cgroup hard limit still apply; the configured-capacity and measured-budget
creation queues do not. This is a guarded OS
baseline, not an unconstrained host; CubeSandbox may impose an additional capacity
check that this setting does not bypass. The NUMA suite compares ordinary OS
placement against the complete managed design, with matched managed ablations.

`mechanisms.runtime.proactive_memory_reclaim` defaults to `false`. The
request size is `file - shmem`; the kernel chooses the pages reclaimed.
`memory.max` can lead to OOM and is not a successful-completion guarantee.
Swap policy is read from the host cgroup, not enabled by this setting. A swap
comparison requires a separately configured and verified host condition for all
arms. No claim about guest free-page reporting is inferred from guest `free`.

These settings are editable YAML fields and require a new run ID, but no image
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
