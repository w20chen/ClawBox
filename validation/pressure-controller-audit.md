# Source audit

Base commit: `bf42eeeaa69429e83965436738e1723f755f872b`.
Pre-existing edit: `worker.py` extends endpoint identity verification retries;
preserve it. Kunpeng is accessible; its working tree contains extensive local
changes, so validation must use an isolated source copy.

Integration points verified before editing:

- `congestion.RuntimeMemoryControl` loads the hash-pinned upstream fallback;
  `priority.PriorityDomain` selects one HIGH session and groups all others LOW.
  Tokens route Cube VMMs into stable role leaves before allocation; verification
  checks shim identity, vCPU threads, cpuset and memory nodes. Restore reuses leaves.
- `worker._run_session` creates separate Runtime/Tool lifecycles. Native SSH
  admission precedes subprocess launch; completion proves SSH reap and endpoint
  epoch. Model gateway start/response callbacks identify waits. Admission is not
  proof that execution started; an explicit start notification is needed.
- `DemandLedger` subtracts observed growth from incremental claims. Per-domain
  `PolicyCoordinator` owns admission, progress-first queues, atomic eviction
  claims and completion notifications. Do not create another queue.
- `PolicyCoordinator._select_victim_locked` guards active Tools and in-flight
  eviction, but currently ranks by LRU without checking wait amortization.
  `CubeSandboxLifecycle` owns checkpoint/restore and snapshot tiers. Existing
  snapshot policies can also pause Runtime using relay callbacks.
- `WatermarkController`, `numa_borrow`, placement and snapshot pools own LOCAL
  headroom, shared borrowing and tier budgets. Controller feedback must use the
  coordinator's domain sample and configured budget; no global-free substitution.
- Suites `admission` and `numa-snapshot` fix independent mechanism settings.
  Replay preserves trace model waits. Worker/reporting already collect completion,
  latency, admission, memory and lifecycle metrics.
- Current predictions provide calibrated host growth, not verified post-command
  memory release or reliable remaining CPU work. Such values cannot be fabricated
  from predicted peak memory or future trace waits. CPU priority must abstain
  without release and execution evidence.
