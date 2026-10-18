# c320 supplementary experiment result (2026-10-18)

## Decision

The planned c320 point is not a usable application-level ablation point on the
current Kunpeng host. No tested configuration completed one Agent within the
observation windows, and each run that reached the full offered load eventually
recorded HTTP 408 Agent failures. Therefore the Full/A-off/OS/I-off matrix was
stopped after Full, A-off, and three bounded Full calibrations. Running OS,
I-off, borrowing, or placement ablations at this point would not answer the
paper question because there is no successful Full baseline or arrival-to-
completion latency distribution to compare.

This is a negative feasibility result for the current real-trace replay on 160
CPUs, not evidence that ClawBox has no benefit in general. It also is not a
memory-capacity result: no cgroup OOM or PSI pressure was observed, and the
least restrictive runs did not activate checkpointing or borrowing.

## Host and input verification

- Host: `kunpeng`; CPUs 0-159 across NUMA nodes 0 and 1.
- Saved host setting after apply: LOCAL physical capacity 928 GiB,
  LOW/HIGH 384/448 GiB, WARM 256 GiB on NUMA node 2, borrowing limit 64 GiB,
  and common live bound 992 GiB.
- The public host-apply workflow passed its real-VM probe.
- Trace SHA256:
  `6998e1d3baf4d1b3e3a2ca127fdfef51771d05f945179b2f943bb3013d7c242c`.
- Saved Runtime and Tool template/image identities matched the selected host
  profile. LLM waits remained at the original 1:1 duration.

## Runs

Counts below use the frozen window summaries in
[`supplement-run-20261018/`](supplement-run-20261018/). GiB values are sampled
cgroup memory peaks. Unfinished offered requests remain censored; Tool
completions are progress counters, not successful Agents.

| Run | Configuration | Window | Offered / VM-created | Successful / failed | Agent-tool completions | LOCAL peak | Activated memory mechanisms | Result |
|---|---|---:|---:|---:|---:|---:|---|---|
| `supp-m320-full-20261018-01` | Full; 384/448 GiB; WARM 256 GiB; gate 8; arrivals 0.5 s | 960 s | 320 / 293 | 0 / 5 | 2,319 | 358.3 GiB | 11 borrows, 8 VM pauses, 4 restores | Timed out |
| `supp-m320-aoff-20261018-01` | A-off; otherwise matched to Full | 960 s | 320 / 289 | 0 / 20 | 4,807 | 357.9 GiB | 8 borrows, 4 VM pauses, 3 restores | Timed out |
| `supp-m320-tuned-full-20261018-01` | Full; 448/512 GiB; pool 512 GiB; gate 12; arrivals 0.5 s | 300 s | 320 / 191 | 0 / 0 | 1 | 217.5 GiB | None | Stopped: no useful progress |
| `supp-m320-paced-full-20261018-01` | Full; 448/512 GiB; pool 512 GiB; gate 8; arrivals 1.5 s | 600 s | 320 / 259 | 0 / 26 | 3,332 | 312.0 GiB | None | Stopped: 95% success became impossible |
| `supp-m320-sustainable-full-20261018-01` | Full; 448/512 GiB; pool 512 GiB; gate 8; arrivals 2.5 s | 900 s | 320 / 286 | 0 / 37 | 4,007 | 388.2 GiB | None | Stopped: 95% success became impossible |

All rows had zero memory cgroup OOM events and zero sampled memory PSI. In the
original Full run, admission wait P95/P99 was 196.23/204.65 seconds. Disabling
tool admission reduced its P90 to approximately one microsecond and roughly
doubled application Tool progress, but still produced zero successful Agents
and more HTTP 408 failures. Admission is therefore one bottleneck, but not the
only bottleneck preventing an application-level comparison.

The trace contains 24 Tool calls per Agent before its final response. Meeting
the 95% gate therefore requires at least `304 * 24 = 7,296` successful
application Tool calls, excluding VM maintenance and final validation. A-off,
the highest-progress matched arm, completed only 4,807 in 960 seconds. At its
observed average rate (5.01 calls/s), those required calls alone need about
1,457 seconds. This work-count bound explains why a different memory size
cannot meet the declared 960-second gate on the observed execution capacity.

The slower-arrival runs show the boundary more clearly. With 2.5-second
arrivals, 119 of 120 offered sessions had created a VM at 300 seconds. Once the
offered population accumulated, the run reached 37 failures and zero successes
by 900 seconds despite a 388.2 GiB LOCAL peak below its 448 GiB LOW watermark.
Changing memory thresholds, WARM size, borrowing, or creation concurrency
cannot by itself turn that execution-throughput failure into a valid memory-
policy comparison.

## Consequence for the planned matrix

- **Full:** executed at the planned point; failed the predeclared completion
  gate.
- **A-off:** executed fairly against the planned Full; it isolated a large
  admission-progress effect but did not restore Agent completion.
- **OS and I-off:** not run. Neither can supply an interpretable relative result
  when Full has zero completions. The relaxed runs also did not activate idle
  checkpointing, so I-off would be ineffective.
- **B-off and other secondary ablations:** not run because the prerequisite
  mechanism/valid-baseline gates were not met.
- **c640:** not run. c320 already failed because offered work did not drain;
  doubling requests on the same 160 CPUs would not be the plan's intended
  "c320 succeeds but lacks contention" escalation case.

The next viable experiment requires a consequential change rather than another
memory-only parameter search: increase execution capacity or reduce/change the
workload/window. Finish-first scheduling could make a small completed subset
look better but cannot satisfy 95% while the total required work exceeds the
window, so it is not a valid remedy for the declared claim. Each real remedy
changes the experimental question and must be chosen explicitly before further
formal runs.

## Cleanup and retained state

All five run states are terminal and report verified cleanup. The final remote
check found no `clawbox experiment run/cleanup` process, no QEMU process, and
the sandbox cgroup count returned to the pre-run baseline of 3,001. Logs and
window summaries were retained. The saved Kunpeng host profile remains at the
M320 384/448 GiB, WARM 256 GiB, and borrowing 64 GiB settings listed above.

## Raw audit data

The complete five-run raw dataset, per-file checksums, extracted metadata, file
layout, and metric-to-event mapping are preserved in
[`audit-20261018/`](audit-20261018/). The compressed archive is 43,586,735 bytes
with SHA-256
`53c1cd1822d70516b9305722ecac133ab9c6eb1a0cb051bf05ad746cdf2b8533`.
