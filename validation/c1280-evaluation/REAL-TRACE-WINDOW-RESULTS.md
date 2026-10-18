# Real-trace memory-budget screens

These are observed finite-window results, not completed c640 batches or proof
of Pareto optimality. All raw events are retained remotely under the study's
`results/` directory. No workload code was changed for this comparison.

## Earlier retained screens

| Full configuration | Window seconds | Successful Agents | Agent-tool / backend executions | LOCAL peak GiB | Admitted-operation wait P99 seconds |
|---|---:|---:|---:|---:|---:|
| LOW/HIGH 512/640 GiB, scale 1.0 | 1,199.95 | 0 | 3,183 / 8,852 | 483.72 | 318.38 |
| LOW/HIGH 768/896 GiB, scale 1.0 | 599.72 | 0 | 3,216 / 8,581 | 356.42 | 1.73 |
| LOW/HIGH 512/640 GiB, scale 0.75 | 1,199.97 | 0 | 4,681 / 12,924 | 510.07 | 39.91 |

The 896-GiB run was stopped during the arrival ramp; its eventual peak and
completion behavior are unknown. It is not evidence that the fully offered
workload would remain below the watermark. The scale=1.0 run later failed
during finalization, so its raw-window measurements remain diagnostic. The
scale=0.75 screen completed more operations within the matched window, but all
these windows had zero successful Agent throughput. Operation throughput must
not be substituted for the primary metric.

## Matched 1,200-second comparison

Common settings: sqlglot-4528 replay, recorded LLM waits at 1:1, target 640
Agents, arrivals every 2 seconds, 160 compute CPUs on NUMA 0/1, configured LOCAL
capacity 928 GiB, LOW/HIGH 512/640 GiB, WARM capacity 384 GiB, borrowing cap
192 GiB, Runtime/Tool guest sizes 2/4 GiB, VM creation concurrency 8.
Prediction reservation scale is 0.75. Snapshot mechanism is incremental COW;
idle checkpoint scope includes both Runtime and Tool. No SSD tier.

| Observation | Full | A-off |
|---|---:|---:|
| Run | `real-trace-p0-c640-balanced-full-s075-01` | `real-trace-p0-c640-balanced-aoff-s075-01` |
| Final envelope / cleanup verified | timed_out / true | timed_out / true |
| Window, seconds | 1,199.97 | 1,199.98 |
| Target Agents | 640 | 640 |
| Requests scheduled in window | 600 | 600 |
| Sessions with at least one VM created | 363 | 421 |
| Successful Agents | 0 | 0 |
| Successful Agents/s | 0 | 0 |
| Recorded independent session failures | 33 | 83 |
| Completed agent-tool executions | 4,681 | 4,873 |
| Completed backend-maintenance executions | 12,924 | 14,291 |
| LOCAL sampled peak, GiB | 510.07 | 535.18 |
| Shared live sampled peak, GiB | 1.01 | 3.75 |
| WARM snapshot sampled peak, GiB | 0 | 0 |
| Borrow / return events | 2 / 1 | 6 / 0 |
| Borrow service times, seconds | 65.03, 20.48 | 21.77, 38.00, 6.08, 6.18, 3.08, 2.74 |
| Return service time, seconds | 61.50 | none in window |
| Pause / restore events | 0 / 0 | 0 / 0 |
| OOM / OOM-kill event deltas | 0 / 0 | 0 / 0 |
| Maximum sampled memory PSI avg10, some / full | 0 / 0 | 0 / 0 |
| Completed-admission wait P50/P95/P99, seconds | 0.0053 / 7.21 / 39.91 | 0.00000086 / 0.00000116 / 0.00000140 |
| Maximum completed-admission wait, seconds | 177.90 | 0.00003315 |

Full cannot establish a throughput benefit: there were no successful Agents
inside this window. A-off also completed zero Agents while retaining placement, checkpointing,
borrowing and runtime control; it recorded more independent failures (83 vs 33).
It is not the all-mechanisms-off OS-default baseline.

Borrowing activation is not evidence of benefit. Its two measured service
times were 65.03 and 20.48 seconds and the return took 61.50 seconds; the
configured estimate for each 4-GiB borrow was approximately 0.267 seconds.
These observations show that the action cost model substantially underestimated
service time under this load. They do not by themselves attribute the whole
Agent slowdown to borrowing. The two observed LOCAL residency reductions were
about 193.4 and 397.1 MiB; each operation reserved 4 GiB of shared capacity.
Distinguish those actual deltas from configured guest memory and reservations.

The final request is scheduled at 1,278 seconds, outside this window. Unfinished
requests are censored; successful-completion P95/P99 are unavailable for Full.
Admission quantiles include only requests that were admitted, excluding still
waiting requests. Agent-tool execution events may include operations making
up a higher-level recorded tool call; do not equate event counts with trace
span counts. Session failure examples are HTTP 408; no claim of OOM causation
is justified from those errors.

The Full window contains 311 memory/CPU samples. Their CPU usage had a
sample mean of 48.04 cores and a maximum of 87.89 cores out of 160; the
mean is not time-weighted. The largest sample gap was 104.98 seconds, so
reported memory peaks are sampled lower bounds, not guaranteed true maxima.
Cumulative cgroup memory PSI increased by only 199/174 microseconds
(some/full) across these samples. This point does not demonstrate Linux
memory thrashing. See `balanced-full-window-extra.json`.

Source: `balanced-full-window-final.json`, generated from timestamped raw events
using `summarize-window.py`. Run envelope and cleanup status are recorded
separately from window metrics; post-window cleanup operations are excluded
from this table. The timeout envelope reports all 640 target sessions as failed
to complete; it is not a count of observed independent exceptions. There were
33 such exceptions within the observation window. Final cleanup was verified;
after the worker exited, 40 remaining run-owned cgroups were killed with a
saved ownership witness and PID list in `post-window-cgroup-cleanup.json`.

Both arms used the forecast-gated idle policy documented in
`IDLE-CHECKPOINT-REVIEW.md`. Their zero-checkpoint result cannot establish
the effectiveness of actual-idle checkpointing. OS-default completed its observation window; its results are below.
The A-off worker exited before run-owned cleanup assistance; 525 remaining
run-owned cgroups were killed with a saved witness, and public cleanup verified.


## Completed OS-default screen

Run `real-trace-p0-c640-balanced-os-s075-01` used the same arrival schedule,
trace, hardware and 1,200-second observation window, with all ClawBox policies
off. The worker exceeded its finalization envelope; cleanup was verified after
164 remaining run-owned cgroups were killed using the saved ownership witness.
Public abort marks the administrative run state cancelled; this does not turn
the failed measurement envelope into a successful run.

| Observation | OS-default |
|---|---:|
| Observed window, seconds | 1,198.84 |
| Requests scheduled / target | 600 / 640 |
| Sessions with at least one VM created | 598 |
| Successful Agents / Agents per second | 0 / 0 |
| Independent session failures in window | 183 |
| Agent-tool / backend-maintenance completions | 6,481 / 17,114 |
| LOCAL sampled peak, GiB | 580.23 |
| Pause / restore / borrow events | 0 / 0 / 0 |
| OOM / OOM-kill deltas | 0 / 0 |
| Maximum memory PSI avg10, some / full | 0 / 0 |
| Completed-admission wait P50/P95/P99, seconds | 0.00000080 / 0.00000145 / 0.00000221 |

All three matched screens have zero successful Agent throughput. The lower
failure count under Full is not sufficient evidence of better throughput,
and the OS failures include Cube creation/envd initialization timeouts. This
comparison does not establish an OOM-prevention or memory-thrashing benefit.
Raw events and metadata: `raw-balanced-os.tgz`, SHA256
`8959152a93220ca590ea2595c7d0b176da480286d02845a6332fa8634d7ca98e`.
Machine-readable summary: `balanced-os-window-final.json`. Full model-gateway
and per-session trace artifacts also remain in the remote run directory.
