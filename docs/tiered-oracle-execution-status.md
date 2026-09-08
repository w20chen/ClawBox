# Tiered oracle execution record

## First verified five-round c40 arm, 2026-09-08 07:08 UTC

`five-c40-20260908-v3` uses execution source `3ac8fdd` in
`/home/weitianc/ClawBox-experiment-five-c40-v3`. The first baseline,
`tool-static-time-oracle-reactive`, succeeded with 40/40 validated sessions,
exact execution-ID join 1.0 and zero lost telemetry events. The detached
supervisor proceeded to `tool-p90-wait-reactive` after cleanup. The remaining
12 baseline outcomes are pending; this is not a completed 13-policy report.

The complete execution-source unit suite passed 332 tests. The subsequent
read-only status-command test also passed. Inspect current state with:

```bash
cd /home/weitianc/ClawBox-experiment-five-c40-v3
.venv/bin/python scripts/study-status.py \
  /home/weitianc/clawbox-tiered-study-20260907/five-c40-20260908-v3
```

Supervisor log: `five-c40-v3-supervisor.log` under the study root. Each arm
retains five recorded rounds plus the explicit stop, c40, 160 GiB LOCAL and
the configured policy's WARM budget. A 1200-second deadline marks incomplete
arms as such; it does not certify them. The runner proceeds through all 13
policies without an agent polling it, and updates `report.md` and `summary.json`
after each arm. Final paper/full-trace results remain outside this prefix check.

## Asynchronous reclaim correction

Five-round v2 reached round 5 for all 23 initially admitted sessions, and four
sessions completed with validation true. It then exposed a second reclamation
issue: the head admission thread was still inside a 4.42 GiB `memory.reclaim`
write after LOCAL had fallen from about 143 GiB to 114 GiB. The kernel can
continue reclaim work even after there is enough memory for admission.
The run was stopped with its partial successes retained.

Only one reclaim request may now run at a time, on a separate daemon thread.
Admission continues sampling real charges rather than waiting for that syscall
to finish. Start and completion are logged; failed requests are surfaced to
admission. A regression test keeps the reclaim callback blocked after it frees
memory and verifies that admission nevertheless completes. This correction
does not increase LOCAL, relax accounting, or change policy definitions.

## Five-round v1 diagnostic follow-up

The cache fix allowed sessions to reach round 2, but host-RSS instrumentation
then dominated tool admission/completion. Live stacks showed many threads
scanning all of `/proc` every 50 ms, and completion callbacks waiting for
those scans to finish. At c40 this generated repeated slow backend-maintenance
operations and delayed real tool completion. The attempt was stopped, not
reported as a correctness pass. Its stack dump is `five-c40-stacks.txt`.

The RSS sampler now discovers matching VM processes once per execution window
and reads only those processes thereafter, while rechecking their command
lines. A fresh sampler discovers new PIDs after restore. This preserves the
host-RSS measurement and leaves clause-level guest eBPF collection untouched.
Regression coverage checks that periodic samples do not enumerate all host
processes and that unrelated reused PIDs are excluded.

## Five-round correctness sweep and cache fix, 2026-09-08

The latest user instruction replaces the full-trace performance run with a
five-round correctness sweep across all 13 baselines at c40. LOCAL remains
160 GiB, with 64 GiB WARM for the two tiered policies. The trace preserves
the first five responses and timings, followed by a labeled synthetic stop.
The later recorded pytest and coding edits are not covered; the separate
post-run regression validation remains enabled. This is not paper performance
evidence or proof that every possible policy path is bug-free.

The first full-trace arm stalled at a 4096 MiB restore admission. Live stacks
showed `configured_memory_budget` and no eligible victim. LOCAL charged
146 GiB including 35.94 GiB file cache. Admission's reserved headroom stopped
growth below `memory.max`, so kernel hard-limit reclamation was not triggered.
A cgroup-only 16 GiB reclaim request resumed restores and increased completed
operations from 42 to 63. This manually intervened run was stopped and cleaned;
its logs and `full-c40-160g-stall-stacks.txt` remain diagnostic evidence.

The fix requests up to 8 GiB of LOCAL file-cache reclamation under admission
pressure, no more often than two seconds after the prior request completes.
It re-samples real usage rather than deducting cache from accounting. Each
request records before/after charges as `local_cache_reclaim`. All 13 policies
use this same mechanism; WARM is separate and LOCAL swap is disabled. Setup
delegates the LOCAL reclaim control to the experiment user. The affected
policy, memory and CubeSandbox suites passed 43 tests on kunpeng.

The five-round sweep is detached under `five-c40-20260908-v1`; supervisor log
is `five-c40-supervisor.log`. The complete Python suite subsequently passed
330 tests (`five-c40-unit-tests-v4.log`). Two obsolete Kubernetes CLI
expectations were corrected, and the mocked OpenClaw worker test now uses
ephemeral policy/gateway ports so it can coexist with a real experiment.
The worker now binds the configured gateway port explicitly; the live study
uses the unchanged standard ports. A passing unit suite does not certify the
13 live baseline arms, whose outcomes are still pending.

## Full-trace resource revision, 2026-09-08

The user requested the full trace and a larger fair LOCAL budget. The active
64 GiB prefix pilot v4 was stopped intentionally and its owned VMs cleaned;
its evidence is retained, not considered a completed formal arm. It had
advanced tools across sessions after the admission and machine repairs.

The new matrix uses 160 GiB LOCAL/NUMA0, unchanged 64 GiB WARM/NUMA1 for the
two tiered policies, unchanged 2+4 GiB VM pairs, all 27 original trace rows,
and unchanged replay timing. There is no synthetic stop. Sizing rationale is
in `tiered-memory-simulation.md`: c4 measured 26.65 GiB, so 160 GiB retains
pressure at c40 while aiming for roughly two resident waves. The deadline is
3600 seconds per arm; around 30 minutes is an estimate, not a measured result.
The planned output is `full-c40-160g-20260908-v1` under the existing study root.

## Post-reboot recovery, 2026-09-08 06:03 UTC

The v2 and v3 attempts were stopped as infrastructure-invalid. The host had
rebooted after a fatal PCIe hardware error at 05:12 UTC (recorded in the crash
kernel log). The reboot removed the temporary LOCAL cgroup limit and WARM
mount. Restoring those settings exposed a second blocker: a new 269 GiB crash
dump had raised disk usage above CubeMaster's scheduling threshold, causing
VM creation to return `no more resource` despite abundant RAM.

Only `/var/crash/127.0.0.1-2026-09-08-05:12:52/vmcore` was deleted, under the
user's crash-dump cleanup authorization; the diagnostic `vmcore-dmesg.txt`
was preserved. Disk usage fell to 82%, with 559 GiB free. A direct Tool VM
creation and owned cleanup then passed. This hardware failure is distinct
from the source admission deadlock documented below.

The corrected matrix restarted as `short-c40-20260908-v4`, using source
`d978839` plus the runner's LOCAL/WARM preflight checks. Its supervisor log is
`/home/weitianc/clawbox-tiered-study-20260907/short-c40-v4-supervisor.log`.
The 23-round, c40, 13-policy workload and 1800-second arm limit are unchanged.
The earlier v2 launch description below is historical, not an active run.
No successful c40 comparison is certified by this restart.

## c40 continuation, 2026-09-08 05:46 UTC

Both c4 arms passed (4/4 sessions each, validation passed, exact telemetry joins
1.0, zero telemetry loss). The user subsequently requested approximately
30 minutes per baseline while retaining exploration and pytest. The selected
workload preserves original rounds 1–23 (including the successful pytest in
round 23), then supplies a labeled zero-latency stop response to round 24's
request. It is a workload-prefix study, not full task-completion evaluation.

`short-c40-20260908-v1` attempted all 13 policies but all hit their external
1800-second deadline. Twelve arms stalled around the first model step.
Creation and tool admissions shared one FIFO, allowing a capacity-blocked
creation to prevent existing sessions from running tools and releasing memory.
Separately reserving Runtime and Tool creation allowed partial pairs to consume
capacity. The lifetime-resident arm made progress and completed nine sessions;
the original top-level timeout report's 0/40 was a missing-data placeholder,
not an accurate count of partial completions. Raw events retain those results.

Commit `d978839` prioritizes progress of existing sessions ahead of new
creation, reserves complete VM pairs, and leaves tool-operation room when
admitting a new pair. The policy and worker suites passed (36 tests).
Timeout summaries now recover observed completions, memory samples and
pause/restore counts. No timed-out arm is considered a successful comparison.

The corrected detached run is `short-c40-20260908-v2`; supervisor log:
`/home/weitianc/clawbox-tiered-study-20260907/short-c40-v2-supervisor.log`.
It uses `/home/weitianc/ClawBox-experiment-short-c40-v2`, 23 recorded rounds,
c40, the same randomized order of 13 policies, and 1800 seconds per arm plus
cleanup. Source trace and failed-run evidence are preserved. Completion of
the corrected c40 study has not yet been established.

## Continuation on 2026-09-08

Physical gate `gates/tier-storage-private-copy-v5.json` passed on kunpeng with
CubeSandbox `48b3268`. Guest RAM restored as anonymous NUMA0 memory charged to
the LOCAL cgroup; WARM was separately charged on NUMA1. Both restores preserved
process state. WARM checkpoint took about 2.07 s, WARM restore 1.30 s, spill
2.23 s, and COLD restore 1.30 s. COLD cached pages were zero before restore.

The subsequent c1 static-eager arm passed all 27 model steps (371.94 s,
54 pauses/restores). The P90 tiered-time arm failed because filesystem tool
admission incorrectly required a shell-command prediction. Its `read` request
had no prediction and received HTTP 409. This caused the replay mismatch and
incomplete-execution report; the Git nonzero exit was also present in the
successful static arm and was not the cause. File tools now receive the
configured static reservation, explicitly recorded as `filesystem_static`.
Prediction/control tests passed (10 tests).

The two-policy c4 gate is running at
`raw-standalone/gate-tiered-policy-c4-v1`, with WARM limited to 5 GiB to exercise
spill. Formal c40 coverage is still 0/13. The older physical-gate status below
is superseded by v5.

## Latest verified gates, 2026-09-07 13:45 UTC

The approved healthy request reference passed strict c1 and c4 replay on
standalone CubeSandbox. All five sessions completed all 27 model steps;
benchmark validation passed, execution-ID joins were 1.0, and telemetry loss
was zero. Original recorded model responses, tool commands, and timing were
preserved; only the request-side reference was recaptured in the healthy Tool
environment, with the user's approval. Evidence under
`/home/weitianc/clawbox-tiered-study-20260907/raw-standalone/`:
`gate-healthy-c1-v1` and `gate-healthy-c4-v1`. The reference and provenance are
under `workload/healthy-reference-v1/`.

Direct WARM/restore and WARM/spill/COLD/restore process-state checks passed,
but physical placement did not: restored guest RAM retained the deleted
snapshot's file mapping and NUMA1 pages. Therefore these checks do **not**
certify WARM-to-LOCAL or release of physical WARM capacity. Evidence:
`gates/tier-storage-v1.json` and `gates/tier-storage-v2.json`. An opt-in backend
change using the existing anonymous-copy restore path is being built; it has
not yet been installed or validated. LOCAL cgroup enforcement and separate
WARM page charging are also still being established.

The restore/spill race now has an exclusive snapshot-consumer guard, also
used by cleanup. Consumers wait for an already selected spill without holding
the lifecycle lock. Deterministic tests cover exclusion, waiting, failed restore
pin release, and concurrent lifecycle restore/cleanup. The snapshot-pool and
CubeSandbox suites passed on kunpeng after this change (31 tests).

Formal c40 coverage remains **0/13**. The final backend build must pass affected
gates before the comparison is frozen. Older status sections below describe
historical attempts, not the current replay outcome.

## Standalone continuation, 2026-09-07 (in progress)

Active ClawBox: `/home/weitianc/ClawBox-experiment-8bc19e6`.
Active standalone CubeSandbox: `/home/weitianc/CubeSandbox-standalone-20260907`;
API `127.0.0.1:3000`. The Kubernetes recovery observations below are historical,
not instructions for the active deployment.

The selected full rec-a gate is still incomplete; no formal c40 outcome is
available. Standalone attempt r13 accepted 20 of 27 model requests before a
pip-network-output mismatch; artifact collection then failed and obscured that
cause in its summary. Both failures are retained under
`/home/weitianc/clawbox-tiered-study-20260907/raw-standalone/`.

The user explicitly approved ignoring the network-dependent pip diagnostic.
The comparison exception is restricted to the recorded
`timeout 12 pip install -q pytest 2>&1 | tail -2` probe and its known unavailable
package/retry diagnostics. It does not remove surrounding import failures,
change commands, fake tool output, or ignore the command exit code. Raw model
requests remain unchanged in the evidence. Search result order is normalized
while retaining filenames, line numbers and contents; generated environment
metadata is also normalized. Consequently, replay consistency is not a claim
of byte-identical output.

To audit an attempt's accepted requests against the selected trace:

```bash
.venv/bin/python scripts/audit-replay-outputs.py \
  --trace /path/to/rec-a-enriched.jsonl \
  --gateway /path/to/attempt/model-gateway/SESSION.json \
  --output /path/to/attempt/tool-output-audit.json
```

The command exits nonzero for incomplete coverage or mismatches. It separates
exact, normalized, different and missing tool results. Rejected requests are
separate gateway artifacts; a complete output audit still does not replace
final task validation, clause-level eBPF joins, or placement/tier gates.

r13 audit: 20 exact tool messages, 4 normalized, 7 not yet observed in accepted
requests. r14 accepted 21 model steps with the explicit pip exception, then
rejected the filesystem wheel search: `find / ... | head` selects ten out of
eleven available wheels in filesystem traversal order. A fresh VM check found
the recorded `/usr/share/python-wheels/pip-22.0.2-py3-none-any.whl` present;
it was eleventh in the new traversal. This is not a missing-package problem,
but neither is it byte-identical output. The user approved the exception.
It is scoped to this exact filesystem-probe command and the two competing
wheel entries; other filenames and error output remain checked. Before r15,
`scripts/validate-rec-a-listing.py` verified all 17 recorded paths on a fresh
VM from the frozen Tool template; none were missing. Evidence:
`gates/rec-a-listing-preflight.json`. Run this preflight again when changing
the template or moving the experiment to another machine.

r14 also preserved artifact stdout truncated at exactly 4,194,304 bytes.
The collector now compresses each framed artifact for transport, preserving
the decompressed bytes and end-marker validation. Its >4 MiB payload regression
passed on Kunpeng; a full live collection with that fix remains to be verified.
r15 accepted 25 of 27 model steps. Its next mismatch was the template-specific
hostname in Git's missing-author-identity error. Compressed Tool artifact
collection completed, removing the 4 MiB truncation obstruction.

The user's subsequent priority is equality of the live-inference and replay
Tool execution environments, not reproducing incidental recording defects.
An earlier uncommitted replay-only workaround removed `/dev/fd` to recreate
the recorded read/edit failures. That workaround is now removed from source
and the remote deployment; r15 and earlier attempts using it are diagnostic,
not valid final parity gates. `native_tool_bridge_setup_command()` is again
independent of inference mode. Tests exercise both replay and live driver
configuration and verify the same effective SSH launcher, tool set, envelope
and ClawTune instrumentation (20 driver tests passed on Kunpeng). This is
source-level parity evidence, not an end-to-end live-LLM certification.

r16 confirmed the incompatibility at model step 2: the live `read` operation
returns the SQL source successfully, but the frozen request reference expects
`python3: can't open file '/dev/fd/3'`. Strict matching correctly rejected that
substantive difference. No blanket output exception or replacement trace has
been introduced. Rebuilding the request-reference metadata on the healthy
shared execution environment requires an explicit study-input decision; keep
all original model responses, issued commands and recorded waits unchanged.

r15's completed artifact validation reports exact join 1.0, telemetry loss 0,
duplicate count 0, 132 native operations and 27 runtime-traced executions.
Its live tool output includes `156 passed` and `ALL CHECKS PASSED` for the
NamedTuple checks, but that does not make the incomplete replay a valid arm.
Full c1/c4 and all 13 c40 runs remain required. Raw standalone attempts and gates
are archived in `standalone-replay-through-r16.tar.gz` for local preservation.

## Latest authorized scope

The user explicitly approved rebuilding only the request-reference metadata
on the healthy Tool environment. The original trace remains unchanged.
`scripts/rebuild-replay-reference.py prepare` makes a separate diagnostic input
without request references, using the same recorded model responses and waits.
The existing gateway records the actual requests but cannot certify strict
completeness for that capture run. This is intentional: it is not a formal arm.
`finalize` requires all responses delivered exactly once, verifies them against
the originals, and replaces only request metadata. A subsequent strict c1 run
on the resulting reference must pass independently before c4/formal use.

The capture attempt is `raw-standalone/reference-capture-c1-v1`; its input and
eventual output reference are in `workload/healthy-reference-v1`. The helper's
regression verifies workload preservation, rejects changed responses, and
refuses to overwrite an existing reference. It passed on Kunpeng.

The user's later instructions supersede the original matrix sizes and repetitions:
complete c1 replay and c1/c4 correctness and tier-transition gates, then run all
13 canonical policies at c40 once. Do not run c8 or c60 or repeated formal runs.
Retain the selected rec-a recording, its responses and timing, and revised RQ2.
Download all traces and logs. Commit and push each milestone.

The main configuration is `examples/experiments/tiered-oracle-rec-a-c40.yaml`.
It offers 240 GiB of VM capacity (40 pairs, Runtime 2 GiB and Tool 4 GiB),
against 64 GiB LOCAL and 64 GiB WARM. These are configuration values, not
claims of measured consumption or validated physical enforcement. The existing
resource and placement gates remain mandatory before formal use. One run per
policy supports a descriptive case study; it cannot estimate between-run variance.
The original plan's sensitivities and capacity-matched controls remain unmeasured
unless separately executed; conclusions requiring them must be marked unsupported.

## Recovery checkpoint, 2026-09-07

ClawBox source before this update: `753de20`, pushed to its existing upstream.
Isolated remote checkout: `/home/weitianc/ClawBox-tiered-2906a91`.
CubeSandbox source: `/home/weitianc/CubeSandbox-tiered-20260907-v4`.

The CubeSandbox branch reached `094daaa` after applying the existing
`deploy/cubesandbox/hostport-hairpin.patch`. Its parent tiered implementation
had omitted this patch. The image build compiled the ARM64 BPF programs and
Cubelet; functional forwarding validation is still outstanding.

Image: `127.0.0.1:5000/clawbox/cubelet:tiered-094daaa`.
Registry digest: `sha256:977399a2e5dcca6659a2a8d4f356343d2352542bbf316358c623f1039324baa0`.
The image update was submitted, but successful node startup has not been verified.
At the latest probe, `cube-node-554rc` was `Init:0/1`, with zero of three
containers ready. SSH authentication succeeds; a simple command took about
37 seconds in the authenticated session. The cause is not established.

Complete selected-rec-a c1 replay has not passed. The historical successful c1
used a different, small smoke trace and cannot satisfy that gate. No formal
c40 results are available at this checkpoint. Existing failed attempts remain
under `/home/weitianc/clawbox-tiered-study-20260907/raw/`.

Guest address `169.254.68.6` is CubeSandbox's normal inner address, not evidence
of stale snapshot networking. Tool bridge startup and Runtime-to-Tool identity
checks must both succeed before interpreting endpoint connectivity results.

## Confirmed host blocker

At 05:35:33 UTC the host kernel reported CPU 141 stuck in a soft lockup in
`runc:[2:INIT]` PID 774692, the process associated with the replacement node's
initialization container. The stack includes `native_queued_spin_lock_slowpath`,
`mntput_no_expire`, `namespace_unlock`, `put_mnt_ns`, and `do_exit`.
At 05:46:54 UTC the same path affected `(ostnamed)` PID 785715 on CPU 250.
Earlier runc lockups are also present. This is the host kernel
`6.6.0-72.0.0.76.oe2403sp1.aarch64`, distinct from the patched guest kernel.

Containerd logs repeatedly fail to handle `TaskExit` events with
`context deadline exceeded`. A recovery helper failed before executing its
restart command: systemd timed out creating its Docker scope. Therefore the
containerd restart did **not** occur. The SSH account's `sudo -n` requires a
password; root SSH authentication is unavailable. Further pod rollouts cannot
establish a healthy experiment host in this state. Administrator host recovery
is required; a reboot may be necessary. The triggering kernel defect has not
been isolated, so recovery alone does not establish that recurrence is fixed.

The initial study evidence archive was downloaded and checked locally:
`clawbox-tiered-evidence-20260907-recovery.tar.gz`, SHA-256
`0499554c56440146d89df2811376a75c4421969d544a7deb8def594b2a83da26`.
It contains 151 archive entries. Later host-kernel and test logs are separate
artifacts. Raw evidence is kept outside public Git because request and deployment
logs may include credentials. Windows recursive SCP encountered long filenames;
use the compressed archives for complete transfer.

See [recovery and resumption commands](tiered-oracle-recovery.md).

## Validation and downloaded artifacts

The c40 configuration loaded successfully on kunpeng with exactly 13 policies,
concurrency 40, and one repetition, preserving the legacy time-oracle recipe.
The targeted remote suites `test_snapshot_pool.py`, `test_policy_v2.py`,
`test_experiments_v2.py`, and `test_policy_ssh.py` then passed against source
`753de20`. This does not satisfy integration, physical placement, or replay gates.
The first test attempt lacked pytest. The reused pip launcher targeted an old
environment; installing through the current interpreter with `python -m pip`
resolved it. Preserve both test-attempt logs.

Additional archives downloaded with matching remote/local SHA-256:

| File | SHA-256 |
| --- | --- |
| `clawbox-tiered-recording-20260907.tar.gz` | `0d69f6cf77d71c7723704884f0fecd0134a91a398fa197ea3072fdbef70e47ee` |
| `cubesandbox-tiered-094daaa-source.tar.gz` | `270ec346a9a7d2d42304b11e70b3c653cf41c1333349a66da37b4c4d7df526e1` |

The recording archive contains the original recording and resource evidence,
selected-trace directory, and prediction inputs. The source archive contains
tracked CubeSandbox `094daaa` source. The host-kernel log is also downloaded.
These artifacts reside in the local workspace's `.worktrees/` directory.

## Capacity audit after host failure

An oversized WARM request now raises `WarmSnapshotTooLarge` at victim selection,
before any pin or relocation. Previously the same request raised generic
`WarmCapacityError` because no victim prefix could satisfy it. The previous code
did not spill in this case, but incorrectly classified the permanent capacity
problem as temporary pressure. The regression checks the specific error and
unchanged manifests, pins, and spill callbacks.

Validated on kunpeng in the isolated directory
`/home/weitianc/clawbox-capacity-validation-20260907-icc9id`: the new regression
failed against the prior implementation, then the snapshot-pool and policy
suites passed with the fix. Raw `evidence/before.log` and `evidence/after.log`
are retained there. No live deployment was changed during this audit.

Further code review identified unresolved integration requirements. The worker's
NUMA sampler measures a node-wide baseline-subtracted delta; it does not enforce
the experiment's LOCAL limit. The lifecycle restore path calls connect before
removing its WARM manifest and has no restore pin around that call. A concurrent
spill selection can therefore pin the generation while restore is in progress;
this needs a deterministic race regression and a lifecycle fix. Backing-file
release/placement after connect also needs backend evidence before removing its
WARM charge. Passing the small pure-policy tests does not establish these
properties. Host recovery is necessary but not sufficient for formal readiness.
