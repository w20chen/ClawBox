# ClawBox handoff — 2026-09-28

This is an agent handoff, not end-user documentation. The local ClawBox worktree
contains uncommitted implementation changes. Do not reset or replace it.

## 1. Final objective

The requested end state is a reliable, self-service standalone CubeSandbox
experiment product that integrates the current ClawTune and can run a fair
memory-overcommit study without help from a code agent.

The supported comparison is intentionally limited to:

1. **A — `tool-static-resident`**: calibrated fixed Tool-memory admission; VMs
   remain resident.
2. **A+B — `tool-p50-resident`**: command-specific P50 admission; VMs remain
   resident.
3. **A+B+C — `tool-p50-wait-reactive`**: the same P50 admission plus
   wait-aware WARM checkpoint and reactive restore.

P50 prediction must use LatticeKB when it has a valid memory result and fall
back to ToolKB inside ClawBox when LatticeKB is unavailable. This fallback must
not be added to ClawTune. Very short calls with no in-execution memory sample
may remain unavailable and must not be turned into zero-valued labels.

The final experiment must use CubeSandbox measurements, not Docker benchmark
replay, at useful concurrency (target c16), with enough LOCAL and WARM capacity.
It must show deployment-density consequences and report throughput/JCT and
memory evidence honestly. If the selected workload or capacity produces no
meaningful difference, choose a representative, higher-memory-variance trace
or correct the experiment design before running for hours; do not claim an
advantage from a smoke test.

The user must ultimately be able to build images, configure the host, validate,
qualify, run/resume/abort/destroy an experiment, and inspect results with the
`clawbox experiment` CLI alone. Old control-plane modules and old baselines stay
in the tree but are marked deprecated. No historical API/schema/state
compatibility is required.

## 2. What is implemented in the local ClawBox worktree

- The three canonical A/A+B/A+B+C baselines are the only supported formal
  policies. Retained alternatives are listed as deprecated and rejected by the
  supported experiment path.
- P50 artifacts contain LatticeKB and ToolKB results and freeze the selection
  before evaluation. Missing memory observations are not rewritten as zero.
- OpenClaw replay validates that a model is configured before VM creation.
  The managed gateway now sends a synthetic prefix stop after the last allowed
  recorded Tool round, so one-step qualification does not fail as
  `trace_exhausted` when OpenClaw asks for the post-Tool response.
- Each arm runs in its own worker process under a supervisor with a hard
  deadline, atomic run state, heartbeat/progress tracking, terminal-state
  handling, cleanup verification, abort, and safe resume of only completed
  arms.
- Policy, model-gateway, Tool telemetry, output validation, LOCAL-pool and OOM
  checks, and VM cleanup failures propagate to the result instead of being
  reported as success.
- Qualification performs a real target-concurrency run. For incremental WARM
  policies it now requires two checkpoint/restore generations, verifies state
  after each restore, and requires generation 2 to be a non-full-base delta
  smaller than logical guest RAM. Qualification receipt schema is version 2.
- `clawbox experiment setup` starts/checks the installed standalone services,
  configures LOCAL/WARM pools, checks templates and immutable digests, probes a
  real VM, and atomically writes the host profile only on success.
- WARM setup accepts `--cube-source`. It checks the Python SDK in a fresh
  interpreter, applies only the SDK hunks from the tiered and incremental Cube
  patches when required, installs the SDK, and activates a just-installed
  editable SDK in the same process. The source path is persisted.
- `configure` reads the active setup profile schema. A bug found on the host was
  fixed: setup writes `warm_capacity_mib`, and configure now maps that value to
  experiment `warm_memory_capacity_mib`.
- `clawbox experiment images` can rebuild both current ClawTune guest
  integrations and templates. It flattens the old base image to prevent layer
  depth exhaustion, normalizes copied Linux scripts to LF (fixing a real
  `python3\r` failure), embeds the ARM64 mvdan parser, uses immutable digests,
  and atomically persists non-secret build inputs so subsequent runs can use
  only `clawbox experiment images`.
- User-facing setup/configure/qualification/run instructions and deprecation
  scope were rewritten in `docs/guide.md`, `docs/lab.md`, and
  `docs/deprecated.md`.

The local ClawTune checkout was not modified during this final repair. It is
clean at commit `8fce6b72770b2eaf8a973b82183c72477fb94c2f`.

## 3. Verification completed

### Local Windows worktree

- A complete `python -m pytest -q` run passed after the major supervisor,
  qualification, gateway, image, and initial SDK-setup changes.
- `git diff --check` passed.
- After the last two host-discovered fixes (same-process SDK activation and
  `warm_capacity_mib` profile mapping), their targeted suites passed:
  `tests/test_lab.py`, `tests/test_cli.py`.
- A new complete local suite has **not** been rerun after those last two small
  fixes. This is the first validation step for the next agent.

### kunpeng ARM64 host

SSH alias: `kunpeng` (`193.124.7.2`). Verification was isolated under `/tmp`;
the user's long-lived ClawBox/ClawTune worktrees were not overwritten.

Current verified templates:

- Runtime template: `tpl-e655070631064896b85a7ed2`
- Runtime image: `sha256:ab5e76e435bfeab5a4d0991ad6f1455fdd923525cf267ef3270c9dca11d31e28`
- Tool template: `tpl-77b10ab318904da1bddb9b5d`
- Tool image: `sha256:d05bcd4b381fdd353ddb8fdff82ed2607c34873118b9d0db37f1b91de52ae754`
- Both images contain ClawTune revision `8fce6b72770b2eaf8a973b82183c72477fb94c2f`.

Matching ClawTune export on the host:
`/tmp/clawbox-verify-20260928/ClawTune`. It contains the expected schemas
`runtime_tool_resource_kb_v4`, `runtime_clause_resource_kb_v7`, and
`clause_lattice_kb_v4`. Do not use `/home/weitianc/ClawTune` for this
verification: it is older (`e7645cb...`) and has unrelated untracked user data.

Latest extracted ClawBox source:
`/tmp/clawbox-final-suite-3/ClawBox`.

The latest `setup` implementation was exercised from the preceding identical
source generation at `/tmp/clawbox-final-suite-2/ClawBox` and passed every
check:

- KVM and cgroup v2
- all CubeSandbox systemd services and support containers
- both templates and immutable image digests
- LOCAL `memory.max` = 64 GiB on NUMA node 0, swap disabled
- WARM tmpfs = 128 GiB on NUMA node 1
- dirty tracking and private-copy settings
- tiered/incremental SDK and host swap disabled
- real VM create, guest command execution, and destruction

Saved verified profile:
`/tmp/clawbox-final-suite-2/host.json`.

The latest configure fix was then used from suite 3 to create:
`/tmp/clawbox-final-suite-3/c1.yaml`. Both
`experiment validate --inputs` and `experiment doctor --probe-vm` passed.

Real c1 qualification passed:

- Receipt: `/tmp/clawbox-final-suite-3/c1.yaml.qualification.json`
- Run: `/tmp/clawbox-final-suite-3/results/qualification-74b7a6cc46e3`
- Policy: `tool-static-resident`
- Status: succeeded
- Completed/failed sessions: 1/0
- Native Tool exact-ID join rate: 1.0
- Native telemetry losses: 0
- Host OOM kills: 0
- Pool budget exceeded: false
- Supervisor fault test: passed
- Cleanup: `cleanup_verified=true`

The c1 run executed real OpenClaw plus a native `exec` Tool call. It took about
49.7 seconds end to end. This is an operational smoke/qualification result, not
a performance result.

An earlier direct two-generation WARM probe on the same host/templates passed:

- generation 1: full 4 GiB base; checkpoint ~1.83 s; restore ~0.30 s
- generation 2: `full_base=false`, lineage depth 2, transferred 5,226,496 bytes
  versus 4 GiB logical RAM; checkpoint ~0.124 s; restore ~0.053 s

That proves the implemented second checkpoint is a small native RAM delta after
the mandatory first full base. A+B+C still needs the new schema-2 qualification
receipt at c16.

The complete ARM64 test suite passed in `/tmp/clawbox-final-suite/ClawBox`
against the matching ClawTune export. This was before the last two small fixes.
The suite-2 targeted `test_lab.py` and `test_cli.py` run passed after SDK
activation; suite-3 `configure`, `validate`, `doctor`, and real c1 qualification
passed after the WARM profile mapping fix.

## 4. Exact stop point and unresolved defects

Work stopped immediately after trying to produce a P50 artifact from the c1
qualification run:

```text
clawbox: error: /tmp/clawbox-final-suite-3/results/qualification-74b7a6cc46e3:
expected a ClawBox CubeSandbox run
```

`clawbox/experiments/training.py` currently requires
`<run>/owned-sandboxes.jsonl`, but the new supervisor stores the journal under
`<run>/attempts/<attempt>/<arm>/owned-sandboxes.jsonl`. Training has not yet
been updated for the supervisor output layout.

Do **not** solve this by training from the successful qualification run. That
run intentionally uses `validation.command: true` and `max_model_steps: 1`; it
is a prefix gate, not a complete validated training workload. Its only agent
command was a ~2.6 ms `printf`, and its memory label reports
`no_in_execution_memory_sample`. It therefore cannot supply a valid P50
extra-memory label even after the path gate is fixed.

The checked-in `examples/traces/smoke.jsonl` contains only the initial model
Tool-call response. Qualification can terminate that prefix by design; a normal
full training run may need a recorded post-Tool assistant response. Use a
complete trace or extend the fixture correctly before a normal run.

No P50 artifact has been produced. A+B and A+B+C have not passed real
qualification. The three-policy c16 experiment has not run, so there is no fair
throughput/JCT comparison and no supported claim of an overcommit advantage.

The new image command's build-input persistence is covered by tests, but the
latest script has not been rerun end to end on kunpeng. The verified images were
built during the preceding iteration. Consequently
`/tmp/clawbox-final-suite-2/host.json` has `cube_source` but no `image_build`
record. A user still needs one successful explicit `images` invocation before
future image refreshes become argument-free.

The verified `/tmp` profile has not been promoted to
`~/.config/clawbox/host.json` because final c16 qualification is incomplete.

An earlier host audit found 47 paused VMs without useful ownership metadata.
They were not destroyed. Automatic approval review rejected VM destruction
because permission to delete crash-dump files did not explicitly authorize
destroying VMs. They did not block current verification; obtain explicit user
permission before removing them.

## 5. Required next work, in order

1. **Re-run complete tests on the exact latest worktree.**
   Run local `python -m pytest -q` and `git diff --check`. Upload the same
   audited source set, run the complete ARM64 suite with
   `CLAWTUNE_ROOT=/tmp/clawbox-verify-20260928/ClawTune`, and keep ClawTune at
   revision `8fce6b7`.

2. **Repair training for the supervisor layout.**
   Change the run-source validation in `training.py` to recognize the current
   top-level `experiment.yaml`/`summary.json` plus owned journals and datasets
   below `attempts/<attempt>/<arm>`. Validate that every accepted arm is a
   successful CubeSandbox arm with cleanup verified. Add focused tests. Do not
   add legacy layout fallback merely for compatibility.

3. **Choose and run a real training workload.**
   Use a complete current OpenClaw replay in CubeSandbox with commands long
   enough to yield valid `guest_memtotal_minus_memavailable` extra-memory
   observations. The user asked for a case with meaningful memory variation;
   the smoke `printf` and the previously questioned
   `15five__scim2-filter-parser-13` case have not been established as suitable.
   Verify the trace source, completeness, command list, and observed memory
   range before committing to the long run. Run A only, with its actual final
   validation command, as a separate training run.

4. **Generate and inspect the frozen P50 artifact.**
   Run `clawbox experiment train` on that completed training run. Confirm the
   exact selected P50 memory value and whether each command used LatticeKB,
   ToolKB fallback, or the accepted short-call unavailable path. Confirm the
   Tool image digest/shape matches evaluation.

5. **Design the fair c16 evaluation before launching it.**
   Configure A/A+B/A+B+C from one base trace, one VM shape, one 64 GiB LOCAL
   pool, one 128 GiB WARM tmpfs, one burst/stagger schedule, and one repetition
   as requested. Calibrate A's fixed reservation from separate evidence; do not
   use an unfair 4 GiB-per-command static value, but also do not choose a value
   so close to P50 that both policies trivially admit all 16 sessions and cannot
   exercise density. Show the predicted admission capacities before running.

6. **Qualify the exact c16 three-policy spec.**
   `validate --inputs`, `doctor --probe-vm`, then `experiment qualify`. This
   qualification must produce a schema-2 receipt, run all three policies at
   c16, perform the two-generation WARM delta test, execute native Tools, and
   finish with cleanup verified.

7. **Run the formal experiment once and report only auditable evidence.**
   Report offered concurrency, actual admitted/running sessions, throughput,
   JCT, admission blocking, peak LOCAL use, OOM counters, checkpoint/restore
   time, WARM bytes, prediction coverage/source/error, and cleanup. Do not infer
   deployment density only from configured 4 GiB VM capacity; distinguish VM
   shape, demand-backed host memory, predicted incremental demand, admitted
   reservation, and measured physical use.

8. **Finish self-service installation.**
   Exercise the latest `clawbox experiment images` once with explicit registry,
   Go, guest-kernel source/build, and network mode so `image_build` is saved.
   After all gates pass, promote the verified profile to the user's normal
   profile path and run the documented commands exactly as written.

9. **Commit hygiene.**
   Review the large uncommitted ClawBox diff, exclude `.verification/`, rerun
   tests, and commit only source/tests/docs. Do not modify or commit the user's
   older remote ClawTune checkout or its untracked data.

## 6. Useful commands and paths

Latest local source archive already authorized for kunpeng transfer:
`.verification/clawbox-final.tar.gz`. It contains Git source/test files only;
it excludes `.git`, credentials, results, and `.verification` content itself.

Remote setup command that passed (source/profile paths may be moved after final
promotion):

```bash
cd /tmp/clawbox-final-suite-2/ClawBox
CLAWTUNE_ROOT=/tmp/clawbox-final-suite-2/ClawTune \
python3 -m clawbox.cli experiment setup \
  --profile /tmp/clawbox-final-suite-2/host.json \
  --runtime-template tpl-e655070631064896b85a7ed2 \
  --tool-template tpl-77b10ab318904da1bddb9b5d \
  --node 193.124.7.2 \
  --cube-source /home/weitianc/CubeSandbox \
  --local-gib 64 --local-node 0 \
  --warm --warm-gib 128 --warm-node 1
```

Latest c1 artifacts are intentionally under `/tmp`; preserve them until the
next agent has inspected the evidence. There is no experiment process still
running at handoff time, and the successful c1 qualification verified cleanup.
