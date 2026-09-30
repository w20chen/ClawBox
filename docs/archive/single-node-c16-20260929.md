# Historical single-node c16 result (2026-09-29)

This record describes one past installation and its experiment artifacts. Its
NUMA 0 compute / NUMA 1 shared-pool settings, commands, paths and host status
are not the current two-compute-node configuration. For a new run, start with
[self-service](../self-service.md) and [supernode configuration](../supernode.md).

## Recorded run

The run ID was `eval-tiered-c16-r2-final6-20260929` on Kunpeng.
The final one-repetition c16 evaluation completed successfully for all three policies.
Every arm completed 16/16 sessions, passed workload validation, reported zero OOM kills
and zero telemetry loss, joined native Tool telemetry at 100%, and verified cleanup.

## Final experiment configuration

- Workload: deterministic managed replay of `tobymao__sqlglot-4528`
- Offered concurrency: 16 agents, each with one 2 GiB Runtime VM and one 4 GiB Tool VM
- Advertised/configured VM capacity: 96 GiB
- LOCAL LOW/HIGH/HARD: 28/32/36 GiB
- WARM/shared NUMA1 pool: 128 GiB
- Maximum live NUMA1 borrow: 64 GiB
- Combined LOCAL plus live-borrow cgroup limit: 100 GiB
- Arrival: burst; repetitions: one
- A static Tool reservation: 259 MiB, calibrated training P90
- B/C frozen P50 reservations: 21.8867 to 225 MiB across 20 command entries
- Snapshot mechanism: WARM-only incremental CoW
- Runtime image: `sha256:aefbc09b9d7b76a23284aa04a258cdd1a9057586742da2926bcd2922faf0dd40`
- Tool image: `sha256:98e10fcc9444e4d29cc4a154237800619396a9ef31ae43d1a81174fc84244deb`

The Tool digest above is recorded exactly in the final result and qualification artifacts;
use those artifacts as the authority if copying the digest into another configuration.

## Final results

| Policy | Duration | Agents/min | JCT p50 / p95 | Admission blocked | LOCAL peak | HIGH exposure | Borrow | Migration |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A: `tool-static-resident` | 1757.29 s | 0.577 | 1656.01 / 1661.07 s | 13269.56 s | 32.10 GiB | 3.86 s / 0.32 GiB-s | 1, 3.97 s | none |
| A+B: `tool-p50-resident` | 1693.34 s | 0.600 | 1553.63 / 1587.85 s | 12651.03 s | 33.16 GiB | 284.41 s / 113.16 GiB-s | none | none |
| A+B+C: `tool-p50-wait-reactive` | 1019.86 s | 1.035 | 898.11 / 922.08 s | 120.38 s | 28.68 GiB | 0 s / 0 GiB-s | none | 16 pause + 16 restore |

Derived comparisons:

- B versus A: duration -3.6%, agents/min +3.9%, JCT p50 -6.2%, admission wait -4.7%.
- B+C versus A: duration -42.0%, agents/min +79.3%, JCT p50 -45.8%, admission wait -99.1%.
- B+C versus B: duration -39.8%, agents/min +72.6%, JCT p50 -42.2%, admission wait -99.0%.

B materially reduced reservation waste: the mean reservation/actual ratio fell from
19.85x for A to 3.70x. Its end-to-end improvement is modest because resident Runtime and
Tool memory, rather than command reservation alone, dominates the high-pressure tail.
B also spent longer above HIGH, but remained below HARD and completed without OOM; this
is a valid transient overshoot, not a failed run.

C produced the main system result. Admission pressure selected model-waiting, response-not-
ready, no-active-Tool VM pairs in LRU order. It checkpointed eight Runtime+Tool pairs
(16 pauses), restored all of them on demand (16 restores), and every restored session
continued. Pause service time was 26.33 s, restore service time was 11.81 s, WARM transfer
and peak committed size were 48 GiB, and shared live-plus-WARM peak was 48.46 GiB. This
kept measured LOCAL below HIGH, so the final C arm had zero actual HIGH crossings and did
not need live borrowing.

The whole-host peak delta for C was 71.19 GiB, higher than A/B, because the node-level
metric includes WARM tmpfs snapshots. This is expected: C moves state from LOCAL to NUMA1;
it does not claim that snapshot bytes disappear. LOCAL physical use, WARM bytes, live
borrow, and whole-host use must be reported separately.

## Prediction interpretation

The frozen P50 policy successfully resolved all managed replay commands. There were 320
frozen P50 Tool admissions and 512 fixed filesystem/backend admissions in each B/C arm.
The explicit-timeout command that previously failed now resolves under its original hash
and receives a 21.8867 MiB reservation.

Do not claim measured command-level prediction accuracy from this run. The final report's
prediction coverage/error fields are `n/a` because no prediction-error observations were
emitted, even though the frozen predictions were used for admission. The supported claim
is reduced reservation waste and the measured end-to-end behavior above, not a quantified
MAE or exceedance rate. A future accuracy study must add prediction-versus-observed labels
to the formal result before making that claim.

## Limits of this result

This is one repetition and does not estimate run-to-run uncertainty. The
command-level prediction-error metrics in the report are unavailable; see the
interpretation above. The result belongs to the historical single-compute-node
topology and should not be compared directly with two-node runs.
