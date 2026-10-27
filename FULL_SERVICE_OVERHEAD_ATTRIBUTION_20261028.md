# Full-1280 overhead attribution

Measured 2026-10-28, Asia/Shanghai. This is an internal evidence report.

The new diagnostic identifies two dominant implementation costs: shared
admission-lock waiting and finalization waiting for queued global pressure
callbacks. Runtime file transfer and Tool artifact export are much smaller in
this run. Checkpoint service also includes a substantial delay after native
checkpoint completion, consistent with the installed SDK's polling path.
These are attribution results, not a measured optimization improvement.

## Run and verification

- Run: `full-c1280-attribution-20261027T164034z`.
- Attempt: `attempt-d3fa8ac49adb4f5d`; arm: `9f3ca15bb4a02f59536788fd`.
- Remote evidence: `/data/clawbox-overhead-attribution-20261028/diagnostic-c1280`.
- Source: complete archive of `f3aa3e3` plus the timing-only changes recorded in
  `manifest.json`. Rechecking every frozen source hash found no mismatch.
- The configuration and qualification receipt were copied from the established
  `c1280-r2` baseline. Arrival seed/window, budgets, native installation, templates,
  prediction and snapshot policies were retained. No image rebuild or retraining.
- 1280 offered sessions; 503 reached agent-runnable. These are cumulative counts,
  not simultaneous active VM pairs. At the saved cutoff: 30 validated completions,
  71 session failures, and 1179 sessions without either terminal event.
- The planned 935-second window was captured at 942.761 seconds by the periodic
  watcher. Analysis excludes events after the saved pre-abort cutoff. The first
  watcher exited before run-state creation; its error log is retained. Watcher v2
  resumed against the original deadline, without restarting the experiment.
- Failure evidence includes 50 Cube capacity errors (130597), replay Tool
  failures, ModelGateway incompleteness and one Runtime command failure. A policy
  callback also recorded HTTP 408. These are not removed from the evidence.
- 215 focused Linux tests passed, 4 skipped. Real VM evidence is this diagnostic;
  clean-machine installation was not tested. All 50 collected Tool artifact sets
  validated, with one collection attempt each and zero reported telemetry loss.
  Artifact validity does not imply that all 50 sessions passed task/replay checks.
- Worker high-water RSS: 6.80 GiB. No cgroup OOM event. CLI abort completed and
  cleanup recorded `populated 0`, `frozen 0`; terminal state is `cancelled`.

## Admission: lock waiting, including the below-LOW path

The 28,402 completed Agent Tool admission callbacks had mean preparation time
5.691 seconds (p50 2.764, p90 13.634, p99 44.642). A disjoint interval partition
attributes 5.528 seconds per callback, **97.14%**, to lock acquisition or
reacquisition. This percentage is not obtained by summing nested spans.

| Preparation branch | Calls | Mean preparation, s | Mean lock waiting, s | Preparation p90, s |
| --- | ---: | ---: | ---: | ---: |
| Resident, below LOW | 17,507 | 1.582 | 1.574 | 3.293 |
| Resident, routing path | 10,798 | 12.097 | 11.704 | 27.628 |
| Restore needed | 97 | Included in overall partition | Included in overall partition | See raw metrics |

In the below-LOW branch, the initial condition acquisition in
`PolicyCoordinator.begin_tool_admission()` alone averages 1.5735 seconds.
These calls did not record capacity-condition sleeps. This directly establishes
implementation contention even when the fast path says the Tool is resident
and below LOW. The old `admission_blocked_seconds` timer starts after this lock
acquisition and therefore omitted this delay.

Under pressure, repeat acquisition and reacquisition amplify the delay. For
4,805 Agent admission callbacks with condition reacquisition spans, their
within-callback reacquisition duration averages 12.864 seconds. This is a
conditional subset and can contain several reacquisitions, not the mean of a
single lock operation. The existing shared sampling Future does work: its
waiting stage averages only 0.0417 seconds on 3,600 affected callbacks, whereas
the enclosing exact-pending stage averages 7.433 seconds. Do not interpret the
whole enclosing interval as filesystem sampling.

The implementation holds the shared condition over physical memory checks and
atomic reservation decisions. Normal Tool admission, routing checks, queue-head
checks, and global pressure checks compete for it. NUMA/cgroup observations take
milliseconds individually but serialize callers; exact sampling also releases
the condition and then rejoins this queue. The low-pressure branch provides the
cleanest evidence that free-memory waiting does not explain the delay.

Across all measured preparations, exclusive capacity-reason sleep totals 188.9
seconds, versus 157,005.8 seconds of cumulative lock waiting. FIFO-predecessor
sleep is kept separate (2849.8 seconds): the predecessor's resource dependency
is not fully reconstructed. Condition sleep is classified from the blocker
observed before sleeping and can include scheduling latency; it is not an exact
measurement of unavoidable resource waiting. Pending admissions are censored.

## Finalization: draining pressure callbacks dominates

On the **same 30 validated sessions**:

| Interval or stage | Mean seconds |
| --- | ---: |
| Final response observed to validated completion | 146.648 |
| `drain_policy_events()` | 138.804 |
| Runtime trace transfers | 1.579 |
| Runtime log transfers | 0.195 |
| Tool collection, including decoding/parsing/validation | 3.039 |
| Restore reservation call, including its controller work | 1.850 |

Policy draining accounts for **94.65%** of response-to-result time on this
cohort. The raw endpoint includes restore waiting; the reservation row is not
classified as pure memory waiting. The other rows are diagnostic stages, not
a complete additive endpoint decomposition.

For all 50 successfully collected artifact sets, policy draining averages
146.862 seconds; Runtime trace transfer averages 1.657 seconds and Tool collection
3.300 seconds. All used one export attempt and none used the large-export/chunk
fallback. Sequential transfer remains optimizable, but it does not explain the
minute-scale finalization delay here. The remaining observed final responses
without completed collection are censored rather than assigned zero duration.

The source mechanism is specific:

1. Every model-wait notification submits `pause_for_model_wait` to a shared
   per-arm `ThreadPoolExecutor` with 24 workers in this configuration.
2. Each session retains its submitted `Future` objects in
   `pending_policy_events`. Finalization iterates them and waits for each result.
3. The callback does not use the originating request identity to determine
   useful work. It calls the coordinator's current `blocked_demand_pressure()`
   and possibly global `relieve_pressure()`, including after that originating
   model request has finished. Both contend with admissions on the shared lock.
4. Finalization therefore waits for global pressure work submitted earlier by
   the session, even when that work is checking pressure or selecting another VM.

Two nonblocking stack snapshots support this connection. Near 843 seconds,
18 of 24 policy workers were waiting on the admission lock in
`blocked_demand_pressure`, five were waiting on that lock in victim selection,
and one was inside HTTP work. Fourteen session workers were waiting in
`drain_policy_futures`. The earlier snapshot also found policy workers waiting
to reacquire the condition after exact sampling. The snapshots took about
0.88 and 1.13 seconds without stopping the process; raw snapshots are retained.

The total drain time is measured. Exact enqueue-to-start versus execution time
for each policy callback was not separately recorded; stack snapshots establish
where workers were blocked at those instants, not their whole-run occupancy.

## Checkpoint/restore: native service, return lag, and placement

| Operation | Count | Mean service, s | Mean transfer-slot queue, s |
| --- | ---: | ---: | ---: |
| Tool checkpoint | 251 | 3.285 | 0.085 |
| Runtime checkpoint | 250 | 3.228 | 0.151 |
| Tool restore | 234 | 1.252 | 0.009 |
| Runtime restore | 240 | 1.749 | 0.044 |

Service includes the transfer-slot queue. Reservation waiting precedes it and
must be analyzed separately. One additional `sandbox_paused` event represented
a spill and is not counted as a checkpoint in this table.

Native phase records were joined by exact VM identity and matching operation
intervals, covering all 501 checkpoint operations:

- Native Cubelet total averages 1.931 seconds for both roles. Memory send/write
  accounts for 1.112 seconds for Tool and 1.362 seconds for Runtime; runtime
  destruction contributes another 0.551 / 0.334 seconds. Metadata and previous
  snapshot cleanup are much smaller. Parent phases contain these child phases.
- After Cubelet completion, the SDK pause call takes another **1.154 / 1.048
  seconds** to return (Tool / Runtime). The installed `Sandbox.pause()` uses
  `get_info()` polling with a default **1.0-second interval**. Polling is a
  concrete candidate for this return lag; transport, state publication and
  scheduling also contribute, so the entire gap is not proven polling sleep.
- Restore native API time averages 0.828 / 1.066 seconds. ClawBox cgroup
  verification contributes 0.267 / 0.344 seconds; cgroup preparation contributes
  0.044 / 0.179 seconds. Placement verification is a measurable secondary target,
  while final NUMA pinning itself is about 0.025 / 0.022 seconds.
- Most checkpoint wall time overlaps observed model waiting. Mean checkpoint
  time outside those intervals is 0.082 / 0.087 seconds. This overlap is not an
  exact critical-path proof, but it prevents treating the whole three-second
  checkpoint service as additive response overhead. Restore generally occurs
  after the model-response-ready marker or before the next Tool starts.

Snapshot materialization is required by the configured policy. Neither its
entire duration nor the measured polling/verification gaps should be called
unavoidable algorithmic cost. Transfer-slot queues are secondary at 1280;
increasing slot counts is not the demonstrated next remedy.

## Next repair order

1. Coalesce redundant global pressure-check notifications per coordinator and
   eliminate finalization's dependency on obsolete/unrelated queued checks.
   Preserve actual in-flight VM ownership, pressure progress, and propagation
   of every policy failure. `set_tool_active()` already waits for a claimed
   eviction to finish; prove the relevant lifecycle invariants before changing
   the drain. Do not simply ignore all futures or swallow their exceptions.
2. Reduce repeated shared-lock entrances/checks and physical-observation
   serialization. Retain the existing in-flight exact sampler. Any observation
   reuse must revalidate identity/generation and conservatively account for
   reservations; never admit against an unsafe stale capacity value. Do not
   route all admissions through completion priority.
3. Measure the benefit and extra request load of improving the supported pause
   completion path, then shorten repeated cgroup verification where an existing
   identity-bound witness suffices. Preserve snapshot and placement verification.
4. Batch Runtime collection only if worthwhile after the dominant callback
   backlog is removed. Retain every required artifact and exact-ID validation.

The next consequential candidate should combine justified changes and be
validated at 1280, then 2560 under the frozen comparison rules. Do not run another
large diagnostic solely for individual missing probes or accepted rare tails.

## Evidence and reproduction

The remote root contains the complete source archive, manifest, configuration,
qualification, launch log, run-state, raw events, ownership ledger, available
Runtime/Tool artifacts, `pre-abort-state.json`, `stop-rule.json`,
`host-samples.jsonl`, `analysis-final.json`, `checkpoint-sdk-gap.json`,
`verification-summary.json`, `evidence-audit.json`, `sdk-pause-connect-source.txt`,
stack snapshots, and cleanup evidence. No earlier run ID or evidence was reused.

Read-only analyzers are preserved in the parent remote directory as
`analyze.py`, `analyze-v2.py`, and `native_analysis.py`. The final native capture
is `native-1793120212635190393/`; it includes filtered checkpoint phase records,
VMM logs, capture offsets, and `summary.json`. Native checkpoint capture covers
the whole run; native restore log excerpts use process-relative time and are
supporting evidence rather than an independently cutoff-matched endpoint metric.

To reproduce the event summary into a fresh local destination:

```powershell
ssh kunpeng python3 /data/clawbox-overhead-attribution-20261028/analyze-v2.py /data/clawbox-overhead-attribution-20261028/diagnostic-c1280 > attribution-refresh.json
```

Local convenience copies are in `.runtime/overhead-attribution-20261028/`.
Timing records add diagnostic work, and this run uses the compressed-evidence
baseline while the earlier 1280 v5 run did not. Absolute differences from those
older runs are not a controlled before/after optimization result. All reported
phase conclusions use this run's own timestamps and explicit cohorts.
