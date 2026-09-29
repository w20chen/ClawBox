# ClawBox experiment handoff

Updated: 2026-09-29

## Current status

The supported standalone `clawbox experiment` workflow is working on `ssh kunpeng`.
The final one-repetition c16 evaluation completed successfully for all three policies.
Every arm completed 16/16 sessions, passed workload validation, reported zero OOM kills
and zero telemetry loss, joined native Tool telemetry at 100%, and verified cleanup.

Use this run for results:

- Run ID: `eval-tiered-c16-r2-final6-20260929`
- Result root: `/home/weitianc/clawbox-lab/sqlglot-4528/results-tiered/eval-tiered-c16-r2-final6-20260929`
- Spec: `/home/weitianc/clawbox-lab/sqlglot-4528/current-eval-tiered-r2.yaml`
- Qualification receipt: `/home/weitianc/clawbox-lab/sqlglot-4528/eval-tiered-r2-qualification.json`
- Installed command: `/home/weitianc/miniconda3/bin/clawbox`
- Durable remote ClawBox source: `/home/weitianc/clawbox-stack/ClawBox`
- Durable remote ClawTune source: `/home/weitianc/clawbox-stack/ClawTune`
- Active host profile: `/home/weitianc/.config/clawbox/host.json`

Do not use `final2`, `final3`, `final4`, or `final5` for performance claims. They were
diagnostic runs made before the final concurrency, liveness, and Runtime-image fixes.

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

## Artifacts

Under the final result root:

- `report.md`: concise comparison table and validity statement
- `summary.json`: complete per-arm metrics and timelines
- `summary.csv`: arm status and duration
- `memory-timeseries.csv`: whole-system LOCAL, shared live-borrow, WARM, and host-memory timeline
- `arms/<arm-id>.json`: complete result for each policy
- `run-state.json`: supervisor state; all three arms are `succeeded`
- `qualification.json`: copied qualification receipt

Regenerate the report without rerunning workloads:

```bash
clawbox --output-root /home/weitianc/clawbox-lab/sqlglot-4528/results-tiered \
  experiment report eval-tiered-c16-r2-final6-20260929
```

## Implemented reliability fixes

- OpenClaw transport timeout is separate from Tool execution timeout. Admission waiting is
  transparent to OpenClaw; the Tool deadline begins only after `ADMIT`.
- A true Tool timeout cancels the remote execution and returns exit code 124.
- Broken policy-response connections roll back reservations and lifecycle state.
- Tool completion can progress while another same-session request waits for admission.
- At the just-below-HIGH idle boundary, exactly one existing-session progress request may
  advance toward HARD; a second request remains blocked. This removes deadlock without
  erasing the A-versus-B reservation distinction.
- The Runtime image contains the current SSH shim. The shim removes the deferred-timeout
  marker before command identity and P50 lookup, while preserving the post-admission
  execution deadline.

The complete local and kunpeng test suites passed after the source fixes. The final
combined HIGH-boundary regression also passed on both machines. `git diff --check` passes.

## Self-service workflow

The public workflow is documented in `docs/lab.md` and should be used instead of manually
editing cgroups, NUMA placement, templates, or arm matrices. On `kunpeng`, setup and the
first image build are already complete, so routine work is only configure plus launch:

```bash
clawbox experiment configure BASE.yaml SPEC.yaml ... \
  --launch --run-id RUN_ID --detach

# Or launch an existing spec. This automatically validates, checks the host,
# refreshes snapshot storage, qualifies if needed, and starts or resumes.
clawbox --output-root RESULTS experiment launch SPEC.yaml \
  --run-id RUN_ID --detach

clawbox --output-root RESULTS experiment status RUN_ID
clawbox --output-root RESULTS experiment report RUN_ID
```

The active profile now records 28/32/36 GiB LOCAL watermarks, a 128 GiB NUMA1 shared
pool, a 64 GiB live-borrow cap, both immutable template/image records, and the saved
non-secret image-build inputs. `clawbox experiment images` therefore needs no repeated
path arguments after the next ClawTune source update.

The self-service path was verified on `kunpeng`: setup completed with a real VM probe;
after restarting CubeMaster, `doctor --probe-vm` refreshed snapshot storage and completed
the first VM create/execute/destroy cycle; a repeated successful `launch` returned the
existing report in 1.484 seconds without qualification or workload replay. The relevant
local and remote test selection passed 88 tests.

## Remaining limitations

- This is one repetition, as requested. It demonstrates the mechanism and a large C
  effect, but does not estimate run-to-run variance or confidence intervals.
- B's command-level prediction-error metrics are unavailable as described above.
- Working-tree changes are not committed. Review and commit only the supported workflow,
  tests, scripts, and user-facing documentation; do not include experiment results or
  temporary verification directories.
