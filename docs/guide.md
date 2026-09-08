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

These commands inspect files and configuration only. The trace is a small schema
fixture, not a real agent recording. Replace it with a trace from a live run before
starting replay experiments. Template aliases and the node name are placeholders.

`trace` reports LLM span counts and recorded model duration. `validate --inputs`
checks the trace and command prediction records. It does not contact CubeSandbox
or verify the task image. `plan` prints the exact
expanded execution configurations as JSON.

## 2. Understand the configuration

The complete starting file is [getting-started.yaml](../examples/experiments/getting-started.yaml).
It uses the following sections:

| Section | Meaning |
| --- | --- |
| `workload` | Task identities, prompts, replay files, repository revisions, repetitions |
| `agent.driver` | `openclaw` runs the real agent for both API calls and replay |
| `inference` | `api` obtains live model responses; `replay` supplies recorded responses and timing |
| `runtime` | Template and fixed resources for the VM hosting the agent |
| `sandbox` | Template, workspace, and fixed resources for the VM executing tools |
| `execution` | Concurrency levels, arrival schedule, random seed, timeouts |
| `resources` | Host memory budget, safety headroom, prediction inputs, optional snapshot storage |
| `policies` | Named combinations to compare |
| `validation.command` | Command that checks the final Tool workspace; exit status zero means success |

There is one agent loop: OpenClaw. Replay replaces model responses, not tool
execution. The agent issues tool calls normally in both modes.

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
predicted policies require independently collected command prediction data.
Set `--p90-kb` to that file before running them.

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

The only replay format is the original ClawTune schema-6 JSONL file produced by
the Runtime sidecar. Each LLM call has a `span_start` and `span_end` with matching
`trace_id` and `span_id`. The start contains `input.messages`; the end contains
`output.content`, `duration_ns`, and completion status. Tool spans and resource
records stay in that file, but do not drive replay. Select one agent run per file.

ClawBox does not convert or rewrite this recording. Replay checks recorded message
history, returns the recorded assistant output (including tool calls), and waits
for the recorded model duration. The native format records messages rather than
the entire HTTP request; gateway HTTP evidence is stored separately.

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

To reuse an installed ClawTune setup, reference its configuration directly:

```yaml
agent: {driver: openclaw}
inference:
  backend: api
  configuration:
    clawtune_config: /path/to/ClawTune/swe_rebench/config.yaml
```

ClawBox uses ClawTune's own configuration loader to obtain the model, endpoint,
and credential. You do not need to copy the API key or configure it again.
Keep this reference when switching the same task to replay.

Alternatively, configure a model directly. Set `agent.driver: openclaw` and
`inference.backend: api`. In `inference.configuration`, set `base_url` to your
provider's OpenAI-compatible API endpoint, `model` to its model identifier, and
`api_key_env` to the name of an exported credential variable. Keep the secret out
of YAML. The workload's prompt and Tool image define the task; replay input is
not consumed during live inference.

After a run, the original sidecar files are collected under
`runtime-traces/<session-id>/` in the result directory. Select the JSONL containing
the agent's LLM spans, inspect it with `clawbox experiment trace`, then use
`configure --trace` with `--inference-backend replay --time-scale 1`.
Keep the agent version, runtime
configuration, original task prompt, tool image, repository, and initial workspace
the same. Recording and replay share the validated Runtime settings: `/workspace`,
the original task prompt without an added prefix, and the same SSH and model
capability settings. OpenClaw and ClawTune use the configured model name and
the same per-session runtime identity, allowing the sidecar to join proxy
requests to model events and record their messages.

Replay matches incoming requests after normalizing known runtime metadata such
as session identifiers and timestamps. A valid JSONL file alone cannot establish
that the request contents match. Missing, extra, or different requests fail the
run. Workload-specific exceptions for package errors and installed files are not
applied. Use the original file from a live run. Do not rewrite recorded inputs or
outputs to make a different execution pass.

Tool sessions use `PYTHONHASHSEED=0` in both live and replay runs. This keeps
Python string hashes and hash-dependent iteration repeatable. It does not make
network responses or explicitly randomized programs deterministic; their outputs
must still match for the recording to pass replay.

Agent prediction files contain command records with `command`, `predicted_command_memory_p90_mib`, and
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
keep the shell connected or use `nohup`:

```bash
nohup clawbox --output-root /data/clawbox-results experiment run local.yaml \
  --run-id marker-02 > /data/clawbox-results/marker-02.log 2>&1 < /dev/null &
```

It checks inputs
before creating VMs, uses the selected concurrency and workload, and replays the
full input. There is no implicit truncation or injected stop response. Use a
complete shorter recording if you need a shorter task.

The two example templates must match 2 vCPU/2 GiB and 2 vCPU/4 GiB respectively.
Changing VM sizes requires corresponding templates and provenance. Pool budgets
and emergency free-memory limits must fit the host's actual available capacity.

## 6. Inspect results and failures

For a prepared single-task configuration, run the live/replay verification matrix:

```bash
python scripts/verify-agent-roundtrip.py task.yaml \
  --clawtune-config ../ClawTune/swe_rebench/config.yaml \
  --output /data/clawbox-results/agent-verification-01
```

This runs c1 and c4 with fixed/resident, fixed/immediate/on-demand, and
capacity/resident command reservations. Every successful live run is followed
by replay of its own unmodified recordings. For c4, each agent gets its own
recording. The output directory must be new. `verification.json` records each
completed run; the individual run directories retain all experimental evidence.
Use `--estimate fixed|capacity` and `--idle resident|immediate` to select a subset.
Use `--concurrency 1` or `--concurrency 4` to repeat just one concurrency level.
To repeat only replay after a fix, add `--recordings-root` pointing to an earlier
verification directory and choose a new `--output`. The script reads the original
live configurations and recordings; it does not make new API calls or edit traces.
This is a correctness check, not a policy performance comparison: live model
outputs can differ. For performance comparisons, replay the same resident-run
recordings across policies with identical initial task images and resource limits.

```bash
clawbox --output-root /data/clawbox-results experiment status marker-01
clawbox --output-root /data/clawbox-results experiment report marker-01
clawbox --output-root /data/clawbox-results experiment collect marker-01
```

`status` lists available arm results, including before the final summary exists.
An incomplete summary does not prove the worker is still running.
`report` prints the generated Markdown summary, and
`collect` returns the complete summary as JSON. The result directory contains:

| Path | Contents |
| --- | --- |
| `summary.json`, `summary.csv`, `summary.md` | Combined arm results, configuration/provenance, and summaries |
| `arms/` | Individual results and completion markers |
| `events/` | Memory samples, admission, lifecycle, and session events |
| `model-gateway/` | HTTP request, response, and replay matching evidence |
| `runtime-traces/` | Unmodified ClawTune recordings collected from each Runtime |
| `policy-control/`, `tool-artifacts/` | Tool admission, measurements, and validation evidence |
| `owned-sandboxes.jsonl` | VM ownership for cleanup and failure investigation |

Summary files are written when the worker finishes; inspect per-arm files and
events for a run that has not produced a summary yet. A failed or interrupted run
is not evidence of success. Preserve its directory before starting another attempt.

ClawBox lifecycle and policy measurements remain separate from ClawTune traces:
VM creation and destruction, checkpoint and restore spans, physical memory,
NUMA placement, snapshot tiers, reservation waits, and policy decisions are saved
in events and results. Tool-level eBPF and cgroup evidence and execution-ID joins
remain in tool artifacts. Changing the replay input format does not remove these
measurements or insert them into the native ClawTune recording.

OpenClaw can reject an `exec` call before sending it to the Tool VM. These
preflight rejections remain in the native trace and are counted separately from
executed commands; they cannot have Tool VM resource measurements. A cancelled
command that did execute must still finish its telemetry and admission record.

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
