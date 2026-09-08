# Configure, run, and inspect experiments

ClawBox compares memory-management policies while workloads execute in CubeSandbox
virtual machines. A policy controls memory reservations, saving idle VM state,
and restoring that state. It does not change the configured VM RAM size.

The public interface is `clawbox experiment`. Use the same configuration format
and commands for every workload and concurrency level. Run the commands below
from the repository root; relative input paths are resolved against the current
working directory, not the directory containing the YAML file.

## 1. Try the included input without a VM host

After installing the Python package with Python 3.12 or newer:

```bash
clawbox experiment trace examples/traces/smoke.jsonl
clawbox experiment validate examples/experiments/getting-started.yaml --inputs
clawbox experiment describe examples/experiments/getting-started.yaml
```

These commands inspect files and configuration only. The trace contains a short
model wait and a shell command that creates `/workspace/result.txt`. The YAML
uses a fixed memory reservation and one concurrent session. Template aliases and
the compute-node name are placeholders, so the example cannot start VMs until
they are replaced with registrations from your host.

`trace` reports the file hash, action counts, and recorded model-wait duration.
`validate --inputs` additionally parses every selected replay trace and required
prediction file. It does not contact CubeSandbox, check image contents, or prove
that an agent's future requests will match a recording. `plan` prints the exact
expanded execution configurations as JSON.

## 2. Understand the configuration

The complete starting file is [getting-started.yaml](../examples/experiments/getting-started.yaml).
It uses the following sections:

| Section | Meaning |
| --- | --- |
| `workload` | Task identities, prompts, replay files, repository revisions, repetitions |
| `agent.driver` | `openclaw` runs the real agent; `replay_engine` directly executes recorded tool actions for controlled system checks |
| `inference` | `api` obtains live model responses; `replay` supplies recorded responses and timing |
| `runtime` | Template and fixed resources for the VM hosting the agent |
| `sandbox` | Template, workspace, and fixed resources for the VM executing tools |
| `execution` | Concurrency levels, arrival schedule, random seed, timeouts |
| `resources` | Host memory budget, safety headroom, prediction inputs, optional snapshot storage |
| `policies` | Named combinations to compare |
| `validation.command` | Command that checks the final Tool workspace; exit status zero means success |

The two drivers are execution settings within this interface, not separate launch
workflows. The included small trace uses `replay_engine`: its recorded command is
executed directly. For agent experiments set `agent.driver: openclaw`; tool calls
then come from the running agent, including when model responses are replayed.
Do not interpret a direct-command check as evidence of a successful agent task.

A *session* is one workload execution. An *arm* is one policy, concurrency level,
case assignment, and repetition. By default, each case is expanded separately.
With `workload.session_assignment: round_robin`, sessions cycle through the
listed cases within the same arm. Policies run sequentially; sessions within an
arm are concurrent. The selected cases and concurrency come from the YAML.

Memory sizes ending in `_mib` use MiB; the friendly `--*-gib` options use GiB.
Configured guest capacity is `sessions × (runtime RAM + tool RAM)`. Actual host
memory includes overhead and cache and must be measured. A pool budget is a
scheduling constraint; hard memory isolation also requires host cgroup setup.

## 3. Select policy dimensions

Use presets as convenient combinations of implemented mechanisms. You do not
need to memorize a fixed list of baseline names:

| Option | Values | Decision |
| --- | --- | --- |
| `--reserve-during` | `session`, `command` | Hold a reservation for the whole session or only command execution |
| `--estimate` | `capacity`, `fixed`, `predicted`, `measured` | Use configured capacity, one fixed amount, command prediction, or held-out measured demand |
| `--idle` | `resident`, `immediate`, `timeout`, `pressure`, `known-wait`, `tiered-lru`, `tiered-wait` | Keep running, reclaim on idle/timeout/pressure, or use advance timing information and optional storage tiers |
| `--resume` | `none`, `on-demand`, `ahead` | No restore, restore when needed, or start restoration before a predicted response |

Repeat a dimension for alternatives; different dimensions are combined as filters.
Omitted dimensions are unrestricted. Only implemented preset combinations are
selected. Unsupported intersections produce an error rather than creating a new
algorithm. `clawbox experiment baselines` lists the available combinations and
their original identifiers; those identifiers remain in saved configurations and
results for compatibility.

For example, compare fixed and predicted reservations with and without immediate
idle reclamation:

```bash
clawbox experiment configure examples/experiments/getting-started.yaml comparison.yaml \
  --reserve-during command --estimate fixed --estimate predicted \
  --idle resident --idle immediate --concurrency 1,4
clawbox experiment describe comparison.yaml
```

This creates a two-by-two policy comparison at each selected concurrency. The
example prediction file contains synthetic values for the example's tool action.
Use independently collected prediction data for a real workload.

Without dimension filters or `--baseline`, `configure` retains the base file's
policies. If both are given, dimensions filter the explicitly named baselines.
Existing output files require `--force` to replace.

| Mechanism | Required input or parameter |
| --- | --- |
| Fixed command reservation | `resources.static_tool_memory_mib`; CLI `--static-tool-memory-mib` |
| Full command reservation | `resources.full_tool_memory_mib`; normally the Tool VM's configured RAM |
| Predicted reservation | `resources.p90_predictions`; CLI `--p90-kb` |
| Measured reservation | `resources.oracle_measurements`; replay only |
| Idle timeout | `fixed_delay_seconds`; CLI `--fixed-delay-seconds` |
| Pressure-triggered reclamation | Model-wait estimate and source in `inference.configuration` |
| Early restoration | `prefetch_lead_seconds`; CLI `--prefetch-lead-seconds`, plus a wait estimate and source |
| Known-wait threshold | `checkpoint_break_even_seconds`; replay only |
| Tiered storage | Local/snapshot capacities, distinct NUMA nodes, memory and disk snapshot directories; replay only for the currently implemented presets |

Immediate and delayed reclamation belong to the same conceptual family; early
restoration is another decision. The selector groups them without modifying their
underlying implementations. Reservation amounts and prediction files remain
experiment-wide inputs. To compare their values, generate separate configurations.

The current pressure-based policy does not compare the predicted wait against
checkpoint cost. Both tiered presets use recorded future waiting information;
the least-recently-used choice is therefore not a pure online LRU baseline.
Advance-information references are not proven performance optima.

`describe` shows effective snapshot-memory capacity for each policy. The existing
execution planner disables that capacity for non-tiered policies. If capacities
differ, a performance comparison includes both policy and resource differences.
The selector does not remove that limitation.

## 4. Supply your own task and trace

A replay trace is UTF-8 JSON Lines: one JSON object per nonempty line. The preferred
interchange format uses action records. Here is a model record, expanded for
readability; put the complete object on one line in a `.jsonl` file:

```json
{
  "type": "action", "action_type": "llm_call",
  "action_id": "model-1", "iteration": 0,
  "ts_start": 0.0, "ts_end": 0.01,
  "data": {
    "model": "recorded-model",
    "raw_request": {"messages": [{"role": "user", "content": "Create a marker file."}]},
    "raw_response": {"role": "assistant", "content": "Finished."},
    "llm_latency_ms": 10
  }
}
```

| Field | Contract |
| --- | --- |
| `action_id` | Unique action identifier; also keys direct-replay prediction inputs |
| `iteration` | Integer sequence number used to order actions with equal timestamps |
| `ts_start`, `ts_end` | Finite timestamps in seconds, using one clock for the recording |
| `data.raw_request` | Recorded request object, including messages and tool definitions for managed agent replay |
| `data.raw_response` | Assistant response message, including any `tool_calls`; function `arguments` remain JSON-encoded strings |
| `data.llm_latency_ms` | Model wait in milliseconds; if present, overrides `ts_end - ts_start` |

For direct tool replay, use `action_type: tool_exec` and
`data: {"tool_name":"exec", "args":{"command":"..."}, "exit_code":0, "result":"..."}`.
See the actual lines in [smoke.jsonl](../examples/traces/smoke.jsonl).
[openclaw-cube-replay.jsonl](../examples/traces/openclaw-cube-replay.jsonl) illustrates
model responses with a tool call. It is a format fixture, not a portable recording
of whichever agent version and image you happen to install.

The parser also accepts paired span records: `record_type: span_start|span_end`,
`kind: llm|tool`, matching `trace_id` and `span_id`, start `wall_time_ns`, and end
`duration_ns`. Model input is `input.messages` and output is `output.content`;
tool input is `input.requested_args`, with `output.result` and `output.exit_code`.
Unpaired spans fail parsing. New managed recordings should use the action format
exported by ClawBox, which preserves the full request envelope.

Changing a trace should also update its task identity and validation:

```bash
clawbox experiment configure examples/experiments/getting-started.yaml task.yaml \
  --trace /data/traces/my-task.jsonl --case-id my-task \
  --prompt 'Implement the requested change and run its tests.' \
  --repository organization/project --base-commit YOUR_REVISION \
  --validation-command 'cd /testbed && python -m pytest -q'
```

This updates metadata; it does not clone a repository or build an image. The Tool
template must already contain the matching repository, revision, dependencies,
and workspace. For several tasks, list complete objects under `workload.cases`,
each with its own `case_id`, `prompt`, `source: recorded_trace`,
`source_reference`, `replay_trace_reference`, and optional `repository`,
`base_commit`, `validation`. The singular CLI overrides deliberately reject a
multi-case input so that one trace cannot silently replace every task.

### Record an agent workload for replay

Use the same CLI and configuration. Set `agent.driver: openclaw` and
`inference.backend: api`. In `inference.configuration`, set `base_url` to your
provider's OpenAI-compatible API endpoint, `model` to its model identifier, and
`api_key_env` to the name of an exported credential variable. Keep the secret out
of YAML. The workload's prompt and Tool image define the task; replay input is
not consumed during live inference.

After a successful run, each session's model recording is exported to
`model-traces/<session-id>.jsonl` inside the result directory. Inspect it with
`clawbox experiment trace`, then select it with `configure --trace` and
`--inference-backend replay --time-scale 1`. Keep the agent version, runtime
configuration, original task prompt, tool image, repository, and initial workspace
the same. Record and replay now use the same agent setup.

Replay matches incoming requests after normalizing known runtime metadata such
as session identifiers and timestamps. A valid JSONL file alone cannot establish
that the request contents match. Missing, extra, or different requests fail the
run. Workload-specific exceptions for package errors and installed files are not
applied. Historical recordings captured with the retired special runtime setup
may need to be recorded again in the selected environment.

For direct replay, a prediction file maps action IDs to positive MiB reservations,
for example `{"tool-1":256}`. Managed agent prediction files instead contain
command records with `command`, `predicted_command_memory_p90_mib`, and
`predicted_host_execution_increment_mib`; the latter is calibrated host demand.
The Runtime must supply matching command metadata. An action-ID dictionary is
not a substitute for that data. File operations use the configured static budget.

## 5. Run on an installed host

Real runs require the patched CubeSandbox server, matching SDK, registered images,
and reachable guest control endpoints described in [installation](installation.md).
Load your machine settings explicitly; the CLI does not source shell files:

```bash
set -a
source ~/.config/clawbox/machine.env
set +a
clawbox experiment configure examples/experiments/getting-started.yaml local.yaml \
  --target-node "$CUBE_NODE" \
  --runtime-template-id "$CLAWBOX_RUNTIME_TEMPLATE" \
  --runtime-image-reference "$CLAWBOX_RUNTIME_IMAGE" \
  --runtime-image-digest "${CLAWBOX_RUNTIME_IMAGE##*@}" \
  --tool-template-id "$CLAWBOX_TOOL_TEMPLATE" \
  --tool-image-reference "$CLAWBOX_TOOL_IMAGE" \
  --tool-image-digest "${CLAWBOX_TOOL_IMAGE##*@}"
clawbox experiment validate local.yaml --inputs
clawbox experiment describe local.yaml
clawbox --output-root /data/clawbox-results experiment run local.yaml --run-id marker-01
```

Use a fresh run identifier and an idle VM pool. `run` is a foreground command:
keep the shell connected or use your host's process supervisor. It checks inputs
before creating VMs, uses the selected concurrency and workload, and replays the
full input. There is no implicit truncation or injected stop response. Use a
complete shorter recording if you need a shorter task.

The two example templates must match 2 vCPU/2 GiB and 2 vCPU/4 GiB respectively.
Changing VM sizes requires corresponding templates and provenance. Pool budgets
and emergency free-memory limits must fit the host's actual available capacity.

## 6. Inspect results and failures

```bash
clawbox --output-root /data/clawbox-results experiment status marker-01
clawbox --output-root /data/clawbox-results experiment report marker-01
clawbox --output-root /data/clawbox-results experiment collect marker-01
```

`status` gives arm status, `report` prints the generated Markdown summary, and
`collect` returns the complete summary as JSON. The result directory contains:

| Path | Contents |
| --- | --- |
| `summary.json`, `summary.csv`, `summary.md` | Combined arm results, configuration/provenance, and summaries |
| `arms/` | Individual results and completion markers |
| `events/` | Memory samples, admission, lifecycle, and session events |
| `model-gateway/`, `model-traces/` | Request matching evidence and exported model recordings |
| `policy-control/`, `tool-artifacts/` | Tool admission, measurements, and validation evidence |
| `owned-sandboxes.jsonl` | VM ownership for cleanup and failure investigation |

Summary files are written when the worker finishes; inspect per-arm files and
events for a run that has not produced a summary yet. A failed or interrupted run
is not evidence of success. Preserve its directory before starting another attempt.

A successful comparison needs all sessions requested by the arm to finish and
pass task validation. For managed replay, also check complete request matching,
exact execution-ID joins, telemetry loss, duplicate execution, and leaked VMs.
Missing metrics are not zero. Compare completion time, throughput, admission wait,
host memory over time, and checkpoint/restore cost under the same workload and
resource scope. Median and percentile results need enough observations; repeat
experiments to estimate variability.

If input checks fail, use the reported case and file path. If VM creation fails,
check template readiness, image provenance, disk capacity, and memory. If replay
fails, compare the rejected-request evidence with the recording and initial image;
do not edit responses to conceal a different workload. Administrative diagnosis
and storage setup are covered in [installation](installation.md).
