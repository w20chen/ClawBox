# Full service-overhead diagnosis handoff

Prepared 2026-10-27 Asia/Shanghai for an independent Codex session. This is the
diagnosis and execution plan. The completion-control update below is current;
the earlier sections remain historical evidence. This file supersedes the ordering in
`FULL_SERVICE_OVERHEAD_HANDOFF_20261026.md`; that file and
`FULL_LATENCY_HANDOFF_20261025.md` remain useful for the earlier evidence and
historical fixes.

## Update: completion-control diagnosis and bounded-priority repair

Fine-grained host-monotonic spans separate callback wall time, calling-thread CPU,
lock waiting/holding, sampler join/final counters, and resource release. The
instrumented 1280 baseline attributes **91.95% of cumulative agent completion
callback wall time to admission-ledger lock acquisition**, not pressure polling.
This sum counts the two sequential ledger waits; nested parent/child spans are
not added. Stack snapshots locate costly NUMA/cgroup reads in the lock holder.
The accounting updates themselves take microseconds. HTTP handlers are threaded;
all NUMA coordinators share one accounting lock, so adding workers would increase
its contenders rather than remove the serialization.

Implemented changes preserve atomic memory-check/reservation commits:

- A reentrant admission lock gives short completion and model-wait updates priority
  over queued memory checks. At most eight priority handoffs precede a waiting
  ordinary caller. Only the selected lock waiter is awakened at each handoff;
  existing policy condition notifications remain intact.
- A completion with other active/pending requests skips the redundant active-state
  publication and lifecycle-lock acquisition. The final owner still rechecks and
  publishes idle under the original lock ordering.
- NUMA sampling removes a redundant total-charge read and parses only required
  LRU rows. RSS sampling retains identity-checked proc `statm` descriptors instead
  of reopening/parsing command lines and status files every 50 ms. The resident
  pages represent the same kernel RSS counter; cadence, baseline/peak/final,
  cgroup-charge and fault-counter semantics remain intact.
- Timing records include failed callbacks. Python GC intervals are collected by
  scoped runtime callbacks without I/O/locks inside GC, then correlated with the
  operation timestamps. No kernel, Cube binary, template, budget or prediction
  change is part of this repair.

1280 baseline versus first candidate (seconds, completed agent Tool callbacks):

| Metric | Instrumented baseline | First candidate |
|---|---:|---:|
| Callback count | 23,134 | 24,878 |
| p50 | 1.177 | 0.024 |
| p90 | 3.869 | 0.123 |
| p99 | 7.730 | 0.620 |
| Maximum | 38.370 | 7.547 |
| Above 1 second | 12,541 (54.2%) | 133 (0.535%) |
| Observed Tool end to completion event p90 | 4.096 | 0.357 |
| Observed Tool end to completion event p99 | 7.911 | 1.511 |

This establishes a large reduction, **not a hard one-second upper bound**.
The first candidate's remaining lifecycle-lock tails correlated with model-wait
state updates (29/29 sampled long waits ended with that same session's
`model_wait_started`). The subsequent candidate applies priority to those short
updates too and retains proc descriptors. Final verification is recorded below.

A clean follow-up, `candidate-v3-1280`, adds scoped Python GC callbacks. Early
in that run, all 18 callbacks above one second overlapped a full collection that
accounted for most of their wall time. Later full collections grew beyond four
seconds; admission and pressure locks amplify a pause when the interrupted
thread owns one. This explains why a kernel-counter span can take seconds of
wall time while its own thread consumes only milliseconds of CPU. The kernel
read itself was not established as slow by that span alone.

The next candidate (`candidate-v4`) addresses retained data, not GC thresholds:
cached replay input histories use JSON text with on-demand decoding; model
requests retain the canonical JSON already computed for their fingerprint.
Retry comparison decodes only the required undelivered request. Completeness
checks avoid materializing payloads they do not use, and ledger flush inserts
already-encoded payloads without decoding/re-encoding every conversation.
Exported request payloads, request fingerprints, response identities, recorded
model delays and process-session rebinding retain their semantics. Automatic GC
remains enabled. The targeted Linux suite passes 199 tests, including admission,
sampling, replay retries, cross-session identity and Unicode ledger evidence.
That change alone is insufficient: the final v4 1280 sample still has 120/28,368
callbacks above one second (p90 0.152 s, p99 0.757 s, max 4.567 s).

The next candidate (`candidate-v5`) additionally stores immutable prediction
cache values and completed per-execution prediction evidence as JSON text.
Active records remain available for final memory measurements; completion uses
an execution-ID index instead of scanning every earlier prediction. Final
exports materialize the original dictionaries in admission order. Policy-control
requests likewise retain encoded payloads plus a separate command identity for
fast duplicate/progress checks. A finalized example contained 5,238 containers
in just 43 prediction records, confirming that histories were not the only
large retained graph. No observation or provenance field is dropped.

Three v4 callbacks also spent 1.22-1.25 s in CPU-placement release without a
material GC overlap. The C2 poller held its lock across all pending assignments
and active-VM pressure checks. The final candidate hands off the lock after each
VM, gives completion bounded priority, and rechecks pending timestamps and active
assignment identity before applying a saved entry. Affinity verification, group
capacity, minimum duration and expansion rules are unchanged. A concurrent test
proves that a completed pending call cannot be assigned from a stale snapshot.
The combined targeted Linux suite passes 222 tests.

Completed v5 windows (seconds; 935-second bounded diagnostic, not complete
workload success):

| Metric | Baseline 1280 | v5 1280 | v5 2560 |
|---|---:|---:|---:|
| Completed agent Tool callbacks | 23,134 | 27,317 | 27,561 |
| Callback p50 | 1.177 | 0.025 | 0.025 |
| Callback p90 | 3.869 | 0.135 | 0.162 |
| Callback p99 | 7.730 | 0.489 | 0.721 |
| Callback maximum | 38.370 | 2.590 | 3.861 |
| Callback above 1 second | 54.2% | 0.300% | 0.493% |
| Observed Tool-end to completion p99 | 7.911 | 1.515 | 1.979 |
| Observed Tool-end to completion maximum | 38.483 | 8.379 | 9.669 |

The admission ledger was the dominant defect, and the bulk callback cost is
substantially lower. **The strict one-second ceiling is still not met.**
The user subsequently accepted rare tails on 2026-10-27 and asked to assess
overall performance instead. Do not pursue more repairs or reruns solely to
eliminate those rare outliers.
The larger offered-load run is a scaling check; there is no clean matching
2560 pre-repair baseline in this diagnostic series.

V5 CPU-placement lock waiting has no observations above one second: maximum
0.131 s at 1280 and 0.855 s at 2560. Full GC still reaches 1.831 s and 1.830 s,
respectively. Direct GC overlap accounts for most of 54/82 and 61/136 slow
callbacks; that does not explain every remaining tail. Pressure completion can
still exceed one second. The broader Tool-end interval also includes work before
the callback and must not be represented by callback latency alone.

Offline `residual-attribution.json` joins those existing host-monotonic events:
Tool-end event to callback entry has p99 1.137 s at 1280 and 1.446 s at 2560
(maximum 8.353/9.625 s). This interval includes wrapper processing, SSH reaping,
HTTP transport and server dispatch; current evidence does not assign it to one
of those mechanisms. At 2560, 9/25 pressure-state lock waits above one second
overlap pressure poll lock holds for most of the wait. Two poll holds last
1.107/1.291 s with only 0.022/0.046 s of recorded GC overlap. Thus GC explains
many tails but cannot justify treating all residual delay as GC.

V5 run IDs are `full-c1280-control-candidate-v5-1280-20261027T122844z`
(attempt `5cd4a24703de422f`) and
`full-c2560-control-candidate-v5-2560-20261027T124615z`
(attempt `93597d80a31745b0`). Both were aborted after the saved measurement
cutoff and cleaned to parent `populated 0`. All 35,245/35,107 completion
callbacks across scopes returned successfully. The 2,664/1,626 exported
prediction records from finalized sessions had valid RSS/cgroup measurements;
this is a finalized-session subset. Neither window recorded a cgroup OOM.
Session completion/failure counts were 35/51 and 22/50, so these are not
successful full-workload runs. Code through v5 is committed as `15d2de9`.

Retaining JSON text avoids GC container traversal but increased worker memory:
peak RSS was 9.87 GiB in v3, 14.54 GiB in v5 at 1280, and 13.33 GiB in v5 at
2560. Progress and session mix differ, so this is not a per-call allocation
comparison. V6 compresses those immutable JSON payloads with standard-library
zlib level 1, preserving exact decoded evidence and public ledger fields. It
also releases the temporary canonical JSON before waiting for a model result.
The 223-test targeted Linux suite passes, including Unicode/retry behavior and
large evidence round trips. Its 2560 comparison uses the same frozen workload
and measurement window.

The v6 2560 window is `full-c2560-control-candidate-v6-2560-20261027T130352z`
(attempt `33c99421da23470a`). It records 26,156 agent completion callbacks:
mean 0.0732 s, median 0.0283 s, p90 0.1617 s, p99 0.8038 s, maximum 4.8128 s;
147 (0.562%) exceed one second. The broader observed Tool-end to completion
interval has mean 0.2931 s, p90 0.4792 s, p99 1.6153 s, maximum 6.2356 s.
This preserves similar typical latency to v5; callback p99 is slightly worse
than v5's 0.7209 s, so compression is not a demonstrated callback-latency win.
There are 643 ready pairs, 25 completed sessions and 31 failed sessions in the
window. These counts do not establish complete-workload throughput or reliability.

For overall interpretation, the matched 1280 baseline-to-v5 mean callback cost
falls from 1.6361 s to 0.0593 s (96.4% lower). The broader observed Tool-end to
completion mean falls from 1.8056 s to 0.2419 s (86.6% lower). At 2560, v5/v6
callback means remain 0.0706/0.0732 s and broader means 0.3132/0.2931 s.
These are control-path improvements, not whole-experiment speedup factors.
Per the user's acceptance of rare tails, stop tail-only optimization here.

V6 peak worker RSS is 6,214,232 KiB (5.93 GiB), versus v5's 13,977,272 KiB
(13.33 GiB) at 2560: 55.5% lower observed peak. Session progress differs, so
this remains a full-window comparison rather than a per-call allocation estimate.
All 33,383 completion callbacks across scopes return successfully; all 1,836
prediction records exported by the 25 finalized sessions have valid RSS/cgroup
measurements. Cgroup OOM counters remain unchanged at zero. The maintained CLI
aborted the run after its saved cutoff and cleanup confirmed `populated 0`.
`verification-summary.json`, `measurement-audit.json`, `gc-analysis.json` and
`residual-attribution.json` retain the final analyses next to raw evidence.

Validation used frozen source checkouts on the existing kunpeng host. The 146
Python source files match the local candidate, allowing Windows line endings.
The separate `/home/weitianc/ClawBox` checkout has pre-existing changes and was
preserved; these results do not imply it was upgraded. No clean-machine install,
kernel change or full successful workload is claimed by this validation.

Evidence root on kunpeng: `/data/clawbox-control-overhead-20261027/`.
`diagnostic-r2` is the baseline; `candidate-1280` is the first candidate;
`candidate-v2-2560` tests the subsequent changes. Each has its own source manifest,
frozen configuration/qualification, run ID, raw event stream, ledger, launch logs,
pre-abort cutoff and analysis. The initial `diagnostic` setup used the wrong Python
SDK environment and is invalid; it is retained and excluded from comparisons.
All runs use the maintained launch/abort CLI, fixed arrival window/seed and
unchanged native components. These are bounded diagnostics: completed operations
are observed, pending operations censored. Session/Cube failures remain in the
raw evidence and counts; none of these runs proves complete workload success.

The v2 2560 run contains a 90-second kernel-uprobe investigation. Long stalls
coincide with that intervention; its unfiltered result remains saved, and it must
not be presented as an uncontaminated performance acceptance run. The trace
recorded 268 GC collections, maximum 12.57 ms, and removed its scoped trace
instance/events. It does not establish the cause of pauses outside that window.
`partition-analysis.json` is exploratory only; excluding the trace interval
cannot undo its effects on workload progress. Prefer the clean verification run.

## Update: VM placement repaired, 1280/2560 diagnostics completed

See [VM creation follow-up](FULL_VM_CREATION_HANDOFF_20261027.md) for the
`b5943df` cgroup-birth repair, completed creation measurements, remaining failures,
and verified cleanup. Initial placement stayed below one second in every matched
creation at both offered loads. Total creation still exceeds one second; measured
IRQ-bypass registration during device restoration remains measured, but its
kernel/VMM redesign is outside the user's revised scope below.

## Scope review: ClawBox overhead, no pure-Cube baseline or kernel change

The user explicitly excludes building a faster sandbox, a pure CubeSandbox
baseline, kernel upgrades/reboots and interrupt-delivery redesign. Focus on
overhead introduced by ClawBox ideas and their implementation, using existing
ClawBox runs as the comparison. Preserve NUMA placement, prediction/admission,
measurement validity, configured snapshot mechanisms and immutable run evidence.

Fresh analysis reads the cgroup-birth 1280/2560 runs through their saved pre-abort
cutoffs. It does not reuse the older v6 distributions as current results. These
are cancelled diagnostics with completed-operation bias, not successful full
experiments. Host-observed Tool progress callbacks bound execution; they are not
calibrated guest CPU timestamps. All durations below are seconds, p50 / p90.

| Path | 1280 | 2560 | Current interpretation |
|---|---:|---:|---|
| Agent Tool non-execution, subtracting that execution's recorded admission wait | 2.980 / 8.818 | 2.524 / 9.406 | Repeated overhead; 22,169 / 18,908 matched calls; includes restore/control/transport |
| Observed Tool end to completion event | 1.440 / 4.078 | 1.114 / 4.153 | First priority: completion/control path, not guest work |
| Tool admission-blocked interval | 0.0068 / 23.777 | 0.0075 / 34.118 | Mixed capacity/controller waiting; do not call all of it avoidable overhead |
| Tool checkpoint total | 6.839 / 17.491 | 5.463 / 14.246 | 781 / 634 completed Tool checkpoints |
| Tool checkpoint transfer queue | 1.840 / 13.276 | 0.248 / 7.233 | ClawBox lifecycle scheduling cost |
| Tool checkpoint service excluding its own transfer wait | 3.723 / 5.725 | 4.271 / 6.693 | Custom tiered snapshot path plus native service; attribution incomplete |
| Tool restore total | 5.200 / 16.040 | 3.411 / 8.507 | 668 / 511 completed Tool restores |
| Tool restore transfer queue | 1.843 / 14.245 | <0.001 / 4.113 | Avoid serializing unrelated lifecycle work unnecessarily |
| Tool restore service excluding its own transfer wait | 1.716 / 4.162 | 2.466 / 4.976 | Includes native restoration; not all attributable to ClawBox |
| Runtime setup | 1.294 / 2.202 | 2.709 / 7.548 | Per-session ClawTune/Runtime initialization |
| Sidecar readiness | 2.850 / 4.512 | 5.920 / 15.675 | Initialization plus health-command transport/polling |
| Pair ready to agent runnable | 5.474 / 9.141 | 10.522 / 25.511 | Startup total; nested/overlapping with the preceding rows |
| Live NUMA borrow total | 0.680 / 1.679 | 0.807 / 1.825 | 91 / 68 observations; old minute-long cpuset stalls absent |
| Return to LOCAL total | 1.231 / 1.882 | 1.159 / 2.168 | 28 / 26 observations; migration remains material |
| Final response observed to validated session complete | 12.077 / 43.824 | 34.765 / 78.010 | Only 17 / 10 completed sessions; collection/validation, not response latency |

The latest borrow cpuset maximum is 0.649 / 0.456 seconds; return cpuset maximum
is 0.570 / 0.458 seconds. The older 68-91-second cpuset diagnosis must not drive
the next patch. Pre-migration now accounts for material borrow/return tails,
but copied bytes are unavailable and residency differences are not copy volume.
77 / 124 borrow rejections say another migration is in progress; this is
serialization, not evidence of shared-memory exhaustion.

### Earlier proposed milestone (superseded by measured attribution above)

Source confirms these ClawBox mechanisms:

- `worker.py:complete_openclaw_tool` calls sampler stop/join and final sampling,
  lifecycle bookkeeping, reservation release and pressure feedback under the
  per-session `reservation_lock`; it later waits for `wait_lock` to update state.
- `memory.py:SandboxRSSSampler` creates one periodic thread per execution.
- `pressure.py:PressureControl.poll` performs cgroup reads/writes while holding
  its global lock, also acquired by execution start/completion notifications.
- `worker.py:session_worker_count` gives ordinary runs one worker per offered
  session; workers sleep until arrival and then wait for the creation gate.

Saved policy-control records independently establish slow host completion
callbacks: p90 1.759 / 1.602 seconds on 1,254 / 667 agent calls. These are selected
finished-session subsets, not the full operation populations. Their matching
end-observation-to-callback-start p90 is 0.192 / 0.286 seconds. The exact division
inside the host callback remains unmeasured; neither GIL contention nor sampler
join is yet established as the dominant cause.

Implement a narrow completion/control milestone first: record callback receipt,
reservation/lifecycle/pressure lock wait and hold, sampler join/final read, and
resource-release stages. Move slow sampling out of the global pressure lock
using VM/generation validation and same-VM serialization; keep completion
ownership and resource accounting correct. Shorten the completion reservation
critical section while retaining final measurement before release. Consolidate
periodic samplers only with preserved per-execution baseline/peak/final/fault
semantics and measured sampling lag. Do not begin with a wholesale async rewrite
or lower offered concurrency to hide queueing.

Then address checkpoint/restore orchestration and custom snapshot work (dispatch
waves, pair sequencing, redundant reads/retries/metadata work), Runtime/sidecar
startup, NUMA migration tails, and artifact collection/validation. Recheck
capacity before dispatching pending reclamation; changes to victim selection,
prediction, retention or action-cost semantics require separate review rather
than being disguised as implementation optimizations.

Current low-priority paths: Tool baseline binding p90 8.44 / 7.65 ms; initial
LOCAL placement p90 17.17 / 21.70 ms; restored LOCAL placement p90 9.66 / 10.26 ms;
CPU placement p90 about 60 microseconds; event-writer lock p90 below a microsecond.
Rare tails remain visible. Prior direct journal measurements were millisecond
scale and do not justify a journal redesign. Native create IRQ/network/readiness
remains reported, but is not the next optimization target without evidence of
ClawBox-added work. The 16 / 32 Cube admission failures and replay failures remain
correctness issues; do not remove failures from denominators or relax budgets.

Validation: compact tests for cross-VM progress, duplicate completion, concurrent
admission, VM replacement and sample integrity; then one 1280 run followed by
2560 if successful. Hold templates, budgets, offered arrivals, snapshot policy
and native binaries fixed. Require lower repeated control/completion latency
without lost measurements, changed admission rules or lower completed work.
Sub-100-ms control overhead is the target, not an achieved result. No new VM run,
deployment or kernel change was performed for this review; the host VM parent
was verified `populated 0`.

Evidence on kunpeng:
`/data/clawbox-create-placement-20261027/clawbox-overhead-review-20261027/`.
`review.py` reproduces `c1280-r2.json`, `c2560.json` and their
`*-completion-subset.json` files in a new output directory. Local copies are in
`.runtime/overhead-review-20261027/`. Per-operation subtraction precedes
quantiling; overlapping stages and cumulative concurrent durations are not
added into an E2E total.

## Update: instrumented Full-1280, 2026-10-27 afternoon

The new run `full-c1280-create-stages-20261027t1515cst` uses instrumentation
commit `5c3b066`, attempt `attempt-ac71205e52fd4f18`, arm
`d1307eb0d194e5816923d8b7`. Evidence root on kunpeng:
`/data/clawbox-create-diagnosis-20261027T1508CST`.

**The journal-first repair priority below is superseded by direct measurements.**
In `analysis/create-stages-20261027T072708Z.json` (1010 completed creates), SDK
create p50/p90 is 2.947/23.397 seconds; journal fsync p50/p90 is
0.001210/0.006623 seconds. SDK time is 99.47% of accumulated outer create-wrapper
wall time, not an E2E fraction. Journal lock-wait max is 0.521 seconds.

Native logs identify a substantial initial cgroup-attachment stall before guest
memory restoration. A 30.187-second SDK call spent about 26.187 seconds before
the shim logged successful LOCAL cgroup entry. A separately correlated shim
(PID 338677, sandbox `4066fc44a5bc460798838d53ad11bd13`) spent 8.849 seconds before
entry and was captured blocked in `cpuset_post_attach -> __flush_workqueue`.
Other shims and Cubelet threads waited in `cgroup_kn_lock_live`. See
`diagnostics/blocked-vm-correlation.json` and
`diagnostics/native-blocked-1793085704.json`. Slow creation was already present
at 920 VMs with zero checkpoint, restore, borrow or memory-scheduling events.

Investigate the native initial `cgroup.procs` attachment and shared kernel
migration queue first. Which queued work makes the flush slow remains unproven;
do not label it measured bulk-copy bandwidth or remove required memory accounting.
The last saved status has all 1280 sessions dispatched, 1052 VMs created,
526 agents runnable, 16 completed sessions and 6 failures; the run is still
active at that snapshot. Refresh its state before further work. The saved host
configuration was reapplied after reboot, and the one approved old crash dump
was removed; the new run retains existing templates, budgets and snapshot policy.
No performance optimization or native rebuild was applied. Raw diagnostics remain
on the host; the local detailed report is `.runtime/create-diagnosis-20261027T1508CST/report.md`.

## 1. Objective and decision rule

Reduce time beyond real guest Tool execution and recorded LLM wait at Full-1280
and Full-2560. Tens of milliseconds are preferred, about 100 ms is acceptable,
and a recurring non-memory interval around or above one second is a critical
investigation target. Waiting for genuinely unavailable memory can be reported
as a capacity wait. CPU scheduling, GIL/thread contention, lock contention,
serial `fsync`, unnecessary scans/hashes, synchronous logging, avoidable
checkpoint waves and inefficient Cube/API use are optimization defects.

Use event occurrence timestamps and per-stage durations. Do not add stage
percentiles, subtract unrelated percentiles, or infer a complete-run E2E result
from the cancelled diagnostic below. Small tests are correctness gates. Any
consequential speed claim needs comparable 1280 and 2560 evidence. Commit and
push each verified milestone, but combine minor fixes into the next material
large run instead of repeatedly rebuilding or replaying the paper workload.

Supported scope is the standalone `clawbox experiment` path, CubeSandbox and
ClawTune integration. Do not create a second experiment path or extend the
deprecated control plane. Preserve the measured replay waits, prediction
semantics, incremental snapshot policy, placement policy and per-session
writable state unless the user explicitly approves a semantic change.

## 2. Primary current evidence and its limits

The latest high-load diagnostic is:

- Run: `full-c2560-service-overhead-v6-cpuset-assignment-20261027t0522cst`
- Attempt: `attempt-ae5551d386034434`
- Arm: `a0e2c177dd4693ea054f15ad`
- Host/native deployment: ClawBox commit `6276057`
- Offered sessions: 2,560 over the frozen 741.1812062263489-second Poisson
  window, about 3.45 session pairs/s
- Outcome: intentionally cancelled after the diagnosis became clear; 25
  validated sessions, 53 observed final responses, 28 failed sessions, 525
  agents runnable, 532 pairs ready and 1,064 sandboxes created
- Cleanup: recorded as verified by the deployment artifact

Remote event stream:

```text
/data/clawbox-service-overhead-20261026T1715CST/v6/runs/
  full-c2560-service-overhead-v6-cpuset-assignment-20261027t0522cst/
  attempts/attempt-ae5551d386034434/a0e2c177dd4693ea054f15ad/
  events/a0e2c177dd4693ea054f15ad.jsonl
```

Derived analysis:

```text
/data/clawbox-service-overhead-20261026T1715CST/v6/analysis/
  full-c2560-6276057-at-2560-offered.json
```

Reproduce it on `ssh kunpeng` with:

```bash
python3 /data/clawbox-service-overhead-20261026T1715CST/v6/analyze_overhead.py \
  /data/clawbox-service-overhead-20261026T1715CST/v6/runs/full-c2560-service-overhead-v6-cpuset-assignment-20261027t0522cst/attempts/attempt-ae5551d386034434/a0e2c177dd4693ea054f15ad/events/a0e2c177dd4693ea054f15ad.jsonl
```

This run is diagnostic, not a complete Full-2560 result. Its service
distributions contain only completed operations. The create-gate distribution
is especially censored: all 2,560 sessions started waiting, but only 547 had
acquired the gate when the run stopped. The observed gate p50 therefore
understates the median across all offered sessions.

Frozen material configuration:

- 160 experimental CPUs, CPU set `0-159`
- two 500-GiB LOCAL domains; 1,000 GiB aggregate capacity
- LOCAL low watermark 917,504 MiB and high watermark 983,040 MiB
- 100 GiB shared-live limit and 100 GiB WARM snapshot capacity on NUMA node 2
- Tool VM 2 vCPU/3 GiB; Runtime VM 2 vCPU/1 GiB
- create gate 16 session pairs, hence up to 32 simultaneous Cube creates
- total lifecycle transfer concurrency 32; checkpoint concurrency 24
- incremental-CoW, tiered WARM/COLD snapshots
- peak sampled experiment cgroup CPU 121.34 of 160 cores
- peak sampled LOCAL/cgroup memory about 879 GiB

The host did not reach the analysis threshold of 140 CPU cores. Memory pressure
was nevertheless triggered from projected demand/reservations: up to 16
in-flight 4-GiB pairs add about 64 GiB of protected headroom. Do not classify
all early overhead as memory pressure: most of the run's Tool calls and 958 of
1,064 creates preceded the first pressure event.

## 3. Current critical overhead ranking

All values are seconds and are observed completed-operation distributions from
the cancelled Full-2560 trace unless noted otherwise.

| Path | n | p50 | p90 | Current judgement |
|---|---:|---:|---:|---|
| Dispatch to create-gate acquisition | 547 | 58.895 | 435.621 | Severely censored backlog; create throughput is below arrivals |
| Create gate acquired to pair ready | 532 | 15.902 | 53.738 | Critical VM activation service |
| Per-VM `api_create_s` wrapper | 1,064 | 12.127 | 39.882 | Not pure Cube time; includes ClawBox journal `fsync` |
| Checkpoint total | 220 | 35.527 | 63.684 | Mostly a 24-slot burst queue |
| Checkpoint transfer-slot wait | 220 | 28.616 | 57.065 | 220 operations arrive in roughly nine waves |
| Checkpoint API wrapper | 220 | 6.673 | 9.157 | Still materially slow service |
| Restore total | 70 | 49.450 | 94.983 | Queue plus very slow restore service |
| Restore transfer-slot wait | 70 | 25.316 | 40.456 | Three or more transfer waves |
| Restore API wrapper | 70 | 31.250 | 50.491 | Critical native/SDK/retry service |
| NUMA live borrow | 2 | 101.833 | 106.210 | Very small sample, but both are unacceptable |
| Agent Tool non-guest time | 27,625 | 2.610 | 6.767 | Dominates the median 85-ms guest command |
| Agent Tool queue to admission | 27,685 | 0.806 | 2.966 | Mostly controller/scheduling, not explicit memory wait at p50 |
| Agent Tool guest end to complete | 27,625 | 1.019 | 3.285 | Completion callback/sampling/release path is too slow |
| Pair ready to SSH warm | 525 | 2.920 | 4.165 | Actual SSH/startup work; warmup-pool queue is small |
| SSH warm to agent runnable | 525 | 1.752 | 2.344 | Runtime setup/sidecar readiness remains above target |
| Destroy | 104 | 5.171 | 7.698 | Usually after final response, but delays capacity release |

Agent Tool guest execution itself was p50 0.085 and p90 0.836. Explicit
memory-admission blocking was p50 0.0033 and p90 0.293. The p50 2.610 seconds
of non-guest time is therefore not explained by genuine memory waiting.

## 4. Critical issue A: VM creation cannot keep up

### What is proven

Sixteen pair slots with p50 15.902-second pair service provide approximately
`16 / 15.902 = 1.0` pair/s at the median. The workload offers about 3.45
pairs/s. The queue must grow. At the current gate, pair service must fall below
about `16 / 3.45 = 4.64` seconds merely to match arrivals.

Raising the gate is not the first fix. Gate 16 already generates up to 32
simultaneous Cube creates because Tool and Runtime are created concurrently in
`start_sandbox_pair()`. Service deteriorated under load. An earlier, lighter
v5 interval observed create API p50 0.262 seconds, whereas the latest high-load
trace observed 12.127 seconds. This is a diagnostic contrast, not a controlled
speedup/regression comparison, but it proves the path is load-sensitive.

The create-stage decomposition in the latest trace was:

- total create p50 12.644
- `api_create_s` p50 12.127
- transfer-slot wait p50 9.6 microseconds
- cgroup preparation p50 1.16 ms
- cgroup verification p50 87.25 ms
- NUMA pin p50 5.53 ms
- placement lock wait p50 3.35 microseconds

Before first pressure, `api_create_s` p50 was about 10.917 seconds; after
pressure it was about 38.700 seconds. Tool and Runtime were close:

- Tool: all 12.348, before pressure 11.272, after pressure 42.440 p50
- Runtime: all 11.859, before pressure 9.977, after pressure 38.339 p50

The refreshed Tool images therefore are not the create bottleneck. Pressure
amplifies the problem, but an avoidable roughly 11-second path exists before
pressure.

### Verified measurement defect and likely implementation bottleneck

`clawbox/cube/lifecycle.py` records `api_create_s` around the entire
`CubeSandboxClient.create_sandbox()` call, not just the official SDK request.
After `Sandbox.create()` returns, `clawbox/cube/client.py:218-221` inserts the
handle and calls `OwnedSandboxJournal.record()`.

`OwnedSandboxJournal.record()` at `clawbox/cube/client.py:49-56` takes one
process-wide lock, opens the file, writes, flushes and calls `os.fsync()` while
holding that lock. With up to 32 simultaneous creates, ownership records and
their durable disk flushes serialize inside the interval currently labelled
Cube API time. Snapshot I/O can make those `fsync`s much worse.

The contamination and serialization are proven in source. Their exact share
of the 12.127-second p50 is not yet measured. Do not state that Cube itself is
12 seconds or that the journal is the entire 12 seconds.

Our parallel API pattern is not inherently wrong. Cube's official concurrent
create example also invokes `Sandbox.create()` from a thread pool. Cube's
published benchmark reports subsecond creates at concurrency up to 50 on a
different x86/NVMe host with smaller 2-GiB VMs. Those absolute numbers are not
a baseline for this ARM host, but they show that 10-40 seconds is not an
intrinsic API requirement:

- https://github.com/TencentCloud/CubeSandbox/blob/master/examples/snapshot-rollback-clone/bench_create_concurrency.py
- https://github.com/TencentCloud/CubeSandbox/blob/master/docs/blog/posts/2026-06-01-cubesandbox-perf-benchmark.md

### Required repair and proof

1. Split `sdk_create_s`, `journal_lock_wait_s`, `journal_write_s` and
   `journal_fsync_s`. Keep the existing outer total.
2. Run the official direct create pattern and the ClawBox wrapper side by side
   on this host/template at concurrency 1, 10, 20, 32 and 50. Record throughput
   as well as p50/p90. Do not launch another Full run to answer this split.
3. Replace one-`fsync`-per-create with a durable group-commit writer: queue
   records, append all currently pending records under one writer, issue one
   `fsync`, then acknowledge every record in that batch. A persistent file
   descriptor is sufficient. Do not remove durability: cleanup after a worker
   crash relies on this journal.
4. Re-run the direct/wrapper sweep and choose the create gate at the throughput
   knee. Do not assume 16 or 50 is optimal.
5. If the SDK/request remains material after removing journal time, test Cube's
   official direct asynchronous HTTP approach inside `CubeSandboxClient`, not
   as a second experiment workflow:
   https://github.com/TencentCloud/CubeSandbox/blob/master/CubeAPI/examples/benchmark.py
6. Only then validate the combined result at 1280 and 2560. If sustainable
   capacity remains below 3.45 pairs/s, report it honestly and lengthen the
   offered-arrival window as a capacity-matched experiment; that is not a
   latency optimization.

Do not pre-create a generic pool without a design review. Session-specific
environment variables, credentials, owner metadata and cgroup placement are
currently applied at creation. Pooling would change semantics unless bootstrap
injection is redesigned.

## 5. Critical issue B: excessive Python threads and GIL/OS scheduling

### What is proven

`session_worker_count()` returns the arm concurrency for a normal run, and
`worker.py:2426-2428` creates a `ThreadPoolExecutor(max_workers=2560)`. All
session tasks are submitted; workers sleep until their Poisson offer and then
block on gates. Each pair also creates a fresh two-thread executor at
`worker.py:97-110`. The warmup pool adds up to 64 threads.

Every admitted Tool command constructs `SandboxRSSSampler`. It creates a daemon
thread, wakes every 50 ms, and reads `memory.current` plus per-process RSS/fault
counters (`memory.py:546-645`). Completion calls `host_sampler.stop()`, joins
that thread, takes final samples and releases memory/CPU while holding the
session's `reservation_lock` (`worker.py:4129-4179`).

At high load, normally trivial Python stages show random wall-time inflation:

- prediction/queue p50 59 microseconds, but max 4.426 seconds
- bookkeeping p50 17 microseconds, but max 4.453 seconds
- lifecycle-lock p50 below one microsecond, but max 9.791 seconds
- memory baseline p50 1.32 ms, but max 2.428 seconds
- sampler start p50 5.56 ms, but max 3.488 seconds

The machine still had global CPU headroom. This pattern is consistent with GIL
and OS descheduling among thousands of threads, not with those individual
operations performing seconds of useful work. Stacks captured during earlier
pressure also show thousands of `agent_*` threads; see
`.runtime/service-overhead-20261026T1715CST/analysis/worker-stacks-v2-pressure.txt`.

The exact composition of guest-end-to-complete p50 1.019 seconds remains
unmeasured. The likely contributors are callback scheduling, acquiring
`reservation_lock`, joining the per-command sampler, final `/proc`/cgroup
reads, coordinator/CPU release and event emission. Treat this as a hypothesis
until those sub-stages are timestamped.

### Required repair and proof

1. Replace one sleeping worker per offered session with one arrival scheduler
   or priority queue. At each due offset, enqueue the session into bounded
   create/active work; do not materialize 2,560 blocked Python workers.
2. Bound the active-session executor by real runnable/resident capacity. Keep
   the same frozen offer offsets and record dispatch lateness so scheduler lag
   is not hidden.
3. Replace one `SandboxRSSSampler` thread per command with one shared sampler
   service that registers active sandbox/execution IDs, batches 50-ms reads and
   unregisters them on completion. Preserve per-execution baseline, peak,
   final, fault and validity fields.
4. Move sampler stop/final sampling outside `reservation_lock` where safety
   permits. Keep reservation publication/removal atomic.
5. Add timestamps for callback receipt, reservation-lock wait/hold, sampler
   stop/join, final sample, coordinator release, CPU release, activity update
   and event write.
6. Prefer async/nonblocking I/O for Cube HTTP, policy calls and similar control
   waits. Use a small bounded thread pool only for blocking SDK operations.
   Process pools are appropriate only for demonstrated CPU-bound Python work;
   most of this path is I/O and scheduling.
7. The current 1-ms Python thread switch interval is only a mitigation. Do not
   claim it solves this architecture.

Acceptance is not just lower CPU. Under comparable Full-1280 and Full-2560
load, non-memory Tool queue-to-admit and guest-end-to-complete should approach
the sub-100-ms goal without losing measurement records or changing guest Tool
execution.

## 6. Critical issue C: checkpoint/restore burst queues and slow service

### Queueing is exactly attributable

The snapshot transfer gate is 24. A pressure decision produced 220 completed
checkpoints, roughly nine waves. Checkpoint API p50 is 6.673 seconds; a median
request waiting roughly four earlier waves predicts about 26.7 seconds, close
to the measured 28.616-second transfer wait. This is a bounded resource queue,
not a mutex bug, but the policy's burst turns expected serialization into an
end-to-end cliff.

Seventy restores require at least three waves, and the restore operation itself
has p50 31.250 seconds. Later waves therefore wait tens of seconds. Increasing
24 blindly may reduce queue time but worsen every operation through storage and
memory-bandwidth contention.

Checkpointing starts from projected demand, not only instantaneous RSS. The
sampled peak of about 879 GiB is below the 917-GiB low watermark, but in-flight
pair reservations can add about 64 GiB. This capacity protection is reasonable.
The 220-operation burst and slow service are the optimization targets.

### Service path and remaining unknowns

Checkpoint uses `pause(wait=True)` plus the custom direct tiered
incremental-CoW path, snapshot manifest validation and metadata/lineage reads
(`client.py:600-648`, `lifecycle.py:398-510`). Restore calls official
`Sandbox.connect()` with `snapshot_mechanism=incremental-cow` through ten
attempts of 0.25-to-2-second retry/backoff (`client.py:224-241`). The current
event does not expose retry count or sleep, so `api_restore_s` is not pure
hypervisor work.

Plausible checkpoint service components are pause/device state, dirty-page
scanning/writing, WARM tmpfs or COLD XFS bandwidth, metadata/lineage work and
contention among 24 writers. Plausible restore components are Cube scheduling,
shim/hypervisor state restoration, external-base/private-delta mapping, page
tables/faults and hidden retry/backoff. These components are not yet separately
measured.

Cube's official simple benchmark reports much faster pause/resume on a
different host and workload, so 31-second restore should not be accepted as an
API invariant. Official lifecycle semantics still use pause and connect; the
basic API choice is correct:

- https://github.com/tencentcloud/cubesandbox/blob/master/sdk/python/README.md
- https://github.com/TencentCloud/CubeSandbox/blob/master/docs/blog/posts/2026-06-01-cubesandbox-perf-benchmark.md

### Required repair and proof

1. Log each restore attempt, response/error, retry sleep and successful Cube
   call separately. Log checkpoint dirty/logical/allocated/transferred bytes,
   storage tier, generation and lineage depth. The current API sometimes cannot
   expose transferred bytes; state that explicitly instead of inventing it.
2. Correlate operation IDs with CubeMaster, Cubelet and CubeShim timestamps:
   request receipt, scheduling, pause/restore begin, memory/device work and
   response.
3. Run a targeted transfer sweep at 8, 16, 24 and 32 using representative
   3-GiB Tool snapshots. Measure aggregate operations/s, per-operation latency,
   memory bandwidth and storage bandwidth. Select the throughput knee.
4. Stagger/coalesce pressure checkpoint dispatch. Start reclaim earlier and
   maintain headroom rather than releasing about 220 operations in one wave.
   Prioritize low-value/oldest-idle VMs and avoid checkpointing a VM likely to
   execute again immediately.
5. Keep hot snapshots in WARM when beneficial; use COLD for colder state.
   Compact/rebase long incremental lineages only if measured restore time
   correlates with lineage depth.
6. Compare the custom incremental-CoW path with the installed Cube native
   full-copy pause/resume on the same host and VM size as a diagnostic. Do not
   silently change the frozen experiment mechanism; qualifying another
   mechanism creates a new baseline.

## 7. Critical issue D: NUMA borrow spends tens of seconds in cpuset work

The latest run completed only two live borrows, so no population percentile is
valid. Both are nevertheless severe:

- borrow 1: total 96.361; transfer wait 10 microseconds; native prepare 1.293;
  pre-migrate 4.226 with two pages remaining; **cpuset phase 90.678**; resume
  5.4 ms
- borrow 2: total 107.304; transfer wait 35.825; native prepare 3.005;
  pre-migrate 0.457 with zero pages remaining; **cpuset phase 67.938**; resume
  5.6 ms

The slow phase is now the cgroup cpuset transition, not bulk page migration.
`numa_rebind.py:136-174` creates threaded source/target children, attaches the
KVM process, changes the parent memory mask, attaches to the target, restores
affinity, attaches back and removes the children. The existing timer covers all
of those operations as one `cpuset_seconds` interval. Even when pre-migration
leaves zero or two pages, the kernel cpuset path can flush global migration
work or wait under cgroup/cpuset serialization.

An earlier source bug held a borrower-domain lock across slow migration and
blocked unrelated restore placement checks. That was changed to per-VM locks.
The latest restore `pin_local` lock-wait p50 is 2.8 microseconds and p99 below
four microseconds; do not reopen the old global-lock design. The remaining
problem is the kernel/cgroup operation itself and the one borrow that also
waited 35.8 seconds for a transfer slot.

Required next work:

1. Time every `_write`/attach in `numa_rebind.py`: source creation/config,
   source attach, parent mask expansion, target creation/config, target attach,
   affinity restore, parent final mask, return attach, child removal and
   subtree disable.
2. Capture short kernel stacks/perf only while a slow cpuset write is blocked.
   Determine whether the wait is page migration, ordered workqueue flush,
   cgroup attach serialization, reclaim or another kernel wait.
3. Check current official CubeSandbox placement/cgroup APIs and Linux cpuset
   semantics before preserving the custom threaded-child dance. A replacement
   must keep existing-page migration, future-allocation policy, KVM pause/RAM
   detach, dirty-bit correctness, CPU affinity and residual shared-memory
   accounting. Do not optimize by weakening those semantics.
4. Test unrelated VM progress while VM A is blocked, same-VM serialization,
   failure after prepare and reservation cleanup. Then validate actual borrow
   latency under 1280/2560 pressure.

## 8. Remaining startup, completion and cleanup overhead

### Startup after pair readiness

The old 2026-10-26 diagnostic saw sidecar readiness p50 25.739 seconds. Runtime
predictor caching, startup-gate parallelism, reusable SSH connections, image
preparation and OpenClaw skill-refresh changes substantially reduced the newer
trace, but current startup still exceeds the target:

- Runtime setup stage p50 about 1.082 seconds
- sidecar-ready stage p50 about 2.332 seconds
- OpenClaw configuration p50 about 43 ms
- native SSH warmup worker queue p50 about 1 ms and p90 about 7 ms
- native SSH warmup execution p50 about 0.643 and p90 about 1.205 seconds

The shared 64-worker warmup queue is no longer the main cause. Actual Runtime
setup, sidecar readiness and transport work remain. Preserve strict sandbox
identity/host-key verification. If optimizing next, timestamp RPC send/return,
guest process launch, listener-ready and health success; reuse established
connections rather than weakening identity checks.

### Completion

Guest-end-to-complete p50 1.019 seconds is critical because it occurs after
real Tool work and delays result delivery and reservation reuse. Section 5
describes the likely sampler/join and scheduling causes. Instrument it before
changing release ordering.

### Destroy

Destroy p50 5.171 and p90 7.698 generally occurs after the final-response E2E
endpoint, but it delays VM/cgroup/memory release and therefore worsens later
sessions. Split Cube delete, physical disappearance observation, cgroup cleanup
and journal/ledger cleanup. Optimize it after create, thread/sampler and
snapshot work unless evidence shows it is blocking capacity sooner.

### Secondary tails

- create transfer-slot wait is microseconds through p90 but p99 27.910 seconds;
  snapshot waves can consume the shared 32-operation lifecycle gate
- create cgroup verification p99 1.631 seconds
- restore cgroup verification p50 0.249, p90 1.118 seconds
- per-session lifecycle lock is normally sub-microsecond but has a 9.791-second
  max; log its holder before calling it a lock bug because same-VM lifecycle
  serialization can be required
- 28 observed session failures include unexpected replay Tool failures and
  incomplete gateway sessions; preserve them in denominators and do not report
  a performance win by dropping failed work

## 9. Confirmed fixes and findings not to repeat

These are already implemented or ruled out as current primary bottlenecks.
Preserve them unless a new trace disproves them.

| Item | Current status/evidence |
|---|---|
| Tool BCC/Clang compilation on first command | Moved into a resident prewarmed collector captured by each Tool template. Old first command 6.582 seconds; refreshed-template trials 0.438 and 0.387 seconds, mean about 0.413. The saving is about 6.17 seconds. |
| 503 Tool templates | `/data/clawbox-service-overhead-20261026T1715CST/combined/collector-refresh/collector-update-state.json` has 503 records, all `template-ready`, with 503 template IDs. Workload/catalog point at them. Do not rebuild them again unless the collector or Tool image content changes. |
| Template validity | Pilot checked first/second command, collector health, preserved collector PID across bridge restart and cross-VM private-state isolation. The refreshed catalog also passed 503-case qualification at concurrency 32. This is correctness evidence; the latest Full run is the high-load usage evidence. |
| Runtime reusable state | Predictor cache and seed cache are built/exposed in the Runtime image; unusable Runtime eBPF startup is skipped. Preserve commits `2ac74d0`, `ff74a29`, `e4410e3`. |
| Repeated image inventory/startup gates | One Cube inventory validates templates and immutable gates run in parallel. Preserve `6a04bc6`, `3e57117`. |
| SSH connection setup | OpenSSH connections are reused within endpoint epochs; OpenClaw SSH skills refresh once per turn rather than every command. Preserve `f3809c4`, `fb9ac8d`. |
| Admission global scans/locks | High-concurrency scans, duplicate active locking and baseline publication under the global lock were removed; low-pressure activation/admission was fused. Preserve `17a62ea`, `a23d61b`, `b7eeddc`, `213af8b`, `089b9d3`. |
| Memory baseline locks | Latest 35,245 observations show zero measured lookup/publish lock wait; baseline service p50 1.32 ms, p90 9.74 ms. Not the current median bottleneck. |
| Event writer | It now keeps a buffered handle and flushes every 128 events or 250 ms. Lock wait p50 0.63 microseconds, p90 0.93 microseconds, p99 2.84 ms across 258,350 events. Do not prioritize another logging rewrite without CPU/I/O evidence. |
| Typical prediction/bookkeeping | Prediction p50 59 microseconds and bookkeeping p50 17 microseconds. Their multi-second maxima are evidence of global scheduling delay, not expensive prediction logic. |
| NUMA placement lock | Per-VM lock wait is microseconds in the latest create/restore trace. The old borrower-wide lock problem is not the current restore bottleneck. |
| CubeMaster debug volume | Host apply now persists the correct uppercase log level. Preserve `9d7ae60`, `581e6e4`, `4f285a9`. |
| Create capacity rejection | Definite Cube 130597 rejections retry outside the shared lifecycle slot; ambiguous POST timeouts reconcile ownership rather than duplicate creates. Preserve `c2c5024`. |
| Initial/reused cgroup placement | Current native patches assign cpuset memory before publishing reused leaves and preserve CPU affinity without redundant memory-policy syscalls. Preserve `bda9eb6`, `aee3f6a`, `6276057`; these do not solve later live-borrow cpuset transitions. |

The prewarmed collector is ClawTune code packaged into the Tool image/template.
A ClawTune change requires refreshing the affected templates only when it
changes that packaged collector or its dependencies. Runtime-only or host-only
ClawTune changes do not imply rebuilding all 503 Tool templates. Cube templates
are pre-warmed microVM snapshots, so per-template process state is captured;
identical underlying image blocks can be shared/reflinked, but the 503 template
identities remain distinct.

Official template guidance:
https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/templates.md

Refresh/validation evidence is at:

```text
/data/clawbox-service-overhead-20261026T1715CST/v4/prewarm-probe.json
/data/clawbox-service-overhead-20261026T1715CST/combined/collector-refresh/collector-update-state.json
/data/clawbox-service-overhead-20261026T1715CST/combined/collector-refresh/catalog.json
/data/clawbox-service-overhead-20261026T1715CST/v5/template-sharing.json
/data/clawbox-service-overhead-20261026T1715CST/v6/qualification-cpuset-memory.json
```

## 10. Recommended milestone order

### Milestone 1: make create time truthful and remove serial journal flushes

- Add the four create/journal stage timers.
- Run the small direct-versus-wrapper concurrency sweep.
- Implement durable journal group commit and targeted crash/cleanup tests.
- Re-run the sweep and select the create gate from throughput.
- Commit and push with the diagnostic artifact path and exact before/after
  service/throughput numbers.

This is first because VM backlog dominates startup and because the current
measurement incorrectly attributes ClawBox work to Cube.

### Milestone 2: remove thread explosion and per-command sampler threads

- Implement the event-driven arrival scheduler and bounded active work pool.
- Implement one shared sampling service.
- Add completion sub-stage timestamps and preserve all measurement fields.
- Use targeted concurrency tests, then one Full-1280 and one Full-2560 run for
  the combined scheduler/sampler change.
- Commit and push the verified result.

### Milestone 3: reshape and profile checkpoint/restore

- Add retry, bytes, tier, generation, lineage and native server timestamps.
- Sweep transfer concurrency with representative snapshots.
- Stagger/coalesce pressure actions and select the measured concurrency knee.
- Validate checkpoint integrity, restore state, cleanup and Full-scale latency.
- Commit and push.

### Milestone 4: replace or repair the live-borrow cpuset transition

- Split every cgroup operation and capture blocked kernel stacks.
- Prefer an official Cube/Linux mechanism if it preserves the same placement,
  migration and accounting semantics.
- Verify same-VM safety and unrelated-VM progress, then validate at pressure.
- Commit and push.

### Milestone 5: remaining startup, destroy and tails

- Optimize the measured sidecar/SSH phase and destroy service only after the
  larger items above.
- Combine small tail fixes into the next consequential run.

Do not start by rebuilding images, changing Cube versions, raising all
concurrency limits or rerunning the full baseline. The 503 refresh is complete.
A Cube upgrade is a new baseline and requires separate qualification.

## 11. Acceptance and reporting

For every important fix, preserve:

1. exact source commit and pushed branch;
2. frozen YAML, workload/catalog hashes and offer offsets;
3. unique run/attempt/arm IDs and complete event JSONL;
4. host/native deployment identity;
5. operation counts plus failed/cancelled/incomplete counts;
6. p50/p90/p99 service and queue distributions with definitions;
7. CPU, memory and storage pressure during the measured window;
8. cleanup verification and owned-sandbox ledger.

Report scheduled-arrival-to-final-response E2E separately from validation and
cleanup. Compute LLM and Tool wall-time coverage as interval unions; concurrent
Tool durations cannot be summed and subtracted from E2E. Separate genuine
memory admission waits, transfer-slot waits, create-gate waits and actual
service. If a run is cancelled, report the outstanding queue ages and the
denominator censoring.

The end condition is not merely passing tests. The supported workflow must
retain correct results and metadata, while the largest non-memory critical-path
overhead is reduced as far as the time budget reasonably allows at both 1280
and 2560. If a component remains above one second after a measured best effort,
document its demonstrated hardware/native limit and move to the next-largest
critical-path source rather than spending hours on speculation.

## 12. Current workspace and remote cautions

- Local workspace at handoff creation: `C:/Users/29068/Desktop/ClawBox`, branch
  `main`, starting HEAD `6276057`, clean before this new handoff.
- Main remote study: `/data/clawbox-service-overhead-20261026T1715CST`.
- The latest run's outer state is terminal/cancelled and cleanup is verified,
  although its nested arm record can still say `running`; refresh API/process
  inventory before assuming anything remains active.
- Preserve unrelated paused sandboxes, `anns`, old template storage and other
  workloads. Clean only IDs proven to belong to the selected run ledger.
- `.runtime` is ignored and contains local diagnostic copies. A different
  machine needs the remote `/data` artifacts or an explicit evidence copy; Git
  alone contains the handoff and source, not the large event stream.
- Follow `AGENTS.md`: use maintained scripts/CLI, do not reinstall the shared
  host for a test, keep public configuration documented, and distinguish unit,
  qualification, real-VM and Full-scale evidence.
