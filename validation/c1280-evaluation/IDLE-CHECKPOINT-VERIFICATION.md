# Actual-idle checkpoint verification

Verified on 2026-10-18; deployed to Kunpeng after the existing OS screen ended.
This is correctness evidence, not a c320/c640 throughput result.

## Implemented behavior

- Supported idle policies are `off` and `wait`. Waiting means a real outstanding
  model request, no generated response, and no active Tool. Pressure triggers
  reclamation; elapsed or predicted LLM duration does not determine eligibility.
- Removed release-cost ranking, duration-estimate inputs/CLI options and the
  associated runtime presets. Oracle eviction and proactive restore are rejected.
  Tool memory prediction and CPU release-history controls remain independent.
- Admission, model-wait and watermark paths share the configured pressure
  selector. The exceptional Cube-409 recovery uses the same paired checkpoint
  implementation; borrowing cannot resolve a Cube VM-capacity rejection.
- Reserve both VM snapshots before pausing either. WARM-only capacity exhaustion
  skips reclamation without failing an Agent. A freed pool permits retry.
  Actual RPC/accounting failures still propagate; unknown snapshots retain their
  capacity charge until cleanup. No claim of atomic rollback after an RPC fails.
- Response generation immediately removes eligibility. A committed pause finishes
  before response delivery; Runtime restores before the response, Tool on its
  next admission. Tool start and victim selection share the coordinator lock.
- The existing public snapshot qualification now checks the selected pair scope,
  Runtime PID survival and Tool guest state across both incremental generations.

## Validation evidence

| Check | Result |
|---|---|
| Kunpeng Linux regression tests | 335 passed in 19.84 s |
| Local Windows regression tests | 330 passed, 3 skipped; two additional qualification tests passed separately |
| Whole-pair reservation and WARM-full retry | Tested with insufficient capacity and competing live-borrow reservations |
| Response during Tool pause | Runtime still pauses as committed; response waits; no new victim eligibility |
| Restore 409 | Both roles pause; second restore attempt succeeds |
| Selector enforcement | Admission cannot bypass the configured selector |
| Public commands in a fresh SSH process | Repeated validate/configure, explicit `--force`, expected overwrite refusal and unsupported-policy refusal |
| Real pair snapshot probes | NUMA 0 and 1; two incremental generations, Runtime PID and guest state preserved, cleanup passed |
| Pressure replay, c32 requested | 15 successful bounded sessions; 12 paired pauses and 12 paired restores; all 12 paused sessions completed; no session/policy errors |
| Final public qualification, c8 | 8/8 successful bounded sessions; cleanup verified; receipt issued |

The c32/c8 qualification uses the original first LLM step at `time_scale: 1.0`
and the corresponding native Tool work. It is not completion of the full trace.
The c32 event log contains 24 `sandbox_paused` and 24 `sandbox_restored` records,
with both roles linked to model request IDs. All 12 actual OpenClaw Agent PIDs
were unchanged and alive before/after Runtime pause. The c8 replay did not
trigger pressure checkpointing; its separate pair probes did. Keep these
evidence types distinct.

The c32 diagnostic used total LOCAL LOW/HIGH 16/20 GiB, split evenly over the
two compute nodes, with unchanged physical LOCAL 928 GiB and WARM 384 GiB.
Borrowing, Tool admission and runtime controls were off. After 15 completions,
no live VMs remained but approximately 7 GiB of file cache did. NUMA 1's cache
plus the next pair's configured reservation exceeded its 10 GiB admission
budget. Proactive cache reclaim was off. The run was stopped and cleaned;
the other 17 requests must not be counted as successful or failed Agents.
Its raw data is retained as a configuration boundary. The next c8 qualification
enabled the existing proactive-cache-reclaim switch using `experiment configure`.
No execution code was changed between these two trials.

The initial one-node verification proposal failed host preflight before any VM
started because it did not match the applied common host topology. The final
configurations retain both compute nodes and the applied physical capacities.

## Artifacts and limits

- `idle-fix-live-artifacts.tgz`: both qualification run directories, both probe
  trees, model/per-session/event logs, terminal cleanup states, receipt, CLI
  verification and Linux test output. SHA256:
  `61643f208e04ea1bceabdd8c4b3193f29df843667967319d8576de0f1c118590`.
- Extracted small reports: `idle-fix-live/`; local tests:
  `idle-fix-verified-tests.txt`. Ten deployed implementation files match local
  source hashes in `idle-fix-live/idle-fix-deployed-source-hashes.json`.
- Pressure run: `qualification-51367dfb7932`, cancelled with cleanup verified.
  Complete qualification: `qualification-3ff1ef31ab25`, succeeded and cleaned.
- Qualification implementation digest:
  `1f3de220e4e5c286226d1467d404ecbf48801ded76a33bc564f5d842daa9627a`.
- Existing c320/c640 forecast-gated runs and the OS screen are preserved, not
  rerun or relabelled. Actual-idle c320/c640 performance ablations remain pending.
  No successful-throughput improvement has been established by this repair.
- Installation scripts/images were not changed. These existing-host tests are
  not evidence of a clean-machine installation. WARM-full fault cases were
  tested in isolation; no large real shared-pool-capacity sweep was performed.

The idle-checkpoint correctness gate is cleared for the next bounded experiment.
Keep a viable admission budget and use the existing cache-reclaim switch where
retained file cache would otherwise block all new VM-pair creation.
