# ClawBox overhead: completed repairs and remaining optimization handoff

Prepared 2026-10-27, Asia/Shanghai. Code baseline: `8fb4760` on `main`, pushed
to `origin/main`. This is an internal continuation document, not a user guide.
It supersedes the next-work ordering and broad performance conclusions in
[the previous diagnosis handoff](FULL_SERVICE_OVERHEAD_DIAGNOSIS_HANDOFF_20261027.md).
That file and [the VM creation handoff](FULL_VM_CREATION_HANDOFF_20261027.md)
retain the earlier detailed evidence.

## 1. Start here: consequential conclusion and user direction

**Completion accounting improved substantially, but overall end-to-end overhead
is not yet small.** Do not repeat the earlier overly broad conclusion that the
whole control path is satisfactory. The 0.24-0.29-second averages previously
reported cover observed Tool end to completion notification only.

Two unfinished priorities now dominate:

1. **Before each Tool execution:** memory preparation/admission has an estimated
   2.40 / 2.85 seconds of residual time at offered concurrency 1280 / 2560 after
   subtracting recorded admission and restore-reservation waits. This accounts
   for approximately 85% / 83% of the corresponding estimated per-call overhead.
   The stage is measured; its internal attribution is incomplete.
2. **After the final response:** reaching a validated experiment result takes
   another approximately 109 / 80 seconds on the finished-session subset after
   subtracting recorded restore-reservation waits. Most is before the Tool
   artifacts-collected marker. This includes Runtime log/trace collection
   inside `run_openclaw`, not just the later Tool artifact collector.

User constraints and accepted decisions:

- The user explicitly accepted **rare long tails**: "If they are rare, we can
  ignore this." Do not pursue a hard one-second maximum or more load runs solely
  to eliminate rare completion/GC outliers. Multi-second *typical* admission and
  minute-scale finalization remain in scope.
- Exclude genuine free-memory/capacity queue waiting from avoidable overhead.
  Mutex contention, redundant work, worker/transfer queues and controller
  synchronization are not automatically memory waiting.
- This is a paper/experiment project. Keep fixes scoped, preserve result
  correctness, and avoid repeated rebuilding/rerunning for marginal gains.
- Work on ClawBox-added costs. No pure CubeSandbox baseline, Cube redesign,
  kernel upgrade/reboot, or renewed IRQ-bypass investigation. Earlier tentative
  reboot authorization was superseded; SSH is the only recovery access.
- Preserve prediction, budgets, reservation safety, NUMA/CPU placement,
  configured snapshot mechanisms, measurement semantics and auditable evidence.
  Do not lower offered load or weaken policy to make latency look better.
- Use the supported `clawbox experiment` workflow. Do not extend deprecated
  control-plane APIs or introduce another experiment execution path.
- Read `AGENTS.md`. Public command/configuration changes require the scenario
  walkthrough and option reference to be updated; internal timing changes do
  not require rebuilding guest images or retraining unchanged predictions.

## 2. Current code, deployment and host state

Important commits already pushed:

| Commit | What it contains |
|---|---|
| `b5943df` | Earlier VM cgroup-birth placement repair; preserve it and earlier native/snapshot optimizations. |
| `1b6d55c` | Base of this completion-control diagnostic series. |
| `15d2de9` | Bounded-priority admission/completion locking, detailed timing, sampling improvements, retained evidence encoding, C2 poll handoff. |
| `8fb4760` | Compressed retained evidence and control-performance handoff results. |

Local checkout: `C:\Users\29068\Desktop\ClawBox`. It was clean before this new
handoff was written. The three recent read-only analysis scripts and outputs
listed below are under ignored `.runtime/`; they are **not tracked in Git**.

Remote: `ssh kunpeng`, user `weitianc`; existing privileged experiment commands
use `sudo -n`. At **2026-10-27 21:49 Asia/Shanghai**, a fresh read showed the
experiment VM parent `/sys/fs/cgroup/cube_sandbox/sandbox/cgroup.events` as
`populated 0`, `frozen 0`. Recheck before any new run.

**Do not reset/pull over `/home/weitianc/ClawBox`.** It is an older, dirty working
checkout, HEAD `d42da58`, with substantial pre-existing changes. Our tests and
load runs used separate frozen source trees. The older checkout supplied the
existing environment/venv; it was not upgraded by these experiments. The remote
comparison files are `working-checkout-comparison.json` and
`working-checkout-diffs.json` under the evidence root. Preserve unrelated `anns`
data, existing templates, results and native installation.

## 3. What was completed and why it helped

The instrumented 1280 baseline attributed **91.95% of cumulative agent Tool
completion-callback wall time** to two sequential acquisitions of the shared
admission ledger lock. The accounting update used only microseconds of CPU.
Handlers were already threaded; a shared lock, rather than a single request
worker, serialized progress behind NUMA/cgroup checks.

Implemented repairs:

- `experiments/admission_lock.py`: reentrant admission lock with FIFO normal and
  progress queues. Short completion/model-wait updates get bounded priority;
  after eight progress handoffs a waiting ordinary caller is served. Wake only
  the selected lock waiter. Preserve `Condition` recursive release/restore.
- `policy.py`: use the priority entrance for short release/active/model-wait
  updates. Capacity checks and reservation commits remain atomic.
- `worker.py`: avoid redundant active-state publication and lifecycle locking
  when another request is active/pending; the final owner still rechecks under
  the correct lock order. Index completed prediction records by execution ID.
- `memory.py` / `numa_borrow.py`: retain PID/start-time-validated proc `statm`
  descriptors; remove redundant total-charge work and parse required NUMA LRU
  rows. Preserve cadence, baseline/peak/final RSS, faults and cgroup observations.
- `priority.py`: the C2 poller releases its topology lock between VMs; completion
  gets bounded priority. Revalidate pending timestamps and assignment identity
  before applying a previously sampled entry. Preserve affinity verification,
  group capacity, delay and expansion rules.
- `replay/trace.py`, `replay/model_gateway.py`, `experiments/prediction.py`,
  `prediction_records.py`, `policy_control.py`: retain immutable/completed
  evidence as encoded JSON, subsequently compressed with zlib level 1, rather
  than growing nested container graphs. Decode for actual consumers and final
  exports. Preserve fingerprints, retries, identities, Unicode, unknown values,
  ordering and complete public evidence. Active records still receive final
  measurements. GC remains enabled and its settings were not changed.

Scoped GC callbacks established that full collections caused many remaining
pauses: the clean v3 1280 run had a maximum observed collection of 5.13 seconds.
Encoding reduced the retained container graph, but raw JSON strings increased
RSS. Compression corrected the memory trade-off. This is not a blanket claim
that GC explains all residual waiting.

### Verified performance and correctness

All durations below are seconds. `1280 v5` predates compression; `2560 v6`
includes it. Offered concurrency is not the number of simultaneously active
VM pairs. These are fixed-window diagnostics, not complete throughput trials.

| Completion metric | Baseline 1280 | 1280 v5 | 2560 v6 |
|---|---:|---:|---:|
| Agent completion callbacks | 23,134 | 27,317 | 26,156 |
| Mean callback | 1.636 | 0.059 | 0.073 |
| Callback p90 | 3.869 | 0.135 | 0.162 |
| Callback p99 | 7.730 | 0.489 | 0.804 |
| Callback above 1 second | 54.2% | 0.300% | 0.562% |
| Mean observed Tool end to completion | 1.806 | 0.242 | 0.293 |

At 2560, v5-to-v6 worker peak RSS fell from **13.33 to 5.93 GiB** (55.5%).
Mean callback time was similar, 0.0706 to 0.0732 seconds; p99 became slightly
worse, 0.721 to 0.804 seconds. Report compression as a memory improvement, not
another demonstrated callback-latency win. Session progress differed between
runs, so RSS peaks are not a per-call allocation comparison.

- Targeted Linux suite: **223 tests passed** for v6. Coverage includes lock
  fairness/Condition behavior, lifecycle progress, stale C2 entries, identity-bound
  RSS, admission policies, evidence round trips and replay retry semantics.
- V6: 33,383 completion callbacks across scopes returned successfully; no cgroup
  OOM event. All 1,836 prediction records exported by 25 finalized sessions had
  valid RSS/cgroup observations. This is a finalized-session subset.
- Completed/failed sessions: v5 1280 **35/51**; v5 2560 **22/50**; v6 2560
  **25/31**. Cube capacity errors (including 130597), replay Tool failures and
  ModelGateway incompleteness remain in evidence. Do not claim whole-workload
  correctness or remove failed sessions from denominators.
- All these runs were aborted through the maintained CLI after a saved cutoff
  and cleaned to `populated 0`. No clean-machine installation was tested here.

## 4. Remaining costs: current measurements, not old bottleneck rankings

Source: the latest raw events, reanalyzed after the completion work. Values are
means unless specified. "Subtracting waits" refers to the recorded mixed
admission timer and estimated placement of restore-reservation intervals; it is
not yet an exact decomposition of pure resource waiting versus controller time.

| Remaining path | 1280 v5 | 2560 v6 | Interpretation |
|---|---:|---:|---|
| Per-Tool non-execution after recorded admission/restore waits | 2.808 | 3.425 | 27,313 / 26,154 matched calls; p90 5.498 / 6.564. |
| Memory preparation remainder within that path | 2.400 | 2.855 | Recurring first priority; includes routing/control/restoration work. |
| Tool checkpoint service excluding transfer-slot wait | 3.407 | 4.244 | 343 / 416 operations; may overlap model waiting. |
| Runtime checkpoint service excluding transfer-slot wait | 3.617 | 4.441 | Distinct VM operation; do not sum overlapping pair operations. |
| Tool restore service excluding transfer-slot wait | 1.299 | 1.704 | 315 / 387 operations; separate from reservation waiting. |
| Runtime restore service excluding transfer-slot wait | 1.387 | 2.317 | 328 / 396 operations. |
| Tool checkpoint transfer-slot queue | 0.056 | 0.735 | Scheduling queue, not free-memory waiting. |
| Runtime checkpoint transfer-slot queue | 0.189 | 2.196 | At 2560 p90 8.786 seconds. |
| Tool restore transfer-slot queue | 0.086 | 0.523 | At 2560 p90 2.062 seconds. |
| Pair ready to agent runnable | 4.097 | 6.783 | Startup after VM creation; once per session. |
| Final response to validated completion, minus recorded restore waits | 109.401 | 79.921 | 35 / 25 finished sessions; outside response latency. |
| Task-validation command alone | 0.050 | 0.062 | Not the explanation for the preceding row. |

For context, native Tool VM creation service averages 0.591 / 1.317 seconds;
Runtime creation averages 0.520 / 1.195 seconds. Initial cgroup placement was
already repaired. Do not count the entire gate-to-pair-ready interval as native
creation service: placement/capacity waiting is inside that interval.

The **66 / 108 seconds per task** from the previous answer are *optimistic
residuals*, not exact totals: on 35 / 25 validated sessions, start at actual VM
creation and subtract the union of whole pre-execution admission queues, observed
Tool execution, observed model-wait intervals and restore-reservation intervals.
This excludes all earlier creation/placement queues and removes implementation
overhead inside admission/model waits too. It ends at observed final response,
so the separate 109 / 80-second finalization interval is not included. Do not
multiply per-call means by call counts or add table percentiles to derive E2E.

## 5. Unfinished priority A: identify and reduce admission preparation

### Known mechanism and instrumentation gap

Read these current functions, rather than assuming a previous proposed refactor
was implemented:

- `worker.py:admit_openclaw_tool`, `_prepare_tool_admission`,
  `wait_for_admission_without_lifecycle_lock`.
- `worker.py:WatermarkController.progress_fits_below_low`,
  `route_progress_to_shared`.
- `policy.py:begin_tool_admission`, `acquire`, `local_commitment_exceeds`,
  `_pressure_reasons`.
- `admission.py:DemandLedger.pending_breakdown_without_condition` and
  `admission_lock.py`.

The caller records broad stages including `lifecycle_lock`, `memory_prepare`,
`restore_and_route`, `memory_baseline`, `cpu_placement` and `sampler_start`.
The large remainder is in `memory_prepare`, not the later sampler or CPU setup.

In the already-resident/below-LOW branch, `begin_tool_admission()` first enters
`with self._condition`, marks the session active, and only then calls
`acquire()`. The reported admission timer starts **inside `acquire()`**.
Thus waiting for that outer lock is absent from `admission_blocked_seconds`.
Routing work before that call and reacquisition of the per-session lifecycle
lock after admission also lie outside the returned timer. These are concrete
measurement gaps, **not proof that any one explains all 2.85 seconds**.

Existing `admission.memory_check` spans can accumulate substantial wall time
with little calling-thread CPU, but these checks may repeat and overlap the
recorded admission interval. Do not add them to its residual. Exact pending
sampling already releases all recursive condition acquisitions and shares an
in-flight sampling `Future`; do not reimplement that as a supposedly new fix.

### Next narrow instrumentation patch

Give each phase host-monotonic start/end, thread CPU, session/execution ID and
result/error status. Buffer records until critical sections have exited.

1. Time initial admission-condition acquisition, eviction-in-progress waiting,
   state update and reservation commit separately.
2. Split `_prepare_tool_admission`: fast-path decision, active marking,
   restore capacity wait, native restore service, routing check, borrow/reclaim
   action, reservation call, and lifecycle-lock reacquisition.
3. In the reservation loop record blocking reasons and timing transitions:
   genuine LOCAL/global capacity shortage versus FIFO predecessor, same-session
   ownership, lock acquisition, sampling work, and scheduling delay.
4. Separate condition sleeping from lock reacquisition. `Condition.wait()`
   returns only after reacquiring the lock; its whole wall duration is not pure
   resource waiting. Use the existing lock hooks if needed, with bounded tracing.
5. Join these to `tool_admitted` and Tool start/end events. Inspect repeated
   no-pressure calls as well as pressure periods, and preserve failed attempts.

Avoid logging entire demand ledgers or doing extra filesystem scans per retry.
Do not weaken atomic capacity-check/reservation semantics to get prettier timing.

### Conditional optimizations after attribution

- If repeated lock entrances dominate, remove redundant entrances or redundant
  checks while keeping a single atomic decision/commit. Do not simply put all
  ordinary admissions in the completion-priority queue.
- If filesystem sampling dominates, reuse an in-flight observation or move work
  outside the lock with identity/generation revalidation, extending the existing
  approach. Any cached upper bound must remain conservative; stale usage must
  not permit an unsafe reservation.
- If lifecycle lock reacquisition or routing serializes unrelated operations,
  shorten the critical section and publish/revalidate only the necessary state.
- If shared state really serializes independent NUMA domains, consider narrower
  per-domain work, retaining correct global-budget accounting. This is a larger
  change, not the first assumption.

Success: materially reduce the typical 2.40/2.85-second preparation remainder
and total per-call non-execution cost, without moving it into excluded waiting,
starving admissions, delaying completion, or changing placement/budget policy.

## 6. Unfinished priority B: final response to usable experiment result

The event interval is measured; **the culprit is not yet isolated**. Do not
attribute the entire 109/80 seconds to gzip, Tool artifact transfer, validation,
or GC without phase evidence.

Important source detail found while preparing this handoff:

1. `openclaw_driver.py:run_openclaw` observes the final response, then fetches
   several Runtime logs through separate `runtime_executor.execute()` calls,
   stops helpers, lists trace files, and **fetches each trace file separately
   with another remote `base64 -w0` command**. This occurs before returning to
   the worker and before the Tool artifact collector starts.
2. The worker then marks finalization active, drains queued policy callbacks,
   materializes prediction records, exports policy records, acquires the
   lifecycle lock, restores if needed, and resolves the collection route.
3. `native_artifacts.py:collect_and_validate_native_tool_artifacts` executes a
   gzip/base64 framed Tool export via Runtime-to-Tool SSH. Large outputs use a
   fixed export plus sequential **1 MiB** chunk reads, each through another
   execute/SSH call. Up to eight collection attempts are allowed. Existing
   `tool-artifacts/<session>/validation.json` records attempt count, not a full
   timing decomposition.
4. It decodes/writes artifacts, parses Runtime traces, joins execution IDs,
   validates evidence and enriches predictions. Only then is
   `native_tool_artifacts_collected` emitted. Later completeness/task validation
   is relatively short on the measured finished subset.

Instrument from the producer's `final_response_observed.observed_monotonic_s`,
not merely the later event-writer timestamp. Add spans for:

- Runtime log fetching, trace listing, per-file transfer counts/bytes and
  `run_openclaw` return lag;
- policy-event drain, prediction materialization/export, collection lifecycle
  lock wait, restore reservation versus service, and route resolution;
- Tool export generation, each attempt/chunk transfer, bytes, decode, local
  write, Runtime trace parsing, evidence join/enrichment and final validation.

Then select the measured remedy. Strong **candidates**, not proven causes:

- Batch Runtime files into one bounded, framed stream instead of one remote
  command per file; validate lengths, identities and complete output before use.
- Avoid regenerating/recompressing an export after initial truncation; retain
  the existing fixed-export principle and make transfer efficient within known
  output limits. Prefer an already-supported native transfer capability if it
  preserves the data and fits this workflow.
- Remove duplicate parsing/materialization and reuse immutable decoded data
  only where needed; preserve the memory reductions already achieved.
- Use bounded parallel collection only if transport/I/O waiting is measured
  and the same-session lifecycle/VM ownership remains safe.

Keep full auditable evidence and exact-ID joins. Do not truncate logs, skip
required measurements/validation, accept partial exports, or destroy VMs while
collection still uses them. Avoid a broad new artifact service/framework.

Success: substantially shorten the response-to-validated-result interval and
earlier resource release, with identical required evidence and validity checks.
Report this separately from user-visible response latency.

## 7. Secondary work; do not restart solved investigations

- **Checkpoint/restore orchestration:** distinguish reservation wait, transfer
  slot queue and actual service. At 2560 Runtime checkpoint slot waiting is
  material. Inspect pair sequencing, redundant native calls/metadata work and
  burst dispatch before changing concurrency. Raising slot counts alone may
  increase native/storage contention. Many checkpoints overlap LLM time, so
  measure their exposed critical-path delay rather than summing service totals.
- **Startup:** Runtime setup means 1.11/1.93 s and sidecar readiness 2.56/4.17 s
  contribute to the 4.10/6.78 s after pair-ready. They may overlap. SSH warmup
  executor queueing is tiny in these runs; do not blindly add workers there.
- **Pressure control:** C2 topology polling was refactored, but
  `PressureControl.poll()` still holds its own global lock over a batch. Its
  callback timing was added, not a complete pressure-lock redesign. Some v5
  long waits overlap poll holds without substantial recorded GC. Given accepted
  rare tails, revisit only if representative mean/tail impact justifies it.
- **VM creation/native kernel:** preserve the cgroup-birth fix. Do not pursue
  the older cpuset-stall diagnosis or prohibited kernel/IRQ redesign.

## 8. Evidence identities and reproduction

Remote root: `/data/clawbox-control-overhead-20261027`.

| Phase directory | Run ID | Attempt |
|---|---|---|
| `diagnostic-r2` | `full-c1280-control-diagnostic-r2-20261027T105841z` | `8c41614ceac541bc` |
| `candidate-v5-1280` | `full-c1280-control-candidate-v5-1280-20261027T122844z` | `5cd4a24703de422f` |
| `candidate-v5-2560` | `full-c2560-control-candidate-v5-2560-20261027T124615z` | `93597d80a31745b0` |
| `candidate-v6-2560` | `full-c2560-control-candidate-v6-2560-20261027T130352z` | `33c99421da23470a` |

Actual attempt directory names have the `attempt-` prefix. Arm IDs:
1280 `9f3ca15bb4a02f59536788fd`; 2560 `37a234b831f6ed30894ae0ca`.
Within a phase, raw events are:

```text
runs/<run-id>/attempts/attempt-<attempt>/<arm-id>/events/<arm-id>.jsonl
```

Each phase preserves frozen `source/`, `manifest.json` source hashes,
`experiment.yaml`, `qualification.json`, `run-id.txt`, launch logs, run-state,
raw events, policy/model records and available Runtime/Tool artifacts. Key
analysis files: `pre-abort-state.json`, `stop-rule.json`, `analysis-final.json`,
`gc-analysis.json`, `measurement-audit.json`, `residual-attribution.json`,
`cleanup-cgroup.txt`; v6 also has `verification-summary.json`.

Do not use `candidate-v2-2560` as an uncontaminated acceptance run: a 90-second
kernel-uprobe investigation disturbed that window. Its partitioned analysis is
exploratory only. Initial `diagnostic` used the wrong Python environment and is
also invalid; preserve both, but exclude them from performance claims.

Local analysis artifacts (ignored, present in this workspace):

| Script | Output | Purpose |
|---|---|---|
| `.runtime/current_e2e_review.py` | `.runtime/current-e2e-review.json` | Broad per-operation/startup review of latest runs. Imports the older read-only reviewer on the remote host. |
| `.runtime/e2e_excluding_queues.py` | `.runtime/e2e-excluding-queues.json` | Per-call wait subtraction and optimistic session interval-union residuals, with explicit limitations. |
| `.runtime/current_overhead_rank.py` | `.runtime/current-overhead-rank.json` | Lifecycle queues/service and post-response interval decomposition. |

Additional local performance summaries are under
`.runtime/control-overhead-20261027/`. The scripts above run on `kunpeng` against
the saved logs; they print JSON and do not launch VMs or modify experiment data.
Example from PowerShell:

```powershell
Get-Content .runtime/current_overhead_rank.py -Raw | ssh kunpeng python3 - > .runtime/current-overhead-rank-refresh.json
```

The imported older reviewer is at
`/data/clawbox-create-placement-20261027/clawbox-overhead-review-20261027/review.py`.
On a fresh clone without `.runtime/`, reconstruct the small analysis from the
definitions here and raw remote evidence, or retrieve these local scripts from
this workspace. Do not assume ignored files travel with the handoff commit.
PowerShell redirection may write UTF-16; use `Get-Content -Raw` or detect encoding.

Measurement definitions and pitfalls:

- Per-call non-execution: `(admitted - queued) + (Tool start - admitted) +
  (completed - Tool end)`, then subtract that call's recorded admission wait
  and overlapping restore-reservation intervals. All event times are host
  monotonic; guest execution is bounded by host-observed progress callbacks.
- Restore wait location is approximated as the duration immediately before
  `lifecycle_timing.started_monotonic_s`; timing between reservation and native
  lifecycle entry is not separately available. This is not exact attribution.
- Filter every raw event by the saved **pre-abort cutoff**, not analyzer run
  time. Aborting/cleanup generates extra events that must not enter the window.
- Compute per-observation differences before quantiles. Union overlapping
  intervals for E2E. Never subtract aggregate percentiles or sum nested spans.
- `control_timing.py` stores monotonic ns and thread CPU ns; `gc_intervals` can
  overlap many callbacks. `.hold` can contain `Condition.wait()` that releases
  the actual lock. Wall time is not exclusive ownership or CPU time.
- Preserve separate response and validated-result endpoints. Keep failures and
  censored operations visible. Finished-session estimates are selected subsets,
  not whole-run throughput, success rates, or an exact overhead percentage.

## 9. Efficient continuation and validation plan

1. Read this file, `AGENTS.md`, and the named source functions. Recheck host
   cleanup/ownership and existing checkout changes. Do not launch immediately.
2. Add the targeted admission and finalization spans together. Keep public
   timing fields documented in `docs/guide.md`, with units/join keys and no
   guest rebuild requirement for host-only changes.
3. Use existing evidence first where possible, especially Runtime trace file
   counts, artifact collection attempt counts, bytes, and saved phase durations.
   Then perform one controlled diagnostic that answers the missing attribution
   questions; avoid a separate large run for each tiny probe.
4. Make the smallest demonstrated repair. Test concurrency/state invariants for
   admission changes and exact evidence round trips for collection changes.
5. Validate consequential improvements at 1280 and 2560 as appropriate, keeping
   budgets, templates, native Cube versions, workload/arrival seed and durations
   fixed. Reuse current runs as comparison evidence where definitions suffice.
   A new matched baseline is warranted only for measurements they do not contain.
6. Preserve a unique run ID and all artifacts per attempt. Report means,
   p90/p99, counts, service versus memory waiting, failure/incomplete counts,
   actual workload progress and worker RSS. Stop after a meaningful verified
   improvement; do not chase accepted rare tails. Commit/push verified milestones.

Existing remote helpers (`prepare.py`, `validate.py`, `watch.py`, `manage.py`,
`analyze.py`, `audit.py`) wrap the maintained CLI or analyze its output. They are
diagnostic conveniences, not a second supported experiment path. Read before
reuse: versions/archives and output names are hardcoded, and some analyzers
overwrite their derived output. Use fresh destinations for new evidence.

Important source/environment traps:

- `source.tar` in the diagnostic root is an archive of **`1b6d55c`**. Historical
  candidates overlay all changes from that base. After committing, plain
  `git diff --name-only` would omit already-committed repairs. Prefer a complete
  fresh archive of the intended source, or explicitly diff against `1b6d55c`.
  Freeze and hash the entire effective source before each run; never edit a
  launched run's source tree or overwrite an old candidate archive.
- Saved host environment:
  `/data/clawbox-create-diagnosis-20261027T1508CST/machine.env`.
  `CLAWBOX_PYTHON` is `/home/weitianc/ClawBox/.venv/bin/python` (Python 3.13).
  Prepend the candidate source to the saved `PYTHONPATH`; do not replace the
  native SDK path or accidentally run under the unrelated default interpreter.
- Frozen configurations/qualification came from
  `/data/clawbox-create-placement-20261027/c1280-r2` and `c2560`.
  Diagnostic arrival window: 741.1812062263489 seconds, seed 20261025. The watcher
  cutoff is approximately 935 seconds after run-state creation, sampled every
  ten seconds, then saves pre-abort evidence and uses `clawbox experiment abort`.
  Such a window is a diagnostic, not a guarantee all offered sessions finish.
- The recorded 223-test selection is in remote `validate.py`; its log is
  `validation-candidate-v6-tests.log`. Do not rerun all tests merely to reproduce
  this count. Use tests relevant to the next actual change. Opt-in kernel/cgroup
  tests must not run against an active shared experiment parent.

**No unfinished code patch or experiment is running on behalf of this handoff.**
The admission/collection instrumentation and optimizations in sections 5-6 are
proposed next work, not already implemented. Completion repairs and evidence
compression are the verified delivered milestones.
