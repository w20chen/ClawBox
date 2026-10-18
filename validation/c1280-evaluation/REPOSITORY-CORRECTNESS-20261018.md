# Repository correctness verification — 2026-10-18

Scope: current dirty working tree based on `1b29a29e8ab015430fb7e52c03993d661ccd260b`.
The user's latest instruction stops formal experiments. No new formal arm or
real-VM performance run was started during this verification.

## Experiment stop status

`actual-idle-c320-full-01` has no run directory and never started. Its prerequisite
`qualification-fe2a68e8f908` failed before session execution with `EADDRINUSE`
while binding the policy-control server. Its terminal record has
`cleanup_verified: true`; subsequent process inspection found no experiment
worker, launcher or supervisor. The port was no longer listening when inspected;
the historical competing owner is unknown. No speculative port change was made.
Logs and state are retained in `correctness-only-stop-artifacts.tgz`.

## Findings and scoped changes

- The common memory action selector still visited the borrowing branch with
  `snapshot_only`/`local_only`. The lower-level execution guard already prevented
  an actual borrow, but candidate inspection and cost metadata could include a
  disabled action. Exclude borrowing before candidate selection in both calibrated
  and fallback paths. No admission or checkpoint policy was redesigned.
- Ten failures in the initial local full-suite run came from stale test doubles
  or assertions: four snapshot tests did not implement Runtime PID/state behavior;
  two borrow tests incorrectly reserved retained capacity twice; two borrower
  doubles lacked `allocates_shared`; one fake cgroup omitted effective cpusets;
  one assertion expected an obsolete validation message.
- Updated those fixtures to match production contracts. Snapshot qualification
  tests now cover both Tool-only and paired scope, including two generations,
  Runtime PID checks, tiered spill failure and cleanup. Two additional tests
  reject disabled borrowing as a calibrated candidate.
- Removed contradictory documentation describing LLM-duration predictions,
  release-cost idle recipes, or Tool-only behavior for the paired preset.

Production source changes during this verification are limited to the borrowing
candidate guard in `clawbox/experiments/worker.py`. They remain in the local
repository and isolated verification copy; the remote experiment checkout was
not redeployed. Existing experiment logs and results were preserved.

## Verification evidence

Initial Windows full suite: **815 passed, 11 failed, 50 skipped**.
After the scoped corrections: **832 passed, 50 skipped**, 76.34 seconds.
Raw final log: `current-repository-tests-final.txt`.
Focused qualification/NUMA regression: **56 passed, 4 skipped**.
`git diff --check` passed.

The Linux tests use an isolated source copy at
`/tmp/clawbox-codecheck-20261018-w9jnj4db`; they do not launch the formal matrix.
An initial run used the wrong Python/source dependencies. A subsequent run with
the older `/home/weitianc/ClawTune` checkout had 854 passes, 5 schema-version
failures and 23 skips. Those five failures concern v3/v6/v3 snapshots versus
the expected v4/v7/v4; they are not evidence against the deployed environment.
The actual CLI selects `setup-source/.venv/bin/python`, and saved machine settings
select `ClawTune-1bdb4c64ea4e9f71231cdd749eb085fbc617f67e`. Both were checked.
Final verification uses that interpreter and source explicitly.
Result: **859 passed, 23 skipped**, 70.97 seconds; one third-party deprecation
warning. Raw log: `current-repository-linux-deployed-env.txt`.

Reproduction, with those paths selected and from the isolated checkout:

```bash
python -m pytest -q -o addopts= --tb=short -p no:cacheprovider
```

Source audit: `repository-verification-source.tgz` and its SHA-256 manifest capture
the starting tree; `repository-verification-fix.tgz` captures the tested source
and fixture corrections; `repository-verification-final-source.json` records
the final tracked source/document contents.

## Review boundaries

Reviewed actual-wait eligibility, response/pause ordering, paired capacity
reservation, WARM backpressure, restore-409 scope, Agent failure isolation,
policy failure propagation and cleanup ownership. Regression tests exercise
those paths, including ordinary Agent failure leaving sibling futures running.
The TAP service names were checked read-only against Kunpeng's installed units.

This is code regression verification, not proof of zero bugs, fresh installation,
high-concurrency performance, or every CubeSandbox backend failure mode. Existing
real-VM qualification evidence remains in `IDLE-CHECKPOINT-VERIFICATION.md`; no new
real-VM qualification was run here. Skipped tests remain unverified in the relevant
environment. No completed formal experiment was rerun or relabeled.
