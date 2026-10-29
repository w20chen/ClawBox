# Read results and diagnose overhead

Use `experiment status RUN_ID` for supervisor state and `experiment report RUN_ID`
to regenerate the summary. Use the same host output root as launch, and use
`bash scripts/clawbox --sudo experiment report RUN_ID` for root-owned runs.

## Run artifacts

| Path under the run directory | Contents |
| --- | --- |
| `experiment.yaml`, `qualification.json` | Frozen input and qualification evidence |
| `run-state.json` | Supervisor state, active arm, ownership, and cleanup status |
| `arms/*.json`, `arms/*.complete` | Arm results and verified completion markers |
| `summary.json`, `summary.csv`, `summary.md` | Compact result index and aggregated results |
| `memory-timeseries.csv` | Sampled host cgroup and NUMA memory |
| `attempts/ATTEMPT/ARM/` | Events, logs, telemetry, gateway records, and VM ownership |

Retain the whole run directory and external immutable inputs needed to reproduce
it. A repeated run ID resumes its frozen specification; a changed specification
needs a new ID. Cleanup commands remove owned VMs, not these files.

`summary.json` indexes canonical `arms/*.json` results instead of embedding a
second copy. `experiment status` reads the compact index; `experiment collect`
expands the references and returns the complete arm results on demand. The
worker's attempt-level result is a transient supervisor handoff and is removed
after the cleanup verdict is stored in the canonical arm result.

Before comparing arms, check submitted/completed sessions, final task validation,
failures, timeouts, OOMs, execution-ID joins, telemetry coverage, and cleanup.
Keep workloads, starting workspaces, templates, hardware scope, arrivals, and
evidence fixed. Report completion counts with latency percentiles: statistics
from completed calls alone omit outstanding waits and unfinished sessions.

Throughput and completion latency are primary outcomes. Tool time, admission
wait, memory, placement, prediction error, and checkpoint cost explain them.
Missing result measurements are unavailable, not zero. See the
[execution model](design.md) for capacity, prediction, and PMU semantics.

## Completion endpoints

`offered_to_final_response_*` measures scheduled arrival to host observation of
the final response. It includes transport and completion notification delay,
but excludes subsequent collection, validation, and cleanup.
`offered_to_completion_*` ends at `final_agent_completion`, after collection and
replay checks but before the separate final task-validation command.

Use `final_response_observed.observed_monotonic_s` as the start of finalization.
`session_collection_timing` joins by session ID and covers Runtime log/trace
transfer, Tool evidence export, parsing, and evidence validation. Its root span
also contains the earlier agent run. For **final response to validated result**,
measure through `session_complete` with `valid: true`; the result timeline also
records `validation_start` and `validation_end`. Inspect cleanup separately;
final-response receipt alone does not establish a validated successful task.

## Logging and diagnostic detail

`execution.control_timing_details` defaults to `false`. Normal runs retain:

- Task traces, results, errors, execution IDs, and final validation.
- Required Tool resource/PMU collection and periodic host memory samples.
- Compact callback timings, queue-wait totals, and lifecycle memory totals.
- VM-create request and ownership-journal timing spans.

Set the field to `true` for a diagnostic run to add nested control spans,
GC intervals, raw lifecycle cgroup dumps, duplicate model/prediction event
payloads, and Runtime working-KB exports. Use a new run ID. No host apply,
image rebuild, or retraining is required. Disabling detail does not disable
ClawTune measurement or task validation.

## Tool preparation and admission

`tool_admission_timing`, `tool_completion_timing`, and `tool_progress_timing`
join by session and execution ID. Compact events contain `duration_seconds`,
`thread_cpu_seconds`, and `resource_queue_wait_seconds`. Admission also contains
`preparation_seconds` and `preparation_resource_queue_wait_seconds`.

Separate resource waits from implementation cost. Queue totals exclude measured
accounting-lock reacquisition. `queue_wait_seconds_by_reason` distinguishes direct
capacity blocks from FIFO, same-execution, CPU-capacity, and eviction waits. FIFO
or eviction waiting alone does not establish a memory shortage. Even a sleep
with a capacity blocker may include wakeup scheduling delay.

`tool_admitted.control_total_seconds` spans the worker callback, including
dependency-install waiting, locks, restore, memory admission, routing, and
bookkeeping. `admission_blocked_seconds` covers the memory-admission wait only.
Neither includes HTTP transport before the callback; use the guest/control
journal for that interval. The retained `memory_baseline_timing` separates claim
lookup, the kernel memory sample, and baseline publication, with wall and
calling-thread CPU time. Current lookup/publication use identity-checked
snapshots; the older lock-wait/hold fields remain zero.

| Queue | Evidence | Interpretation |
| --- | --- | --- |
| Resource preparation | `sandbox_prepare_gate_*` | Waiting to enter bounded preparation, before memory admission |
| Actual VM creation | `sandbox_create_gate_*` | Prepared sessions waiting for a creation slot |
| Memory admission | Memory samples by `create`, `restore`, `tool_admission` | Capacity decisions and last-observed blockers |
| Dependency installation | `dependency_install_gate_*` | Optional limit held until the SSH install command is reaped |
| Lifecycle transfer | Lifecycle timing spans | Configured operation slots, not free-memory waits |

Memory samples report queued tasks and resident VMs separately, including oldest
wait age. A queued session can have zero, one, or two resident VMs. Preparation
and actual creation are independently bounded; a memory-blocked preparation
does not hold a creation slot.

Use `memory_sample.observation_start_monotonic_s` and
`observation_end_monotonic_s` for the memory-reading interval;
`observation_age_seconds` records its age when the event is prepared. Queue,
filesystem and CPU fields are collected afterward, so the event is not an
atomic snapshot. `memory_sampling.samples` is the complete core memory series.
Rich `memory_sample` events are prepared by a serial latest-only reporter and
may be coalesced under load; `memory_sample_reporting` records submitted,
processed and coalesced counts plus callback time. The core sampler targets the
configured start-to-start interval and skips missed ticks rather than producing
catch-up bursts. Report sampled peaks and actual timestamp coverage rather than
treating gaps as continuous measurements.

Compute-domain `outstanding_bytes` reuses the most recent admission observation
whose claim identities and baselines still match. Its
`outstanding_observation_start_monotonic_s`,
`outstanding_observation_end_monotonic_s` and
`outstanding_observation_age_seconds` describe that separate reading.
If a current measured claim lacks a matching observation, `outstanding_bytes` and
`remaining_budget_bytes` are unavailable; `outstanding_observed_claims` and
`outstanding_total_claims` show coverage. The separately named
`outstanding_nominal_upper_bound_bytes` is a reservation bound, not measured
consumption. These reporting fields do not replace admission's live checks.

C2 placement runs independently of memory collection at the same configured
interval. Use its `tool_call_cpu_placement` and `topology_cpu_group_load` events
for placement changes. A sampler or placement-loop exception fails the arm
instead of allowing a successful result with missing evidence.

## VM creation and startup

`sandbox_created.lifecycle_timing.create_attempts[].spans` separates `sdk_create`
from `journal_lock_wait`, `journal_open`, `journal_write_flush`, `journal_fsync`,
`journal_close`, and `journal_lock_hold`. These spans are retained by default.
They use Unix and host-monotonic start/end times, `duration_s`, and calling-thread
`thread_cpu_s`, in seconds. Failed attempts retain error types.

`api_create_s` includes SDK requests and ownership-journal work; it is not pure
VM boot time. Correlate server logs using sandbox ID and Unix timestamps to
separate server-side queueing and creation. `journal_lock_hold` contains the file
operations, so do not add it to its children.

`native_ssh_warmup_finished` separates executor queueing, execution, retries, and
identity results. `native_ssh_identity_ready.critical_path_wait_seconds` measures
the session's blocking join. Warmup overlaps Runtime startup; adding both full
durations would double-count time. Failed Runtime readiness preserves
`openclaw/SESSION/sidecar-startup.log`.

## Timing interpretation

Detailed `spans` use host-monotonic `start_ns`/`end_ns` and calling-thread
`thread_cpu_ns`, in nanoseconds. Lifecycle `stage_spans` uses the same clock.
Parent intervals contain child intervals; overlapping stages and GC intervals
must not be summed. A `.hold` span can contain a condition wait that temporarily
releases its lock, so its full duration is not exclusive lock ownership.

`dropped_spans` reports truncated detail. When nonzero, the recorded wait total
or phase decomposition can be incomplete. Compare host-monotonic timestamps
only within that host; use synchronized wall timestamps and execution IDs for
guest/server correlation. Span `status` records Python success/error; remote
command success is reported separately as `exit_code`.
