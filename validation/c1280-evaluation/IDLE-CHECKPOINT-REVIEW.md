# Idle checkpoint implementation review (pre-fix audit)

This is the pre-fix audit recorded on 2026-10-18. Statements about the running
OS arm, pending fixes and supported release_cost below describe that audit's
point in time. The subsequent user-authorized repair removes all LLM-duration
policies, unifies pressure reclamation and reserves the whole VM pair before
pause. Current validation is in `IDLE-CHECKPOINT-VERIFICATION.md`.
Existing experiment artifacts retain their original semantics.

## Public choices

`idle_reclaim.policy` accepts `off`, `wait`, and `release_cost` (default `off`).
`idle_reclaim.scope` accepts `tool` and `agent_and_tool` (default `tool`).
`release_cost` with pair scope is rejected before execution. The idle-reclaim
recipe therefore creates resident, wait-tool, wait-pair, and release-cost-tool.
There is no accepted unconditional eager-on-idle, fixed-delay, or oracle mode.
Legacy enum members with those names are rejected by PolicySpec validation.

- off: leave the VMs resident; checkpoint settings are dormant.
- wait: pressure-based reclamation of a resident session with an outstanding
  model request, no generated response, no active Tool, and no eviction in
  progress. Select the oldest last-used eligible session. Current remote code
  ALSO requires an unexpired predicted wait deadline. The local correction
  removes that extra requirement and makes the prediction optional/unused.
- release_cost: requires blocked admission demand, measured LOCAL release
  potential, a minimum release threshold, cooldown eligibility, and predicted
  remaining wait sufficient to amortize checkpoint plus restore. Rank by LOCAL
  bytes divided by round-trip cost. Use recent per-VM costs when available,
  otherwise independent configured costs. This separate strategy intentionally
  retains a duration forecast and supports Tool-only reclamation.

Pair scope normally pauses Tool then Runtime. Runtime is restored and its
model relay reconnected before response delivery; Tool restores on its next
operation. Restore is reactive, not predictive. A response arriving during
an already-claimed checkpoint may wait for that operation to finish.

`checkpoint_restore_headroom_mib` defaults to 1024 MiB. It is LOCAL operation
headroom, not a WARM snapshot-size measurement. The separate checkpoint
dimension selects encoding (full-copy/incremental-cow), storage
(disk/warm-only/tiered), residency (lru/value), and transfer concurrency/timeout.
The study uses incremental-cow, warm-only, lru; no SSD comparison is intended.

## Findings

1. **Confirmed design mismatch: wait-duration expiry.** Commit d7a5126 added
   the expiry guard on 2026-10-17 at 01:02 CST. Frozen real-trace configurations
   from c320-01 onward use 2.7181621785 seconds from an independent trace.
   Restoring original LLM delays does not remove this gate. Full's first borrow
   began while 19 saved model requests on its NUMA node were still outstanding;
   12 had exceeded the request-clock forecast. This overlap is not a complete
   reconstruction of the selector: callback execution times and every rejected
   candidate's flags were not recorded. Do not attribute every empty candidate
   set solely to this guard. The scoped local fix removes the gate for wait
   only; release_cost retains its forecast. Unit and mocked-worker coverage
   passes; remote real-VM verification is pending.

2. **Confirmed WARM-full failure propagation and partial pairs.** The joint
   watermark selector prechecks shared capacity, but direct model-wait and
   admission eviction paths call lifecycle checkpoint without that same gate.
   Warm-only lifecycle raises WarmCapacityError when its reservation cannot fit.
   Policy eviction propagates it; the watermark thread treats exceptions as
   controller failures. Two VM reservations are not acquired atomically: Tool
   can pause and Runtime can then fail to reserve. The isolated fault audit
   reproduces exception propagation and Tool-paused/Runtime-resident state.
   A small-pool experiment would therefore measure more than fewer checkpoints.
   This issue did not cause the current zero-checkpoint result: the observed
   384-GiB WARM pool was largely empty. Fix before capacity-scaling ablations.

3. **Confirmed measured-cost coverage gap.** WatermarkController's joint
   action selector compares reclaim and borrowing costs. PolicyCoordinator's
   blocked-admission loop and the model-wait callback can independently
   checkpoint directly. Thus measured_cost is not a global policy over every
   checkpoint. The selected victim is also fixed before that cost comparison.
   Do not claim all checkpoint/borrow decisions optimize that common objective.
   Preserve a single selection rule before using coordination as an ablation.

4. **Confirmed scope mismatch in restore-capacity recovery.** After a Cube
   restore returns 409, PolicyCoordinator.restore claims another victim and
   pauses only its Tool lifecycle. Runtime is omitted even with pair scope.
   Isolated reproduction records two restore attempts, one Tool pause and zero
   Runtime pauses. This is an exceptional path, not an explanation for current
   runs with zero pauses; retain its lower priority unless exercised.

5. **Timing risk, not yet reproduced as a workload failure.** The model-wait
   callback checks response/tool state before PID-witness I/O and snapshot
   calls, and it only checked memory pressure when scheduling the callback.
   A delayed callback can act after pressure changes; a response can be generated
   while a claimed/in-progress pause finishes. Response-hold time must remain
   separate from recorded inference time. Existing serialization protects
   response delivery, but it is not evidence every pause starts while useful.

## Evidence and execution decision

- `idle-prediction-history.json`: frozen run configs and creation timestamps.
- `idle-eligibility-audit.json`: saved model-wait intervals aligned with decisions;
  includes node-filtered counts and explicit reconstruction limitations.
- `audit-idle-checkpoint.py` / `idle-checkpoint-fault-audit.json`: isolated
  fault injection; no real VM or performance claims.
- `raw-balanced-full/` and `raw-balanced-aoff/`: raw event backups with hashes.

Keep the OS comparison running. Do not launch a large Full/checkpoint or
small-WARM ablation until the relevant correctness gate is resolved. Retain
all earlier results as measurements of their original forecast-gated policy;
do not rerun unaffected OS/resident arms. The paper's intended simple comparison
is Full with actual-idle pair reclamation versus the identical configuration
with idle_reclaim.policy=off. Tool-only is a secondary scope ablation.
