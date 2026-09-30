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

| Behavior | `--baseline` value | Required input |
| --- | --- | --- |
| Fixed per-command memory reservation; VMs remain running | `tool-static-resident` | `static_tool_memory_mib` |
| Recorded per-command P50 prediction; VMs remain running | `tool-p50-resident` | `prediction_artifact` |
| P50 prediction plus pressure-triggered checkpointing while the agent waits for a model response | `tool-p50-wait-reactive` | Prediction file and model-wait estimate with its source |

Repeat `--baseline` to compare policies in one experiment.
`--reserve-during`, `--estimate`, `--idle`, and `--resume`
filter the predefined policy catalog; they do not create arbitrary
combinations. Policies marked `DEPRECATED` in `baselines --all`
are excluded from the supported workflow.

`--static-tool-memory-mib auto` uses the measured 90th percentile of
extra command memory from a validated training run. The
`--model-wait-prediction-seconds auto` value comes from the current
trace's median model-call duration, scaled by `time_scale`. Both
values and their sources are frozen when the YAML is generated; a
formal run does not train them online.

## Experiment YAML fields

Fields without CLI options can be edited directly. The format requires
`schema_version: 2` and rejects unknown fields. See
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
`--force-qualify` requests it explicitly.

`--detach` backgrounds the formal run after preflight has finished.
A run ID belongs to one frozen specification. Calling `launch` again
returns an active run's status, returns an existing successful report,
or resumes an interrupted run. On resume, only trials with a matching
specification, successful completion, and verified cleanup are skipped.
Use a new run ID for changed parameters. After reboot, reapply the host
YAML before launching the same specification and run ID. Keep ownership
and completion records intact.

## Reading the results

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
failed trial or evidence of prediction error. OOMs, shared-pool capacity
violations, failed final validation, lost telemetry, or unverified
cleanup invalidate a comparison. `n/a` for prediction error or
coverage means the observation is missing, not that error was zero.
For a performance claim, keep workload, topology, VM identities,
arrival pattern, and random seed comparable; use independent
training data, multiple repetitions, and the raw failed samples.
