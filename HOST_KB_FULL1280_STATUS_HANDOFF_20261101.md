# Host KB and Full-1280 continuation handoff

Continuation resumed at the user's request on 2026-11-01. Updated after the real pair-learning gate, approximately 21:50 Asia/Shanghai. Full-1280 acceptance remains incomplete.
This is an operational handoff; the requirements remain in
`HOST_KB_REDESIGN_HANDOFF_20261030.md`.

## Current judgment

**Full-1280 has not passed.** The host-KB implementation and template refresh
are substantially complete, but the large-run acceptance and final measurements
are incomplete. Do not describe the redesign as fully verified.

The latest Full run is terminal **failed**, with cleanup verified. No replacement
Full run has been launched. Capacity and pair-learning diagnostics are now terminal.
The user requested a plan and execution through completion, with careful gates to
avoid repeated Full runs. Preserve 160 CPUs and 1,000 GiB across NUMA0/1.

## Latest continuation results (supersede earlier interim notes)

- The four 8 GiB cases completed. Final validators passed, but 3477 had guest
  OOM, 3024 had one extra segfault, and 3057/5407 had consequential package-install
  differences. Saved detail: `capacity-checks/divergence-detail-1793537534768321244.json`.
  Some recorded timeouts were misclassified because importer advice followed
  the exit marker; commit `20aa2fa` corrects that parser without weakening comparisons.
- The 16 GiB 3477 candidate completed all 115 calls with no replay divergences;
  cleanup passed. Use `tpl-4fb84c3c9cb347f98b28d59e`, unchanged image/two vCPUs.
  Evidence: `capacity-checks/pennylane-3477-16g/final-audit-1793538344187811938.json`.
  No OOM/worker-crash markers were found in its actual outputs; sampled dmesg
  is not continuous proof. Do not repeat this trajectory.
- Native commit `6811a0d` writes large contiguous disk-backed COLD ranges in
  16 MiB batches with the existing sync/cache-discard helper. WARM and snapshot
  format/mechanism are unchanged. Native source is `native-stream-v1/CubeSandbox`
  below the remote project home; installed binaries/provenance are in
  `host/cold-stream-v2/`, backups `/data/clawbox-backend-backups/cold-stream-v2-20261101`.
  Prepared source repeat-check and release build passed. Bounded pressure writing
  preserved SHA256 and reduced direct reclaim; real first/incremental snapshots
  preserved a live 128 MiB buffer, nonce and PID through restore, with cleanup:
  `checkpoint-pressure/vm-pressure-streaming-healthy-365c11635a/result.json`.
  These checks do not prove Full-1280 pressure acceptance.
- Real two-pair testing found and corrected two learning defects: the query used
  the seed memory domain after receiving guest memory (`20aa2fa`), and observations
  described the external launcher instead of payload commands (`5af9163`). The
  standard injected SSH shim now uses builtin environment setup; no guest image
  rebuild. Linux shell-equivalence and host tests passed (40 tests).
  `pair-learning-launcher-fixed-20261101`, attempt `attempt-6b334be4752e4aa3`,
  succeeded with all eight observations accepted and cleanup. The fourth memory
  prediction was 74.51 MiB for A and 12.12 MiB for B after different measured
  allocations. Real-journal replay proved every update leaves the other pair's
  predictions unchanged: `pair-learning-gate/isolation-journal-replay-1793540811369778052.json`.
- Commit `ae55222` bumps the maintained qualification contract to 16 for the
  changed learning/execution integration. Do not reuse an old full receipt as
  current execution proof. Reuse unchanged physical-template sharing evidence.
  ClawTune is still clean/pinned at `4a6d0af66e76b377bfc957baf84a5f414c6d8bf5`.
- Storage preflight found the native CoW directory on the root filesystem,
  distinct from the custom COLD mount. Thirty-two old immutable distribution-cache
  directories were copied to the existing spill filesystem, SHA256 checked,
  symlinked at their original paths, then only verified source duplicates removed.
  Preserve `host/cache-relocation-plan-1793539739976515256{.json,.applied.jsonl}`.
  Root margin before Cube's 85% threshold is now about 448.9 GB versus the
  conservative 386.5 GB Full requirement. No VM/template storage or anns data removed.
- Package preflight in progress: `host/check-capacity-packages.py` verifies the
  16 PennyLane wheel URLs seen in saved outputs against index SHA256 values.
  Evidence `network-check/capacity-package-integrity-1793540768324400323.jsonl`.
  The 3057 failure was a truncated 0.35.0 wheel download; 5407 timed out during
  dependency resolution/download. A two-install-only CLI gate is prepared at
  `package-install-gate/experiment.yaml`; do not repeat their long trajectories.
- Remaining: finish package gate; freeze corrected workload/shapes, sharing proof
  and exact current qualification; launch one Full-1280; audit section 21 metrics.
  Isolated clean-machine install and actual host-reboot recovery remain unproven;
  do not reboot/reinstall the shared host to claim them.

## Continuation milestones

The following are earlier interim notes; use the latest results above for status.

- Restore-route real-VM gate passed with cleanup:
  `restore-route-gate/restore-admit-58353ac4dbd4/result.json`. The restored Tool
  changed port 23703 to 23705; concurrent admission reached its identity while
  the first callback waited for memory. The earlier probe used the wrong SSH
  user (`root` instead of `executor`). Explicitly preserve `PYTHONPATH` through
  sudo; `sudo -E` alone selected the old editable checkout in this environment.
- The 8 GiB four-case replay is still active. Cases 3024, 3477 and 3057 have
  completed; 5407 remains active. Final validators passed for completed cases,
  but do not imply command parity or absence of guest OOM. Case 3024 completed with its final
  validator passing, but a replayed pytest invocation segfaulted (139), so this
  is not complete trajectory parity. Case 3477 has multiple confirmed guest OOM
  kills at 8 GiB. Preserve `capacity-checks/guest-processes-1793533530872820830.json`.
- A 16 GiB candidate for 3477 was registered from the same immutable image,
  two vCPUs unchanged: `tpl-4fb84c3c9cb347f98b28d59e`. Its input, provenance and
  one-case specification are in `capacity-checks/pennylane-3477-16g/`.
  Sharing/qualification correctly refused while the existing replay populated
  the physical VM domain. A bounded continuation helper now waits for the
  existing run's terminal result and verified cleanup, then performs the normal
  sharing proof and `experiment launch` for this candidate:
  `host/launch-capacity16g-after-current-v2.sh`, log
  `host/capacity16g-wait-launch-v2.log`, launcher PID 1868860 (recheck identity).
  Planned unique run ID: `capacity-c1-pl3477-16g-20261101t1158z`, under
  `capacity-checks/pennylane-3477-16g/runs`. It uses the frozen source
  `/home/weitianc/clawbox-hostkb-20261101/source/ClawBox-client-timing-cd7f27a`
  with explicit `CLAWBOX_REVISION=cd7f27ab6717a5ae07b26b076ac0ab9c1be827c4`.
  Do not launch a duplicate helper or reuse the original four-case run ID.
- Native captures quantify NUMA1 at approximately 503.382 GiB total and
  8.016-8.071 GiB free, with about 34 GiB slab/page tables/kernel stacks outside
  the zero-valued VM kernel counters. Evidence:
  `host/checkpoint-node-headroom-1793533442449561068.json`.
  The user explicitly requires retaining **160 CPUs and approximately 1 TiB
  across the two compute NUMA nodes**: NUMA0 CPUs 0-79 and NUMA1 CPUs 80-159,
  configured local capacity 500 GiB each (1,000 GiB total). Keep the existing
  per-node low/high watermarks 448/480 GiB and the existing shared pool.
  Resolve reclaim, physical headroom and checkpoint behavior within this
  resource envelope; do not silently reduce the experiment budget. The 448 GiB capacity / 400/432 GiB watermark
  proposal is **not approved, not applied, and not a proven timeout fix**.
  The 33.90 GiB figure is node-wide kernel occupancy, including 19.96 GiB
  reclaimable slab; it is not demonstrated unavoidable ClawBox overhead.
  A later allocator capture (`host/node1-slab-breakdown-1793535228228496338.json`)
  identifies 18.70 GiB in XFS inode cache and 4.87 GiB in generic 1/2 KiB
  allocation caches. Their historical per-project ownership is unproven.
  Investigate reclaim and physical per-node headroom before selecting a lower
  budget. `NumaCgroupMemorySampler.current()` returns per-node VM charges but
  whole-host MemAvailable; it does not establish physical headroom on that node. The syntax-validated budget-only
  draft is `input-corrections/numa448-proposal/experiment-budget-only-draft.yaml`;
  retain it as an unapproved historical proposal; do not use it for the next Full run.
- Implementation milestone `cd7f27a` is committed and pushed. The per-run
  Runtime launcher now records compact client prediction timings
  without an image rebuild. Windows and Linux each passed 56 focused tests.
  Real Runtime-to-host success, repeated prediction, rejected credentials and
  cleanup passed: `host/client-timing-5b562f09bc82/result.json`.
- Additional failed-Full audit:
  `host/failed-full-additional-audit-1793534003424045416.json` records output size,
  operation CPU attribution and listener queues. These remain failed-run data.
  Isolated committed-KB recovery has completed:
  `host/isolated-kb-replay-40caddd64b28/summary.json`. All 1,034 scopes refer to
  one shared seed; 12,913 generations were reconstructed. Retained RSS increased
  24.536 GiB over the import baseline (seed-load increment 60.059 MiB).
  This excludes live prediction/receipt caches, HTTP buffers and concurrent
  update transients; it is not total live KB RSS or a Full latency measurement.
  The failed run had listener backlog in 227/338 samples, maximum 85 queued
  connections. Complete continuation audit: `host/hostkb-continuation-audit-20261101t1205z.json`.

## Repositories and deployed code

- Local ClawBox: `C:\Users\29068\Desktop\ClawBox`, branch `main`.
- Latest implementation milestone: `8483f31`, committed and pushed. It sums
  multiple mappings of the same RAM file when checking template sharing.
  Windows: 30 tests passed, four POSIX tests skipped. Kunpeng: 34 tests passed.
  Four real 8 GiB templates subsequently passed sharing verification.
- Restore-route race fix: `35bccb8`, committed and pushed. It invalidates the
  pre-checkpoint SSH route and prepares the restored bridge **before** releasing
  the lifecycle lock to wait for memory. Otherwise a concurrent admission could
  use the old port. Focused Windows tests: 120 passed, two skipped; Linux: 122 passed.
- Latest failed Full used `ac7bc09`; neither fix was injected into its process.
- ClawTune remains unchanged at
  `4a6d0af66e76b377bfc957baf84a5f414c6d8bf5`; local worktree checked clean.
  Retain its explicitly marked imputed training targets.
- Prior mutable remote staged source (preserved; use the frozen source above for the queued candidate):
  `/home/weitianc/clawbox-hostkb-20261101/source/ClawBox-restore-route-f1063e0`.
  Its name predates the later worker and qualification fixes; those files were
  copied in and verified. The editable venv can reference an older source:
  use this checkout's `scripts/clawbox`, or explicitly set `PYTHONPATH`.
- Frozen failed-run source:
  `/home/weitianc/clawbox-hostkb-20261101/source/ClawBox-full1280-ac7bc09`.
  Preserve it and every failed run's frozen inputs and evidence.

Unless stated otherwise, paths below are relative to:

```text
/data/full1280-hostkb-pagemap-bit61-20261101
```

## Completed work to retain

1. Host-only mutable KBs, one shared canonical seed and private pair overlays;
   host prediction/observation, receipt admission, durable idempotent learning,
   and current upstream imputation semantics are implemented.
2. Runtime and 503 selected Tool templates were refreshed once.
   `qualification-v9/qualification.json` passed all 503. **Do not rebuild them
   again for host-only changes.**
3. Real-VM same-attempt recovery retained completed/interrupted journals;
   untouched queued traces continued. An interrupted Tool call remains explicitly
   incomplete. Explicit cold attempts used independent IDs/journals and equal
   initial predictions. See `recovery-gate/same-attempt-verification.json` and
   `recovery-gate/cold-attempt-verification.json`.
4. `recovery-gate/live-vm-filesystem-inspection.json` verified no writable VM KB
   or raw clause telemetry in the inspected state locations.
5. Package networking works. A fresh real Tool VM passed APT update/install,
   pip install/import, repeat update and cleanup again during this handoff.
   Evidence: `network-check/recheck-*/package-network-*.json`.

The canonical seed SHA256 is
`9a2ce8f1adec6401af5a41924376291e3b26c0ec24f8548e245184e68d4b1aa6`.

## Latest Full failure

| Item | Value |
|---|---|
| Run | `full-c1280-hostkb-network-20261101` |
| Attempt | `attempt-ff7f2ba60f2d4dbc` |
| Arm | `13eefa6530ff5eedc6b6217f` |
| Terminal state | Failed; cleanup verified; worker and supervisor exited |
| Progress before abort | 1,034 pairs ready; 159 successful session measurements; 91,941 completed Tool calls |
| Fatal event | Native pause HTTP 408 at 10:56:03 UTC |
| Terminal audit | `host/network-terminal-audit.json` |

There were four genuine guest OOMs at 3 GiB: PennyLane cases 3024, 3477, 3057
and 5407. Host cgroup OOM counters being zero does not disprove guest OOM.
There were also three stale-route exit-255 failures before abort, addressed by
`35bccb8`. Later abort cascades must not all be counted as independent defects.

The `andrewgodwin__urlman-12` validator had a proven false negative: the original
and current patches were identical and worked, but its predicate demanded a
literal string neither contained. Preserve the failed verdict and frozen input.
Use the separately corrected manifest named in
`input-corrections/urlman-validation-evidence.json` for a future run; do not
weaken general validation or rerun 503 template checks for this predicate.

### Checkpoint failure still unresolved

The two timed-out native requests were:

- `0f724370-e9bc-4068-a0ff-f0b0a1aa6b0d`, Runtime
  `6539baa9e6dc4f8cb5e614dc241e4790`.
- `68cf136c-e182-4bbb-9849-1a160cd37231`, Runtime
  `bcacd0ad0c3d48469fbb9344c87c64ee`.

The policy event's session 0848 was the trigger, not the identity of these
checkpoint victims. Both involved first-generation COLD Runtime snapshots.
The CubeAPI lifecycle route has a hardcoded 120-second timeout; the client was
configured for 130 seconds. The later missing-file errors followed timeout
cleanup and are not established as the original cause.

Captures at 10/30/60 seconds show continuing writes, running VMM threads and
severe memory stalls: leaf memory PSI full reached approximately 44% and 87%.
NUMA1 was nearly full despite room in the configured VM budget. Kernel memory
accounting fields in the VM cgroup were zero while host node statistics showed
substantial slab/page-table use. This is evidence to investigate actual node
headroom; it does **not** yet prove one exact native bottleneck or a tested remedy.
Do not simply extend the timeout or claim the stall fixed.

Evidence:

- `host/network-slow-checkpoint-phases.json`
- `host/network-failed-checkpoint-progress.json`
- `host/network-failed-checkpoint-memory-context.json`
- `host/checkpoint-watch-full-c1280-hostkb-network-20261101/`

CPU-clock perf sampling was unsupported on this kernel; no valid CPU profile
was captured. Native source is
`/data/clawbox-sourceprep-pagemap-20261101T0220Z/CubeSandbox`.
No new native change or memory-policy configuration change was deployed during
this handoff. Preserve the existing native optimizations and snapshot mechanism.

## Four-case capacity diagnostic: inspect first

Four 8 GiB Tool templates were registered from the **same immutable images**,
with two vCPUs unchanged. Images were not rebuilt:

| PennyLane case | New template |
|---|---|
| 3024 | `tpl-6d5fec9406544c1e8ea94b0f` |
| 3477 | `tpl-f97bd907ff01481c979ff29b` |
| 3057 | `tpl-7bb1bda009ff4137b6e8ff0c` |
| 5407 | `tpl-d165c1b785dc4b3784d65782` |

8 GiB is a diagnostic ceiling, **not** measured required memory or a proven
sufficient final capacity. At the final sample, the 3477 guest had only about 123 MiB available.
Check terminal outcomes and guest OOM captures before adopting these shapes.

- Plan/provenance: `capacity-checks/plan.json` and registered profiles. Profiles
  may contain saved credentials; do not print them wholesale.
- Sharing proof: `capacity-checks/template-sharing.json`, four passed.
  Successful raw proof is under `template-sharing-raw-v2`; retain failed v1 too.
- Spec: `capacity-checks/experiment-capacity8g.yaml`.
- Run: `capacity-c4-hostkb-20261101`.
- Attempt: `attempt-bc1f0af0301c40c3`.
- Arm: `2fae8a032249eac9720fd134`.
- Supervisor PID: `1775492`; worker PID: `1775748`. Recheck `/proc`/command lines;
  these are recorded identities, not a claim they remain live indefinitely.
- Four-case qualification passed at launch. The run uses the normal CLI,
  full recorded Tool trajectories and original command timeouts, but replay
  model-wait scale is zero. It is a correctness diagnostic, not a timing baseline.
- At the latest interim inspection all four pairs were ready, 341 Tool calls
  had completed and no 124/137/255 exit had been recorded. Long commands were
  still executing; no final pass was established.
- Guest snapshots: `capacity-checks/guest-memory-*.json`.
- Do not rerun registration or `run-capacity-gates.sh`: preparation intentionally
  refuses to overwrite its files, and this run already exists.

From a fresh SSH session:

```bash
export CLAWBOX_MACHINE_ENV=/home/weitianc/clawbox-hostkb-20261101/machine.env
cd /home/weitianc/clawbox-hostkb-20261101/source/ClawBox-restore-route-f1063e0
bash scripts/clawbox --sudo \
  --output-root /data/full1280-hostkb-pagemap-bit61-20261101/capacity-checks/runs \
  experiment status capacity-c4-hostkb-20261101
```

The read-only diagnostic `host/inspect-capacity-progress.py` checks the recorded
worker, events and live guest memory/OOM messages, saving unique evidence files.
Run it with the project environment and sudo. Do not restart a run merely
because a poll timed out. Inspect its terminal result before choosing resume
or a new run ID.

## Project network and storage

- Original `~/.bashrc` was restored byte-for-byte; original SOCKS port 1080 is
  preserved and tested working. Do not replace its general-purpose setup.
- `clawbox-build-proxy.service` exposes HTTP `127.0.0.1:8118`, forwarding through
  SOCKS `127.0.0.1:1080`. The existing Windows SSH alias restores the tunnel:
  `ssh -N kunpeng-tunnel`. It depends on the PC proxy and SSH connection staying up.
- ClawBox's selected `machine.env` supplies proxy exports, TUNA pip mirror and
  internal `NO_PROXY` addresses. Load it for manual project commands. Never
  print the whole file; it contains credentials.
- Tool APT cache: `http://193.124.7.2:3142`; PyPI cache:
  `http://193.124.7.2:3141/root/pypi/+simple/`. Explicit experiment fields enable
  these with internet access. Existing cache data was preserved.
- Devpi `mirror_no_project_list=true` fixed false 404s for `py` and `build`.
  It changed at 10:40:42 UTC during the failed Full; that run spans two cache
  configurations and cannot serve as a fixed-environment performance baseline.
- Latest snapshot filesystem check was about 83.1% used, with approximately
  305 GB before Cube's 85% rejection threshold. Recheck before Full; that margin
  is not automatically sufficient for another 2,560 VM creations.
- Preserve old logs/results, VM/template storage and **all anns-related data**.
  Do not delete arbitrary large files or automatically prune Docker volumes.

## Ordered remaining work

Execution plan (2026-11-01 continuation): preserve 160 CPUs and 500 GiB per
compute NUMA node, the 448/480 GiB watermarks and the existing shared pool.
Use the following gates to avoid repeating expensive runs:

- Finish the already-running 16 GiB case and classify the saved four-case
  command divergences. Require evidence for each affected template choice;
  a successful final validator alone does not establish absence of guest OOM.
- Identify and reproduce the checkpoint failure mechanism in a bounded
  diagnostic. Apply only a demonstrated correction, then verify snapshot
  contents and restore under pressure before changing the Full baseline.
- Close the real two-pair learning-isolation check and inventory remaining
  acceptance evidence. Record isolated-install/reboot prerequisites explicitly;
  do not reboot the shared host or call worker recovery a host reboot test.
- Freeze one new Full specification and code/native provenance after those
  gates pass. Preflight network, disk margin, unchanged-template receipts,
  resource placement and collection of required metrics before launch.
- Run Full-1280 once against that frozen baseline, audit all session outcomes,
  consequential divergences, measurements and cleanup, then commit/push the
  verified milestone. A failed gate blocks dependent work, not unrelated checks.

1. Inspect the existing four-case replay and the queued one-case 16 GiB helper; preserve terminal results,
   validation, OOM messages and cleanup. Decide final guest capacities from its
   evidence; change only affected templates if necessary.
2. Restore-route real-VM gate is complete at the scope recorded above. Retain
   the receipt; large-concurrency acceptance remains part of the next Full.
3. Resolve the memory-pressure checkpoint timeout with a targeted, evidenced
   correction. Check real per-node headroom and maintained native paths before
   adding custom machinery. Isolated round trips alone do not prove load safety.
4. Prepare one new Full specification: corrected urlman validation, verified
   affected guest shapes, committed host fixes and stable network settings.
   Preserve the prior workload/policy/default logging unless a consequential
   change is explicitly documented. Reuse valid unchanged-template evidence;
   qualify changed templates/configuration as required by the CLI.
5. Launch a **new unique Full-1280 run**, then verify all 1,280 sessions, original
   workload coverage, consequential command divergences, final validation and
   cleanup. Do not launch Full-2560 automatically.
6. Complete the requirements audit in section 21 of the original handoff.
   Remaining measurement gaps include client prediction end-to-end latency,
   isolated KB memory/CPU attribution, listener/thread queues, overlay/output
   size and learning degradation. Actual host-reboot KB recovery and fresh
   bare-metal Cube installation are not proven by worker recovery/new-venv tests.

The failed Full's useful but incomplete KB analysis is
`host/network-hostkb-metrics.json`: 13,847 prediction and 12,913 observation HTTP
requests, all 200; 50 degraded compact-observation records among 17,922 collected
Tool records. Prediction handler p50/p95/p99 were approximately
0.358/1.304/1.904 seconds. These exclude transport/listener queueing. The roughly
38 GiB process peak belongs to the whole worker, not the KB alone. Journal disk
bytes are not RSS. Do not turn these partial measurements into an acceptance claim.

Continuation is authorized. Retain the user-confirmed 160-CPU / 1,000-GiB local resource envelope across NUMA0/1; investigate reclaim and physical node headroom without silently reducing it. No successful Full-1280 result is being claimed.
