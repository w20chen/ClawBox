# Full 1280 failure debugging handoff

Prepared on 2026-10-28 for an independent code agent. The task is to diagnose and fix the failures observed in the saved Full-1280 experiment on Kunpeng, prioritizing effects on experimental validity. No fixes or new experiments were performed while preparing this handoff. All observations below come from existing logs, artifacts, replay inputs, and source inspection.

The main finding is that the failed sessions combine genuine execution problems with overly strict or incomplete validation. Do not treat all 82 failures as 82 independent bugs, and do not make them disappear by broadly accepting failures. Some immediate mechanisms are confirmed; several underlying causes remain unresolved and are identified explicitly below.

## Scope and operating constraints

- Read the repository's `AGENTS.md`. This is a paper project: make scoped repairs that affect the results; avoid unrelated hardening, repeated rebuilding, or repeated full experiments.
- The supported execution path is the standalone `clawbox experiment` CLI with CubeSandbox and ClawTune. Do not introduce another experiment runner or extend deprecated control-plane modules.
- Preserve the existing optimizations, templates, configurations, raw evidence, and unrelated workloads. Do not overwrite this run or reuse its run ID for a changed implementation.
- The user required existing default logging only. `control_timing_details=false` was retained. Do not add heavy tracing, profilers, or high-frequency polling to a performance comparison. Offline analysis of saved files is appropriate.
- The user stopped the experiment and requested results for its first 22 minutes. The experiment is over; do not resume the cancelled run. An eventual new experiment must have a new run ID and explicit implementation/configuration provenance.
- Do not silently weaken validation, replace missing observations with zero, conflate resource reservations with physical consumption, or classify sessions unfinished at the cutoff as failures.
- Public command/configuration changes require corresponding walkthrough/reference updates under `AGENTS.md`. Keep this debugging evidence separate from user documentation.

## Run identity and reproduction context

Local repository: `C:\Users\29068\Desktop\ClawBox`. Remote access: `ssh kunpeng`.

| Item | Exact value |
|---|---|
| Evidence root, referred to below as `ROOT` | `/data/full1280-default-20261028T0828Z` |
| Run ID | `full-c1280-default-20261028T082135z` |
| Attempt | `attempt-21032b43c3b94860` |
| Arm | `54f54e53e94ca302a3392632` |
| Artifact directory, referred to below as `ARM` | `ROOT/runs/full-c1280-default-20261028T082135z/attempts/attempt-21032b43c3b94860/54f54e53e94ca302a3392632` |
| Session ID convention | Arm ID followed by a four-digit suffix, e.g. `54f54e53e94ca302a3392632-0010` |
| Frozen ClawBox source | `ROOT/source`, commit `f0848878832ce25a44392e9f38ada39e7937ff4a` |
| Source archive | `/data/full1280-default-20261028-source-f084887.tar` |
| Archive SHA256 | `f525507c4ed264c86ce0fc94b1505c9274a01ccb41cbde5e879ca297db5d699f` |
| Workload manifest | `/data/clawbox-service-overhead-20261026T1715CST/combined/collector-refresh/workload-collector-refreshed.json` |
| Machine settings used at launch | `/data/clawbox-create-diagnosis-20261027T1508CST/machine.env` |
| Frozen configuration and qualification | `ROOT/experiment.yaml`, `ROOT/qualification.json` |
| Launch provenance | `ROOT/manifest.json` |
| Report cutoff receipt | `ROOT/user-cutoff-22min.json` |

The name of the evidence directory is not a reliable start timestamp. Use the cutoff receipt: origin monotonic time `94961.646700025 s`, cutoff `96281.646700025 s`, duration exactly `1320 s`. The wall-clock reporting interval was **2026-10-28 16:21:46.024 through 16:43:46.024 Asia/Shanghai**, equivalent to 08:21:46.024 through 08:43:46.024 UTC. Cancellation occurred later, so raw logs contain post-cutoff events. These may help debug causes but must not be included in the 22-minute performance distributions.

The user explicitly chose the saved Full-1280 settings. Important conditions were:

- 1,280 offered sessions, one Runtime VM plus one Tool VM per session; Poisson arrivals with expected window `741.1812062263489 s`, seed `20261025`, replay time scale `1.0`.
- 16 concurrent preparation slots and 16 VM-pair creation slots; arm timeout `5400 s`.
- Two LOCAL NUMA domains: node 0, CPUs 0–79; node 1, CPUs 80–159. Each had 500 GiB configured capacity, LOW 448 GiB and HIGH 480 GiB. Combined admission budget was 960 GiB.
- Shared/WARM budget 100 GiB on NUMA node 2. Incremental-CoW tiered checkpoints, shared borrowing, C2 CPU placement, template affinity, 4 MiB small-tool admission bypass, and existing native cgroup-birth/IRQ optimizations were retained.
- Saved resource sampling interval `1.0 s`; extra control timing disabled. This is the saved run setting, not necessarily the schema's general default.
- The Runtime base template was `tpl-07ef242fbf8b40468b1404ce`. The generic Tool template in experiment YAML was `tpl-54c3784270154d41a3c9c6a3`, but **individual workload cases override Tool templates and workspace settings**. Inspect `tasks[].sandbox` in the workload manifest rather than assuming every case used the generic image.
- Inference was replay, not a fresh live-model reasoning run. A recorded final answer is not evidence that the current tool execution succeeded.

The manifest records the exact historical launch command. Do not run it again with this ID. The frozen source is the authority for this run; the shared remote checkout or a later local HEAD may differ.

## Evidence map and quick start

The following paths are relative to `ARM` unless otherwise stated:

| Evidence | Purpose |
|---|---|
| `events/54f54e53e94ca302a3392632.jsonl` | Approximately 540 MB of host events; timestamp field is `monotonic_time_ns`, not `monotonic_s` |
| `runtime-traces/SESSION/*.jsonl` | Raw and normalized runtime tool records, including rejected calls and tool outputs |
| `openclaw/SESSION/final-answer.json` | Agent summary, failure warning, stop reason, incomplete-turn evidence |
| `openclaw/SESSION/agent.stderr.log` | Runtime errors, tool rejection messages, early-exit diagnostics |
| `openclaw/SESSION/policy-ssh.jsonl` | SSH shim stages and policy request/route evidence |
| `policy-control/SESSION.json` | Admission requests, errors, completion records, and timing |
| `tool-artifacts/SESSION/` | Tool bridge and per-execution cgroup resource artifacts |
| `model-gateway/SESSION.json` and `.deliveries.jsonl` | Model replay accounting and response delivery |
| `owned-sandboxes.jsonl` | VM ownership and lifecycle ledger |
| `worker.stdout.log`, `worker.stderr.log` | Worker output; absence of an exception here does not establish sampler health |
| `ROOT/analysis-22min-final/failures.json` | The 82 session failures inside the cutoff |
| `ROOT/analysis-22min-final/summary.json` | Derived report statistics, not a replacement for raw evidence |
| `ROOT/cleanup-verification.json`, `ROOT/user-abort.log` | Cancellation and cleanup evidence |

Local report and audit files are under `.artifacts/full1280-20261028/`:

- `analysis-22min-final/report.html`: self-contained English report with six figures and metric definitions.
- `full1280-first-22min-report.zip`: original report bundle. It predates this debugging audit and does not include this handoff or the new audit JSON.
- `audit_failures.py`: read-only offline comparator using the frozen remote parser and validator helpers. It reads the 82 failures, original replay traces, and copied runtime traces; it does not execute workload commands.
- `failure-debug-evidence.json`: comparison output with expected/actual content, per-case trace paths and SHA256 values, raw failed spans, and filtered failed spans. Read with `encoding='utf-8-sig'` because PowerShell may write a BOM.

A portable bundle, `.artifacts/full1280-20261028/full1280-debug-handoff-20261028.zip`, contains this document, the audit script/output, the failure list, and launch/cutoff/cleanup provenance. Archive entries retain their paths relative to the repository root. The full raw logs remain on Kunpeng; they are not duplicated in this bundle.

To inspect the saved run from a fresh remote shell, these commands are read-only:

```sh
ssh kunpeng
ROOT=/data/full1280-default-20261028T0828Z
ARM="$ROOT/runs/full-c1280-default-20261028T082135z/attempts/attempt-21032b43c3b94860/54f54e53e94ca302a3392632"
python3 -m json.tool "$ROOT/analysis-22min-final/failures.json"
python3 -m json.tool "$ROOT/runs/full-c1280-default-20261028T082135z/run-state.json"
```

To reproduce the offline comparison from the local PowerShell repository root, write to a new output file:

```powershell
Get-Content -Raw .artifacts/full1280-20261028/audit_failures.py |
  ssh kunpeng python3 - |
  Set-Content -Encoding utf8 .artifacts/full1280-20261028/failure-debug-evidence-recheck.json
```

This was exercised successfully against the frozen source. The comparison's categories are mechanical differences, not automatic root-cause labels. The same call ID can have multiple span variants. Inspect the actual records and the production validator rather than equating span counts with session counts. On this host `rg` was unavailable in the fresh remote shell; use Python file iteration as needed. Never print the whole machine environment: it may contain credentials.

## Observed failures and their significance

At the cutoff: **68 sessions passed configured validation, 82 failed, and 1,130 were unfinished**. The last group is right-censored, not failed. The five terminal-error groups are mutually exclusive by the first reported session failure; underlying symptoms may overlap.

| Terminal error | Sessions |
|---|---:|
| `unexpected replay Tool failure: ...` | 45 |
| `Runtime Tool failure evidence does not cover OpenClaw failure signal` | 32 |
| `managed ModelGateway session incomplete: ...` | 3 |
| `policy admission failed ... ApiError: HTTP 408` | 1 |
| `cgroup artifact is not cgroup-v2` | 1 |

The measured 42.8212 tool-plus-model steps/s includes work belonging to failed sessions. It is not successful-task throughput. End-to-end latency is measured only for the 68 validated completions. Failures can release VMs early, alter queueing and memory occupancy, and change later throughput. A comparison must account for these effects.

Also distinguish **passing the experiment's configured validation** from solving a benchmark task. For example, the Coffea case's validation command checks the repository base commit and the existence of `/opt/conda/envs/testbed/bin/python`; it is not a full test-suite correctness check. The audit includes the configured validation command for each failed case. Inspect the full workload before making task-success claims for the completed cohort.

## Issue 1: Replay failure matching rejects variable output and also catches real divergence

**Trigger.** After the agent finishes, `worker.py` checks replay completeness and the agent's tool-failure warning. If the warning is positive, it passes normalized runtime spans to `match_recorded_tool_failures`. The matcher recognizes expected failed `exec` calls from the original replay trace and demands matching call identity, command, nonzero integer exit code, and normalized error text. It rejects signal/no-output-timeout outcomes as well.

**Confirmed mechanism.** `clawbox/replay/tool_outcomes.py::_failure` mostly returns `(exit_code, entire_stripped_output)`. It removes exit-code trailers and has narrow normalizations for missing directories and injected shell line offsets. It does not generally separate a stable failure signature from variable output. `match_recorded_tool_failures` around lines 109–125 compares these tuples exactly.

Two concrete text-comparison failures:

1. Session `0010`, case `CoffeaTeam__coffea-572`, call `call_11_0`: `ls -la /usr/bin/python* /usr/local/bin/python* 2>/dev/null`. Both recorded and observed exit codes are **2**, with the same command. The recorded listing has binary size **6,839,928** and mode `-rwxr-xr-x`; the observed listing has **6,618,336** and `-rwxr-xr-x.`. Exact listing-text equality causes rejection. This is a confirmed comparison mismatch, not evidence that ClawBox failed to launch the command.
2. Session `0124`, case `briancurtin__deprecation-37`, call `call_11_0`: both runs exit **2** with the same `unittest2` collection error involving `collections`. Recorded pytest/pluggy versions are **9.1.1/1.6.0**, observed **9.2.0/1.7.0**; elapsed time changes **0.11 s → 0.36 s**. Version banners and elapsed time change the tuple even though the inspected exception is the same. Package-version differences also establish that relevant environments were not textually identical; why these versions differ is not yet traced.

Do not generalize these two examples to all 45 sessions. Other saved evidence includes:

- Session `0247`, `reanahub__reana-client-606`: recorded submission command succeeded, but actual `cat patch.txt` failed because the file was absent. Investigate preceding edits and patch creation. This is actual output divergence, not just timestamp normalization.
- Session `0116`, `awdeorio__mailmerge-95`: a recorded Python-script command was refused by runtime preflight with `complex interpreter invocation detected; refusing to run without script preflight validation`. A returned span reports an error while its exit-code field is **0**. Other cases include the same preflight rejection with null exit code. A refusal is not a successful command merely because an exit-code field is zero.
- Some cases have missing `pytest` or different test outcomes/exit codes. The original trace itself can contain expected environment failures, so the mere presence of `python: not found` or a missing package is insufficient to identify a new defect.

**Next investigation.** Use the audit's expected/actual pairs and `source_trace` paths. Separate cosmetic output variation, genuinely different error classes, runtime preflight rejection, missing artifacts, and input/environment mismatch. Inspect the complete trajectory before the first rejected call. Determine whether the case template, shell, cwd, package installation, and filesystem operations match the recorded workload. Do not execute replay snippets outside their intended disposable environment.

**Repair boundary.** A narrowly defined semantic comparison may be appropriate for demonstrably equivalent failures. Broad acceptance by exit code alone would hide missing packages, refused commands, new exceptions, timeouts, and missing patches. Retain the distinction between an expected workload failure and a new execution failure. Any deliberate change to what constitutes valid replay must be documented as an experimental semantics change.

**Validation.** Reproduce the Coffea and deprecation comparisons from saved fixtures; show equivalent variable text is handled as intended while a different command, failure class, exit code, timeout, or missing expected failure is still rejected. Investigate the patch-missing and preflight examples separately before treating this whole group as resolved.

## Issue 2: Failure validation loses requests that never became native executions

**Trigger.** The agent reports tool failures, but the list supplied to the replay matcher contains no matching failed execution. This occurred in 32 sessions.

**Confirmed mechanism.** `clawbox/experiments/native_artifacts.py::_runtime_spans` around lines 256–312 intentionally excludes records without a recoverable native execution ID and requests explicitly rejected before execution. That is appropriate for joining native executions to admission and cgroup artifacts: a command that never ran cannot have those artifacts. However, `worker.py` around lines 4640–4654 reuses that filtered list to explain the broader agent failure warning. `tool_outcomes.py` around line 133 raises the coverage error when no failed span was matched. This loses the more useful rejection evidence.

**Observed subgroups.**

- **30 sessions** have raw tool records saying `command was not executed`. Session `0227`, `nilearn__nilearn-4701`, call `call_15_0`, specifically reports `workdir "/workspace" is unavailable or not a directory`. Tool templates may use `/testbed` while the Runtime default is `/workspace`; this is a concrete configuration lead, not yet proof of the source of every rejected cwd. Inspect effective requested cwd, runtime configuration, and any preceding directory checks.
- The other two sessions have raw read/edit failures without native execution IDs. Session `0244`, `pytorch__ignite-242`, includes edit calls `call_9_0`/`call_10_0` with error code **125** and read `call_12_0` reporting `ssh exited with code 125`. Session `0231`, `online-judge-tools__oj-896`, has edit `call_14_0` reporting the same SSH exit code. Their normalized failed-span lists are empty. No `error`/`*_failed` entries were found in their saved policy-SSH audit files during this inspection, so those audit files alone do not establish why SSH returned 125.

**Unresolved underlying cause.** The filtering/reporting mechanism is confirmed. The reason for each unavailable cwd or SSH failure is not. Exit 125 is not a unique CubeSandbox error: `scripts/clawbox-policy-ssh.py` returns it from several failure branches, including admission/route and completion failures. Do not label every 125 as a memory-admission failure. Runtime preflight and filesystem operations require their own evidence path.

**Repair boundary.** Keep the strict native-execution join. Preserve and classify rejected requests separately when explaining agent failures. Report the underlying cwd, preflight, or transport error instead of claiming no evidence exists. Do not invent execution records for commands that never ran or automatically mark those sessions valid.

**Validation.** Offline fixtures must cover a rejected exec request, read/edit error without execution ID, and a true native execution with missing artifacts. Confirm that the first two retain their actual error and the last remains rejected. Use a small real-VM check only if a cwd/transport repair needs it.

## Issue 3: The agent ends before replay completion

**Trigger and cases.**

| Session suffix | Case | Expected model steps | Observed |
|---|---|---:|---:|
| `0071` | `PennyLaneAI__pennylane-5857` | 56 | 13 |
| `0069` | `PennyLaneAI__pennylane-5781` | 52 | 16 |
| `0418` | `tobymao__sqlglot-4007` | 29 | 11 |

All three completeness records have no missing/failed request IDs, `replay_failure=null`, zero HTTP retries, and delivered required responses for the requests actually made. This does not establish that the whole replay was delivered; the agent stopped requesting remaining steps.

Sessions `0071` and `0418` explicitly log `incomplete turn detected`, `stopReason=toolUse`, and `replaySafe=no`. Session `0071`'s final record reports an `incomplete_turn` error and abandoned state. Session `0069`'s final record still says `livenessState=working`, `stopReason=toolUse`; do not describe it as the identical abandoned-state case. Session `0418` also logged several read errors with SSH exit 125 before termination.

**Root-cause status.** Premature runtime termination is confirmed. The reason the agent loop returned instead of continuing is not established. Tool/transport failures may contribute, but their causal role has not been proven for all three. There is no evidence here for an external model-provider outage.

**Next investigation.** Compare the last delivered replay response, its tool calls/finish reason, subsequent tool results, and final runtime status. Inspect `model-gateway/SESSION.json`, delivery JSONL, the original trace, and agent stderr together. Locate the deployed OpenClaw code emitting `incomplete turn detected`; verify its version against the Runtime image before changing a convenient checkout. `worker.py` around line 4634 correctly rejects incomplete replay; changing that check would conceal the failure.

**Validation.** Reproduce the final-turn sequence with an offline runtime fixture if possible. A real-VM replay should consume the expected sequence exactly once and end normally; never pad the step count or treat the delivered prefix as a complete session.

## Issue 4: One admission callback propagates an API timeout

Session `0110`, case `asottile__dead-21`; execution ID `exec-93d852e3-c7fc-45f5-8115-cc02eed20b4d`.

`policy-control/SESSION.json` records an admission starting at monotonic `95336.935986877 s`, ending `95462.142152514 s`: **125.206165637 s**. `admission` and `completion` are null; `admission_error` is `ApiError: HTTP 408`.

The runtime SSH audit reports **HTTP 503 Service Unavailable** for that same execution ID. These are different layers of the same failure: `policy_control.py` maps uncategorized callback exceptions to HTTP 503, while retaining the original callback exception in its execution record. `scripts/clawbox-policy-ssh.py` catches the failed admission request and returns 125. Do not mistake the 503 for a separate root cause or attribute the entire 125-second interval to pure queueing.

**Root-cause status.** Propagation from callback error to HTTP response is confirmed. The underlying API call that produced 408 has not been identified. A Cube lifecycle/restore operation is an investigation lead, not a proven diagnosis. Increasing the timeout without locating the failing request would not establish a fix.

**Next investigation.** Correlate this execution ID and session with `tool_admission_queued`, `tool_admission_failed`, `tool_admission_timing`, VM lifecycle operations, and any retained Cube service logs over that interval. Inspect admission callbacks in `worker.py` and policy/lifecycle code. Preserve policy failure propagation and incomplete-operation validation in `native_artifacts.py::validate_native_tool_join`.

## Issue 5: Tool resource collector falls back to process-tree accounting

Session `0267`, case `smartystreets__smartystreets-python-sdk-28`; execution ID `39730de0-1912-49f3-85c4-0de0ac0afdb0`.

Inspect `tool-artifacts/SESSION/cgroup-resource-39730de0-1912-49f3-85c4-0de0ac0afdb0.json`. It reports:

```text
schema = cgroup_resource_v1
source = process-tree
fallback_used = true
monitor_source = psutil-process-tree
cpu_source = memory_source = disk_source = procfs-process-tree
sampling_quality = degraded
cgroup_setup_error = null
cgroup_read_error = null
collector_errors = []
```

**Confirmed mechanism.** `native_artifacts.py::_validate_cgroup` around line 399 requires `source == "cgroup-v2"` and rejects the fallback. The message does not mean the host is running cgroup v1. The artifact has a cgroup path but used another accounting source, and it does not record an explicit setup/read error explaining the fallback.

**Root-cause status.** Why the collector selected fallback is unknown. A short-lived process/cgroup race is only a hypothesis. The correct response is not to rename the source field or accept fallback as equivalent measurement.

**Next investigation.** Follow the exact execution through Tool bridge and collector records, including start/end timing, cgroup creation, process attachment, sampling, and removal. ClawTune source referenced by the launch machine settings includes `/data/clawbox-service-overhead-20261026T1715CST/v5/source/ClawTune`; its relevant module is `services/sidecar/src/clawtune_sidecar/telemetry/cgroup_resource.py`. The environment file has multiple historical `CLAWTUNE_ROOT` assignments. Verify deployed image/collector provenance rather than assuming the first assignment or that source tree is the running collector. The workload's collector receipts include revision and manifest/image hashes.

**Validation.** If a collector race is demonstrated, use a targeted short-command fixture and a small native Tool check. Keep agent-tool measurement requirements distinct from explicitly permitted backend-maintenance fallback rules. Do not relax all measurements to accommodate this one case.

## Issue 6: Cancellation leaves stale per-arm status

**Trigger.** The supported abort command is used while an arm is running.

**Evidence.** Saved `cleanup-verification.json` reports overall state `cancelled`, arm status `running`, and `cleanup_verified=true`. The owned VM parent cgroup reported `populated 0` and `frozen 0`; worker/supervisor termination was checked at cleanup. This is historical cleanup evidence, not a promise about unrelated current host activity.

**Confirmed root cause.** `supervisor.py::abort_run` terminates the supervisor, invokes `cleanup_run`, then sets top-level state to `cancelled`. `cleanup_run` updates per-arm cleanup fields but does not transition the arm's running status. Thus a successfully cancelled/cleaned run can still display a running arm.

**Repair and validation.** Update only the statuses that should transition on successful cancellation, preserve already-terminal outcomes and cleanup failures, and test cancellation with running versus already-terminal arms. This is lower priority than workload validity. It does not justify rerunning Full-1280 or claiming a VM leak.

## Issue 7: Resource sampling drifts and then stops while work continues

**Conditions and observations.** The saved interval is 1 second. Within the first 22 minutes, there are 507 emitted memory samples, mean spacing approximately **2.606 s**, median **2.031 s**, P90 **4.556 s**, maximum **19.011 s**. The final sample inside the window is at **1319.781806 s** relative to origin.

The full saved log has two later memory samples, at **1322.314314 s** and **1325.900091 s**, and none thereafter. Tool completion events continue to **1862.409337 s** and model-response events to **1863.572857 s**. Therefore the long sampling stop begins just after the selected performance window; sparse intervals already occur inside it.

**Confirmed implementation properties.**

- `memory.py::NodeMemorySampler._run` waits `interval_s`, then calls `current()`, appends a sample, and synchronously invokes `sample_hook`. The actual cycle is wait plus collection/hook work; it is not a fixed-rate one-second scheduler.
- `worker.py::record_memory_sample` around lines 2416–2463 gathers domain memory, snapshot pool state, topology placement, filesystem usage, queue snapshots, policy metrics, NUMA residency, CPU data, and other fields sequentially before emitting one event. The event is not an atomic simultaneous snapshot. Queue-counter disagreement with event reconstruction is not by itself a scheduling bug.
- `_run` catches an exception, stores `_sampling_error`, and exits. `stop()` raises that error later. A failing callback can therefore stop resource events without immediately stopping other work. A callback blocked on a lock or I/O can also prevent emission. Neither mechanism has been proven to explain this particular stop.

**Root-cause status.** The scheduling structure explains why configured interval differs from observed cadence. The exact expensive or blocking operation, or exception responsible for the prolonged stop, is unresolved. There is no saved sampler stack identifying it. Do not assert a deadlock solely from a flat curve or silence.

**Next investigation and validation.** Inspect callback lock ordering, synchronous filesystem/topology reads, and exception handling using existing source and logs. Reproduce slow-hook and failing-hook behavior with focused tests before changing the sampler. Ensure a repair does not silently lose required evidence or introduce heavy per-VM polling. Distinguish measured sample peaks from true instantaneous peaks. Do not interpolate the post-1325.9-second gap as if continuous measurements existed.

## Recommended order and completion criteria

1. Start offline with Issues 1 and 2. They account for 77 terminal failures, but include several underlying causes. Produce an explicit classification of expected failures, new runtime failures, and validation mismatches before changing acceptance rules.
2. Investigate the actual preflight/cwd/read/edit failures and early exits together where the same session evidence connects them. Confirm deployed Runtime/Tool image and collector provenance before changing templates or dependencies.
3. Trace the single API timeout and resource fallback only as far as needed to establish their effect and a scoped remedy. Keep unknown causes marked unknown until evidence identifies them.
4. Repair status metadata separately; investigate sampler failure visibility/cadence without turning the experiment into an instrumentation project.
5. Use saved-event fixtures and targeted tests first. Use a small real-VM validation where semantics cannot be established offline. Small checks establish correctness only; any claim about changed performance at 1,280 concurrency requires appropriately comparable evidence. Do not repeatedly launch a full run merely to test formatting, status, or parser changes.

For each repair, report the triggering condition, confirmed cause, changed behavior, evidence/test that demonstrates the fix, remaining uncertainty, and whether an existing result can still support its original claim. Preserve this run as evidence of the observed implementation; any revised interpretation or reclassification should be a separate derived artifact with its rules stated. The existing 22-minute report remains a description of activity under this run's failures, not proof of clean workload completion or a final whole-run success rate.
