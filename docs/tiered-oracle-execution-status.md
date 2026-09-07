# Tiered oracle execution record

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
r15 is running with these two fixes. Gateway/native-artifact regressions passed
on Kunpeng (24 tests). Full c1/c4 and all 13 c40 runs remain required.

## Latest authorized scope

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
