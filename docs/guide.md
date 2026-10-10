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

## Supported comparison policies

| Preset | Runtime control | Admission | Residency |
| --- | --- | --- | --- |
| `runtime-resident` | Official cgroup fallback | off | resident |
| `predicted-resident` | Official cgroup fallback | predicted | resident |
| `fixed-resident` | Official cgroup fallback | fixed | resident |
| `uncontrolled-resident` | off | off | resident |
| `prediction-only-resident` | off | predicted | resident |
| `predicted-wait-snapshot` | Official cgroup fallback | predicted | wait-aware snapshot |

Repeat `--baseline` to compare presets. In YAML, policy names are labels:
`admission: "off"`, `fixed`, or `predicted` is independent of
`runtime_control: "off"` or `agentcgroup_cgroup`, and of reclamation.
Quote `"off"` in YAML so it is not parsed as a boolean. Runtime control defaults
to the official fallback. Snapshot policies require `eviction: wait_aware_pressure`
and `restore: reactive`; resident policies use `none` for both.

The main admission ablation compares `runtime-resident` against
`predicted-resident`. Add `uncontrolled-resident` and `prediction-only-resident`
to measure the interaction. Keep workload, physical budget, priorities, placement,
creation margins and residency fixed. Repeat this matrix separately on an
ordinary pool and on the supernode configuration; neither controller requires
shared NUMA memory. See [control-ablation.yaml](../examples/experiments/control-ablation.yaml).

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
and does not load prediction artifacts. VM creation/restore accounting remains
identical between paired arms; `startup_headroom_mib` defaults to 256 MiB.

`experiment train` emits `clawbox_prediction_v2`: frozen ClawTune guest P50
predictions plus a measured guest-to-host cgroup increment calibration from a
separate validated training run. Host cgroup growth is sampled alongside guest
measurements in every policy. RSS is reported separately. Guest values are never
implicitly treated as host memory. Missing predictions/calibration fail explicitly;
short duration without memory observations does not establish a zero estimate.
Recollect training data when the artifact lacks this measurement contract.

`--admission`, `--runtime-control`, and `--reclamation` filter convenience presets.
For other supported combinations, edit the independent YAML fields directly.

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
| `workload.repetitions` | `--repetitions`; repeats each task/concurrency/policy combination |
| `workload.cases[]` | Explicit tasks with `case_id`, `prompt`, `source`, `source_reference`, `repository`, `base_commit`, `replay_trace_reference`, and optional validation |
| `workload.session_assignment` | `--session-assignment single_case` gives each task separate trials; `round_robin` distributes at least two explicit tasks among sessions in a trial |
| `execution.concurrency_levels` | `--concurrency 1,2,4`; offered sessions, each with two VMs, not the measured number executing simultaneously |
| `execution.placement_policy`, `session_compute_nodes` | `--placement-policy` and `--session-compute-nodes`; compute-node mapping, separate from task assignment |
| `execution.randomized_order`, `random_seed` | Trial order is randomized by default; `--random-seed` fixes the seed; edit YAML to change the order switch |
| `execution.arrival_schedule`, `stagger_interval_seconds` | `--arrival-schedule burst` or `fixed_stagger`; the latter needs a positive `--stagger-seconds` |
| `execution.arm_timeout_seconds` | `--arm-timeout-seconds`; positive integer limit for the entire trial; cannot be `null` |
| `execution.command_timeout_seconds` | `--command-timeout-seconds`; command execution deadline; admission wait is timed separately but remains within the trial deadline |
| `execution.memory_sample_interval_seconds`, `stabilization_seconds` | `--memory-sample-interval-seconds` and `--stabilization-seconds` |
| `runtime`, `sandbox` | Agent/tool VM templates, image digests, `vcpu`, `memory_mib`, workspace, preflight command, and internet setting |
| `resources.pool_memory_budget_mib` | `--pool-memory-gib`; with multiple compute nodes, must equal the sum of their high watermarks; normally copied from the host profile |
| `resources.emergency_free_memory_mib` | `--emergency-free-memory-gib`; host-wide available-memory safety floor |
| `resources.checkpoint_restore_headroom_mib` | `--checkpoint-headroom-gib`; per-node allowance for snapshot operations, charged only to checkpointing policies |
| `resources.static_tool_memory_mib` | `--static-tool-memory-mib`; per-command reservation for the fixed policy, not VM RAM |
| `resources.non_command_tool_memory_mib` | Fixed reservation for non-command tools; defaults to the static tool reservation when unset |
| `resources.prediction_artifact` | `--prediction-artifact`; frozen prediction file from an independent training run |
| `resources.snapshot_mechanism` | `--snapshot-mechanism incremental-cow` or `full-copy` |
| `resources.snapshot_storage` | `--snapshot-storage warm-only` or `tiered`; the latter may use disk, whose path must match the service setting |
| `validation.command` | `--validation-command`; final task check executed in the tool VM; required explicitly for training |

To change vCPUs, guest RAM, or writable disk size, register a new VM
template as described in [installation](installation.md#4-register-templates-and-verify-ssh)
and update the host YAML. `configure --runtime-vcpu`,
`--tool-vcpu`, and the memory options do not resize registered templates.
Likewise, changing NUMA nodes, CPU lists, shared-pool size, or disk
paths requires `host apply` before generating a new experiment YAML.

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
clawbox experiment import-trace /data/source-trace.jsonl --output /data/replay.jsonl
clawbox experiment trace /data/replay.jsonl
clawbox experiment validate /data/eval.yaml --inputs
```

`import-trace` converts recognized research traces; it does not install
task dependencies or create an initial workspace. Prepare the
repository, starting revision, and dependencies in the tool VM image.
For one task, use `configure --trace --prompt --repository --base-commit`
to replace the base input. For multiple tasks, fill in
`workload.cases` in YAML. Each case's `source` must match
`workload.source`. Explicit cases take precedence over
`workload.input`; otherwise that field is read according to source type.

A complete replay ends with a model response that makes no further
tool request. A positive `inference.configuration.max_model_steps`
runs only a prefix, which is insufficient as a full training run.
`--time-scale` scales recorded model wait; policy-induced delays
are measured separately. Actual tool output is retained rather than
replaced with recorded text.

Training requires a successful completed run using the fixed-reservation
policy, complete measurements, final task validation, verified cleanup,
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
| `host inspect/init/check/apply` | Inspect topology, create host YAML, check prerequisites, apply host settings, and probe a VM |
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
policy. That step must request a tool. Its `true` validation command
tests the execution path; the formal trial still uses the task's own
validation. Preflight concurrency defaults to the experiment's maximum.
A record made with a lower `--qualification-concurrency` cannot
cover a higher-concurrency experiment. The record is tied to the full
specification, implementation revision, templates and images, nodes,
policies, and concurrency. `launch` reruns preflight when those change;
`--force-qualify` requests it explicitly. Runtime-control qualification also
requires real kernel activation and LOW throttling probes, including measured
guest allocation growth under the default upstream signals, plus HIGH/LOW
placement of both VM roles and their configured snapshot restores. A host
allocation probe does not establish guest-to-host free-page reclamation.

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
