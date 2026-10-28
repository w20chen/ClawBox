# Full-1280 admission and finalization optimization

Measured 2026-10-28, Asia/Shanghai. This is an internal evidence report.

Current implementation: `aba9903`, pushed to `main`. The final default-4-MiB
c1280 window has admission callback median **44 ms**, mean **144 ms**, and p90
**186 ms** with recorded queue waits reported separately. Final response to
validated completion averages **4.65 s**, p90 **7.57 s**, across 47 successful
sessions. The p90 admission target is met; the mean remains above tens of
milliseconds because long tails remain. See the continuation below for queue
attribution, incomplete sessions, VM timings, and validation scope.

The earlier c1280 result below measures internal memory preparation, not the
whole admission callback. A later timestamp audit of the same 799.9-second
window found full callback overhead of **1.653 s mean / 5.033 s p90**, excluding
recorded queue sleep. The earlier claim that the aggregate Tool objective
was met was too broad. The logging/VM continuation is recorded below.

Excluding capacity and FIFO condition sleep, 24,355 internal Tool preparations
average 72.8 ms with p90 83.4 ms. The observed fully successful response-to-result cohort averages 6.36
seconds with p90 7.64 seconds; all 26 retained native-validation scopes average
5.88 seconds with p90 8.03 seconds. The baseline values were 5.691 seconds mean
/ 13.634 seconds p90 for Tool preparation and 146.648 seconds mean for
response-to-completion.

The result deliberately uses the configured low-memory compromise: predictions
at or below 4 MiB remain active and measured but do not enter the memory
admission ledger. At the user's subsequent request, the default is now 4 MiB;
an explicit zero disables this bypass. The separate accounted below-LOW fast
path remains enabled.

## Result

| Metric | Attribution baseline | Final c1280 evidence |
| --- | ---: | ---: |
| Tool preparations | 28,402 | 24,355 |
| Preparation mean, raw | 5.691 s | 0.313 s |
| Preparation p90, raw | 13.634 s | 0.083 s |
| Preparation mean, excluding capacity/FIFO sleep | Not separately reported | 0.073 s |
| Preparation p90, excluding capacity/FIFO sleep | Not separately reported | 0.083 s |
| Final response to validated `session_complete`, mean | 146.648 s (30 sessions) | 6.363 s (5 sessions) |
| Final response to validated `session_complete`, p90 | Not reported | 7.640 s |

The raw preparation mean remains larger than its p90 because a small pressure
cohort waits behind FIFO predecessors. Removing capacity, FIFO and eviction
sleep changes the mean but not the aggregate p90:

| Branch | Calls | Share | Implementation mean | Implementation p90 |
| --- | ---: | ---: | ---: | ---: |
| Unaccounted prediction at or below 4 MiB | 23,157 | 95.1% | 33.3 ms | 68.4 ms |
| Accounted below-LOW fast path | 871 | 3.6% | 131.1 ms | 180.3 ms |
| Accounted pressure/routing path | 327 | 1.3% | 2.716 s | 7.097 s |

Thus the internal preparation p90 is below one hundred milliseconds, but the rare
accounted pressure path is not solved to that standard. Its remaining exclusive
implementation time is mainly lock waiting, exact claim sampling, and physical
sampling. It no longer limits the common path; raising the bypass above 4 MiB
would reduce it further at a larger unaccounted-memory risk and was not done.

The 4 MiB threshold is an explicit policy tradeoff. One simultaneous bypassed
call in each of 1,280 sessions omits at most 5 GiB of predicted claims, about 0.52% of this run's
983,040 MiB policy budget; multiple simultaneous calls scale that exposure.
Actual memory is not bounded by the prediction. The editable
`mechanisms.admission.small_tool_admission_bypass_mib` defaults to four and the
run metadata records bypassed calls as `unaccounted-small` with zero admitted
memory while retaining their predictions and measurements.

## What changed

- Admission kernel observations are taken outside the atomic condition and an
  in-flight read is shared only while it is executing. Exact claim identities
  are frozen briefly, read with at most sixteen readers, then reconciled against
  current claims at commit; there is no time-based capacity cache.
- FIFO followers use per-request events. Queue progress wakes the head plus a
  bounded batch of existing-session progress requests. The priority admission
  lock preserves a bounded progress handoff across condition release/reacquire.
- A configured small Tool uses a single active-state update and neither reads
  nor changes the memory ledger. It remains protected from reclamation and its
  actual memory is still sampled.
- Model-wait pressure notifications are single-flight per coordinator: one
  running scan and at most one dirty trailing scan. Finalization no longer
  drains obsolete global pressure work; a progress-priority active-state update
  is the session-local safety barrier.
- Runtime logs/traces and Tool evidence each use one compressed transfer rather
  than one SSH command per file. Members are decoded without filesystem
  extraction, checked as regular safe-named files, and written to the existing
  result layout in parallel.
- Runtime trace validation excludes only a Tool request whose own structured
  result says it was rejected with `failureKind=runtime-error`, no exit code,
  and “command was not executed.” Such a request never crossed policy admission
  and cannot have a native Tool artifact. All other runtime errors remain in
  the exact-ID check.

Admission is not served by one worker thread. HTTP callbacks and session work
run concurrently, while each memory domain intentionally serializes its atomic
ledger decision. Adding decision workers would increase contention on that
shared state. Parallelism is used for independent kernel reads and artifact
decoding; coalescing and bounded wakeups reduce redundant contenders. In the
final window the policy executor saw 9,902 submissions, coalesced 7,500, executed
3,811 scans, used at most two active coordinator keys, and recorded zero
failures.

## Validation and limitations

- Performance run:
  `full-c1280-admission-finalize-opt-v11-20261027T193859z`, attempt
  `attempt-e236450b66c546a4`, source archive SHA-256
  `579af3c00a3546e786309cb41e20cc509b1e8d7ba6b24d630d5c45c1b8d4c0db`.
  All 1,280 sessions were dispatched before the fixed window was stopped.
- The c1280 qualification completed 1,280/1,280 valid sessions with cleanup
  verified. In that synchronized first-step cohort, 7,456 preparations measured
  1.48 ms mean / 2.69 ms p90 / 26.8 ms p99.
- The full workload reached 27 final responses before cutoff. Five originally
  reached `session_complete`; two failed later ModelGateway completeness and one
  had an expected replay Tool-result mismatch. Eighteen native validations were
  initially false negatives caused by the pre-execution rejection described
  above. Offline revalidation of the immutable raw files with the scoped fix
  passes all 26/26 collected artifact sets (8 previously valid plus 18
  corrected). It does not invent later task/replay outcomes for those sessions.
- Combining all 26 now-valid collection scopes gives 5.882 seconds mean, 5.378
  seconds p50, 8.033 seconds p90, and 9.102 seconds maximum from final response
  to validation-scope end. The five observed full successful completions measure
  6.363 seconds mean / 7.640 seconds p90.
- Both the worker samples and the independent host observer recorded zero
  `memory.max`, OOM, and OOM-kill events. The policy event executor recorded
  zero failures. Supported abort completed with `populated 0`, `frozen 0` and
  terminal state `cancelled`.
- The frozen remote regression selection passed with four skips. The final
  trace-classification correction passed the focused native-artifact and
  OpenClaw suites remotely, also with four skips.
- This was the user-selected c1280 validation. No c2560 run was made. Checkpoint
  and restore were intentionally left for a later study; this report makes no
  checkpoint improvement claim.

## Evidence

- Performance and immutable run evidence:
  `/data/clawbox-overhead-optimization-20261028/candidate-v11-c1280`
- Final analysis: `analysis-final.json`; cutoff: `pre-abort-state.json`; cleanup:
  `cleanup-cgroup.txt`; source/config/qualification: `manifest.json`,
  `experiment.yaml`, and `qualification.json`.
- Scoped post-run correction and offline audit:
  `/data/clawbox-overhead-optimization-20261028/final-validation-20261028`,
  especially `offline-native-revalidation.json`.
- Earlier rejected candidates remain under the same parent directory with
  unique run IDs; none replaced an earlier result.

## Logging and VM continuation

The continuation uses `ssh kunpeng` and offered concurrency 1280. Its evidence
root is `/data/clawbox-vm-overhead-20261028`. Comparisons use the first 799.9
seconds after dispatch; they include incomplete queues and failed sessions.
The source archive, configuration, qualification receipt, events, launch log,
host samples, and cutoff remain separate for every candidate.

The completed `candidate-v5-publication-c1280` window offered 1280 sessions,
created 653 VM pairs, observed 46 final responses, and validated 18 sessions;
28 sessions failed replay/task checks. Across 25,092 completed admissions,
the full callback measured 165.5 ms mean / 321.8 ms p90 after subtracting
recorded capacity/FIFO/eviction sleep. Internal preparation alone measured
46.1 / 68.0 ms. Late-window callbacks were slower: 311.8 / 489.6 ms. These
partial, different completion cohorts do not establish a throughput speedup.
FIFO and eviction waits are not independently proof of a memory shortage;
the later lightweight schema retains queue duration by reason to distinguish
direct resource waiting from unresolved queueing.

The pair lifecycle window fell from 2.767 / 7.942 seconds mean/p90 to
1.580 / 3.622 seconds. Tool cgroup verification fell from 1.162 / 3.117
seconds to 0.379 / 0.898 seconds. These lifecycle windows exclude subsequent
pair registration and startup; the raw stage timestamps retain those costs.
Final response to successful validated completion remained 7.222 / 9.394
seconds for 18 sessions, so this candidate did not finish the finalization work.

Implemented changes:

- Pressure and C2 controllers retain short registry/accounting locks and use
  per-session locks for cgroup I/O. C2 reserves capacity before affinity I/O
  and rolls it back on failure. Same-VM generation ordering is retained.
- Resource preparation and actual creation are separate bounded stages. A
  memory-blocked preparation no longer occupies an actual creation slot.
- Event producers serialize immutable rows and enqueue through `SimpleQueue`;
  one consumer sequences and batches writes. Reporting also drains queued
  activity transitions instead of blocking producers behind its snapshot.
  V5 event publication averaged 40.3 microseconds with p90 55.5 microseconds.
- `execution.control_timing_details=false` keeps compact timings and required
  evidence. Nested spans, GC tracing, repeated raw lifecycle cgroup dumps,
  repeated session payloads, and copied Runtime working KBs require opt-in.
- Runtime helper shutdown shares the evidence-export command. Normal exports
  omit `traces/tool-resource`, which native validation explicitly skips.
  The retained trace payloads were verified byte-for-byte: one representative
  export falls from 23,539,272 to 4,595,530 uncompressed bytes. KB use and
  learning inside the Runtime are unchanged; only extra host copies are opt-in.
- Active Tool samplers retain the cgroup counter descriptor and use positional
  reads on Linux, preserving fresh samples, fault-counter identity checks,
  and the 50 ms sampling cadence.

A 100-microsecond interpreter handoff candidate was rejected and stopped early:
the worker reached approximately 126 CPU cores of consumption, mostly kernel
time, while progress stalled at roughly 276 pairs. Its stacks and cutoff are
retained in `candidate-v6b-light-100us-c1280`. The supported default remains
1 ms. No kernel/Cube upgrade or checkpoint redesign was performed.

`candidate-v7-minimal-c1280` validates the complete candidate with 1 ms handoff,
4 MiB bypass, and LOW accounting enabled. Its remote regression selection
passed 388 tests with 11 platform-dependent skips before launch. The archive
SHA-256 is `4dc913ad1eb076994b8ee66ac08b2465781a1043e70dc39d675962f7231f81d6`;
`source-verification.json` confirms all 243 Python source/test files match the
working files committed as `aba9903`.

| Metric | Earlier v11 baseline | Final v7 candidate |
| --- | ---: | ---: |
| Full callback excluding recorded queue sleep, mean / p90 | 1.653 / 5.033 s | 0.144 / 0.186 s |
| Same callback, median | 0.244 s | 0.044 s |
| Internal preparation excluding recorded queue sleep, mean / p90 | 0.073 / 0.083 s | 0.070 / 0.033 s |
| Tool cgroup verification, mean / p90 | 1.162 / 3.117 s | 0.206 / 0.436 s |
| Runtime/Tool pair lifecycle window, mean / p90 | 2.767 / 7.942 s | 1.543 / 3.832 s |
| Final response to validated completion, mean / p90 | 6.363 / 7.640 s (5 sessions) | 4.645 / 7.570 s (47 sessions) |

The v7 window contains 39,596 completed admissions, 39,150 completed Tool
operations, 676 created pairs, 68 final responses, 47 validated sessions, and
20 failed sessions. Another 118 admission requests have no completed callback
at cutoff and are excluded from completed-callback latency statistics.
At cutoff, 588 sessions still wait to enter preparation
and 16 have entered placement without finishing it. One observed final response
has not reached a terminal task outcome. These are fixed-window measurements,
not a claim that all 1280 sessions completed or a statistically replicated
throughput comparison. Existing replay/completeness failures still fail the run.

The small path handles 37,455/39,596 admissions (94.59%): 28,250/30,391 agent
Tools and all 9,205 backend maintenance operations. Accounted LOCAL calls still
have 0.890 s mean / 2.415 s p90 service overhead; 39 shared-route calls average
2.477 s. The late-window aggregate p90 is 309 ms. Rare callback tails reach
66.9 s and are retained, not discarded.

Queue attribution matters: 915 callbacks record FIFO-predecessor sleep averaging
36.74 s. Without assuming that FIFO sleep is genuine memory scarcity, excluding
only direct recorded memory blocks gives 0.993 s mean and the same 0.186 s p90.
The 144 ms headline is service outside recorded queues; it must not be described
as an exact total of all avoidable overhead. Mutex/worker delays remain included
in that service metric. The policy wrapper adds only 75 microseconds on average
on the 5,873 retained session records checked separately.
`fifo-resource-context.json` finds a concurrent memory-budget blocker in 279 of
306 per-domain snapshots with FIFO waiters (15,676 of 17,463 summed observed
FIFO depths). This supports resource pressure as an important cause of those
queues, but does not identify every individual FIFO interval exactly.

The remaining Tool create API averages 1.243 s / p90 3.433 s; Runtime API
averages 1.122 / 3.120 s. Those API values include SDK/journal work, not just
native boot. Raw lifecycle memory observation before/after creation is now
about 20 ms each. Pair registration/startup is separate from the lifecycle
window. Runtime evidence export averages 0.637 s and decoding 0.159 s.
Successful VM cleanup after validation adds time: final response through cleanup
averages 8.371 s / p90 11.540 s on 46 observed successful cleanups. Validation
latency therefore must not be presented as the full cleanup latency.

Supported abort verified an empty VM parent before the identical-source
`candidate-v8-no-small-bypass-c1280` control launched with threshold zero and
LOW accounting unchanged. Both 800-second windows have now finished and
supported abort verified cleanup (`populated 0`, `frozen 0`).
`bypass-config-verification.json` confirms identical source archives and only
the 4-to-0 MiB configuration difference.

The control offered 1280 sessions, created 595 pairs, completed 32,411 successful
admissions plus one failed callback, observed 38 final responses, and validated
28 sessions. Its 18 failed sessions include nine Cube API `no more resource`
errors and nine replay/completeness failures. These outcomes remain failures.
Across all 32,412 completed callbacks, service outside recorded queue sleep
averaged 1.293 s with p90 1.341 s, versus 0.144 / 0.186 s with the default bypass.
The control had 447 requests still without a completed callback at cutoff.

`matched-admission-comparison.json` pairs 30,815 successful calls by session
ordinal, execution scope, command SHA-256, and occurrence ordinal:

| Matched callback service outside recorded queue sleep | Default 4 MiB | Bypass disabled |
| --- | ---: | ---: |
| All calls, mean / p90 | 0.126 / 0.166 s | 1.214 / 1.273 s |
| Agent Tools (23,676), mean / p90 | 0.130 / 0.187 s | 1.031 / 1.146 s |
| Backend maintenance (7,139), mean / p90 | 0.114 / 0.114 s | 1.819 / 1.933 s |

The matched mean falls 89.6% and p90 falls 87.0% with the default bypass in
these windows. This supports retaining the user-approved compromise. It is a
single comparison under evolving pressure, not replicated throughput evidence
or an isolated instruction-cost estimate: the bypass also changes reservations,
queueing, and subsequent workload progress. LOW accounting remained enabled in
both runs. The same queue-attribution limitation described above applies.

Storage maintenance used the maintained crash-cleanup preview/apply command
to remove two recognized old kernel dumps, freeing 434,772,213,760 bytes
(approximately 405 GiB). The receipts are `crash-cleanup-preview.json` and
`crash-cleanup-apply.json` in the evidence root. Cube templates, `anns` data,
the shared remote working checkout, and unrelated workloads were preserved.
