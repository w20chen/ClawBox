# Full-1280 admission and finalization optimization

Measured 2026-10-28, Asia/Shanghai. This is an internal evidence report.

The c1280 objective is met for the aggregate Tool path. Excluding capacity and
FIFO condition sleep, 24,355 Tool preparations average 72.8 ms with p90 83.4
ms. The observed fully successful response-to-result cohort averages 6.36
seconds with p90 7.64 seconds; all 26 retained native-validation scopes average
5.88 seconds with p90 8.03 seconds. The baseline values were 5.691 seconds mean
/ 13.634 seconds p90 for Tool preparation and 146.648 seconds mean for
response-to-completion.

The result deliberately uses the configured low-memory compromise: predictions
at or below 4 MiB remain active and measured but do not enter the memory
admission ledger. The option is disabled by default.

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

Thus the requested overall p90 is below one hundred milliseconds, but the rare
accounted pressure path is not solved to that standard. Its remaining exclusive
implementation time is mainly lock waiting, exact claim sampling, and physical
sampling. It no longer limits the common path; raising the bypass above 4 MiB
would reduce it further at a larger unaccounted-memory risk and was not done.

The 4 MiB threshold is an explicit policy tradeoff. One simultaneous bypassed
call in each of 1,280 sessions omits at most 5 GiB, about 0.52% of this run's
983,040 MiB policy budget; multiple simultaneous calls scale that exposure.
`mechanisms.admission.small_tool_admission_bypass_mib` defaults to zero and the
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
