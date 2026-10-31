# Run experiments

ClawBox's supported interface is `clawbox experiment`. Each session has a
**Runtime VM**, which runs OpenClaw, and a **Tool VM**, which executes commands
in the task workspace. Complete the [host installation](installation.md) before
following this walkthrough.

Run commands from the ClawBox checkout on the ARM64 Linux experiment host.
`bash scripts/clawbox` loads `~/.config/clawbox/machine.env` and selects the
project's Python environment in a fresh shell. Wrapper options precede CLI
arguments:

```bash
bash scripts/clawbox --machine-env /absolute/path/machine.env --sudo \
  --output-root /data/clawbox-results experiment status RUN_ID
```

The output root defaults to `CLAWBOX_OUTPUT_ROOT`, or `results` when unset. Use
the same root for launch, status, resume, reporting, and cleanup. Runs launched
with `--sudo` also need `--sudo` for commands that rewrite their artifacts,
including `collect` and `report`; `status` is read-only. See the
[command and configuration reference](reference.md) for options and defaults.

## 1. Check the host

```bash
cd "$HOME/src/ClawBox"
bash scripts/clawbox experiment host check "$HOME/.config/clawbox/host.yaml"
```

After a reboot, follow [host recovery](installation.md#recover-after-reboot).
After changing the host plan, apply it on an idle VM pool and regenerate the
experiment from the resulting profile. The editable plan is `host.yaml`;
`host.json` is the applied profile used by `configure`.

When upgrading from ClawTune with EdgeKappaKB, first follow
[Runtime predictor and KB refresh](installation.md#refresh-the-runtime-predictor-and-kb).
Use the updated seed, refreshed Runtime template, and a new run ID; old KB
manifests containing EdgeKappa are not supported by the new ClawTune.

## 2. Run a smoke experiment

```bash
mkdir -p "$HOME/clawbox-specs"
bash scripts/clawbox experiment recipes
bash scripts/clawbox experiment configure \
  examples/experiments/six-dimension.yaml "$HOME/clawbox-specs/smoke.yaml" \
  --profile "$HOME/.config/clawbox/host.json" \
  --recipe custom --concurrency 1 --repetitions 1
bash scripts/clawbox experiment describe "$HOME/clawbox-specs/smoke.yaml"
bash scripts/clawbox experiment validate "$HOME/clawbox-specs/smoke.yaml" --inputs
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/smoke.yaml" \
  --run-id smoke-01
bash scripts/clawbox --sudo experiment report smoke-01
```

The smoke trace creates `/workspace/result.txt`; final validation checks its
content. `configure` fills in template identities and resources from the
profile. It does not resize templates or install task dependencies. Standard
new profiles use 1 GiB Runtime and 3 GiB Tool VMs; select registered templates
with the sizes and workspace required by your workload.

New specifications use **schema version 4**. Review the generated YAML before
launch. `--force` replaces a generated YAML file; it does not permit changing a
frozen run. Relative input paths resolve from the working directory. Use
absolute paths for external traces, KBs, manifests, and results.

## 3. Select a workload and comparison

For an existing replay and prepared Tool image:

```bash
bash scripts/clawbox experiment configure \
  examples/experiments/six-dimension.yaml "$HOME/clawbox-specs/eval.yaml" \
  --profile "$HOME/.config/clawbox/host.json" \
  --recipe custom --trace /absolute/path/replay.jsonl \
  --repository OWNER/REPOSITORY --base-commit COMMIT \
  --tool-workspace /testbed --prompt 'Complete the recorded task.' \
  --validation-command 'cd /testbed && YOUR_VALIDATION_COMMAND' \
  --concurrency 1 --repetitions 1
```

Replace the repository, revision, prompt, and validation command with the task's
actual inputs. The Tool image must already contain that revision and its
dependencies. For multiple tasks, use `--workload-manifest /absolute/path/workload.json`;
this selects unique case assignment and the manifest's per-task environments.
See [workload preparation](workloads.md) for trace import and task-image builds.

Use `inference.backend: replay` for this workflow. The CLI also accepts `api`
provider settings, but current qualification requires replay, so live API
inference does not have a complete supported launch path.

Recipes generate explicit comparison arms. Use `--fixed-methods --recipe FAMILY`
to compare one policy family while fixing unrelated mechanisms. Available
families cover admission, runtime control, memory reclaim, placement, idle
reclaim, shared memory, coordination, checkpointing, integrated policies, and
resident capacity. `custom` keeps an editable design.

Each arm's `comparison.arms[].override` takes precedence over common mechanism
values. For example:

```yaml
comparison:
  recipe: custom
  arms:
    - name: full-pool
      override: {placement.tool_call_cpu_policy: c0}
    - name: one-cache-cluster
      override: {placement.tool_call_cpu_policy: c2}
```

Use `configure --set GROUP.FIELD=VALUE` to edit common mechanisms, or edit the
YAML directly. Environment, workload, and evidence stay common across arms.
Run `describe` and `plan` to inspect the resolved comparison.

### Predicted memory admission

Choose the prediction source explicitly:

| CLI mode | Input | Use |
| --- | --- | --- |
| `host-live` | Immutable ClawTune seed directory | Independent live host KB for each Runtime–Tool pair |
| `cold-start` | Immutable ClawTune seed directory | Query a general deployment prior across repositories |
| `runtime` | Training JSON and matching `.kb.json` | Query the trained, frozen KB for incoming commands |
| `frozen` | Training JSON with precomputed predictions | Replay commands covered by that artifact |

For cold-start admission:

```bash
bash scripts/clawbox experiment configure \
  "$HOME/clawbox-specs/eval.yaml" "$HOME/clawbox-specs/predicted.yaml" \
  --profile "$HOME/.config/clawbox/host.json" \
  --recipe admission --fixed-methods \
  --prediction-mode cold-start \
  --prediction-artifact /absolute/path/ClawTune/seeds/bootstrap-v2 \
  --static-tool-memory-mib 64
bash scripts/clawbox experiment describe "$HOME/clawbox-specs/predicted.yaml"
```

Here `64` MiB is an explicitly chosen fixed-baseline charge; replace it with
your experiment's value. With a trained artifact, `--static-tool-memory-mib auto`
uses its measured host-growth P90 calibration. Cold-start seeds do not provide
that fixed-baseline calibration.

Cold-start mode uses a selected P50 memory signal directly as a host-growth
estimate. Trained modes use the artifact's measured guest-to-host mapping.
Neither is a hard memory bound. The current bootstrap seed permits zero-imputed
missing labels; prediction availability is not evidence of accuracy. See
[prediction semantics](design.md#prediction-and-measurement).

For online learning, use `--prediction-mode host-live` with the same seed path.
First refresh the Runtime plugin and Tool collector as described in
[installation](installation.md#refresh-the-runtime-predictor-and-kb). The host
shares the immutable seed and keeps each pair's learned observations separate.
The Runtime requests predictions; the Tool sends compact observations before
its command returns. Neither VM owns a KB. Admission uses the exact host
prediction issued for that command. A prediction failure stops that trace
before command execution. An observation delivery failure preserves the command
result, continues with committed knowledge, and marks online learning degraded.

The generated YAML exposes these settings under `execution`:

```yaml
kb_scope_policy: pair
kb_http_timeout_seconds: 30
kb_observation_retries: 2
retain_clause_telemetry_artifacts: false
```

Missing training targets retain ClawTune's explicitly marked imputed zeros.
Measured guest memory keeps its own measurement identity. Enable telemetry
artifact retention only when the full clause diagnostic file is needed; normal
learning and result validation use compact records. Changing these settings
requires a new run ID, without retraining or another image refresh.

Inspect seed coverage for a workload without starting VMs:

```bash
python scripts/evaluate-cold-start-coverage.py \
  --seed /absolute/path/ClawTune/seeds/bootstrap-v2 \
  --workload /absolute/path/workload.json --output /absolute/path/coverage.json
```

Use a new output path. This reports query coverage and prediction values, not
held-out duration or memory accuracy.

The default **4 MiB bypass** skips memory-ledger accounting for reservations at
or below 4 MiB, including fixed-budget filesystem and maintenance operations.
Calls remain active and measured. Set
`mechanisms.admission.small_tool_admission_bypass_mib: 0` to disable it. The
automatic below-LOW routing fast path is separate and retains memory accounting.

### Train a workload-specific KB

Collect successful training runs with admission disabled and the intended Tool
image, repository, VM shape, and collector. Keep training and evaluation runs
separate. Then generate both prediction files:

```bash
bash scripts/clawbox experiment train /data/clawbox-results/TRAIN_RUN \
  --trace /absolute/path/evaluation-replay.jsonl \
  --repository OWNER/REPOSITORY \
  --output /data/clawbox-predictions/workload.json
```

Training requires completed, validated runs and paired guest/host memory
measurements. It writes `workload.json` and `workload.kb.json`; keep them together.
Configure evaluation with `--prediction-mode runtime` and
`--prediction-artifact /data/clawbox-predictions/workload.json`. Updating images,
measurement semantics, or the training environment requires new matching
training artifacts. Updating only scheduling or diagnostics does not.

### Compare resident capacity

`--recipe resident-capacity` generates charges of 1.0, 0.8, and 0.75 times each
VM's configured memory, rounded up separately to MiB. It keeps both VMs resident,
uses round-robin placement, and disables Tool admission, checkpointing, and
adaptive memory controls. Charges remain until session cleanup, including model
waits. Remove unwanted arms from the generated YAML before launch.

## 4. Set arrival and execution limits

`--concurrency` specifies the number of **offered sessions**, not guaranteed
simultaneous residency. Each session needs two VMs. Memory budgets, placement,
and creation limits determine how many can run at once.

Poisson arrivals are the default. The expected arrival window is twice the
median selected trace duration after replay time scaling. Set
`--poisson-arrival-window-seconds SECONDS` for a fixed expected window, or choose
`--arrival-schedule burst` or `fixed_stagger --stagger-seconds SECONDS`.
Identical cases, concurrency, repetition, and seed produce the same arrival
sequence across policy arms. The realized Poisson window differs from its
expectation.

For a target rate of `lambda` sessions/second and `N > 1` sessions, use an
expected window of `(N - 1) / lambda` seconds. Set the arm timeout beyond arrival
and execution time. The default arm timeout is 3600 seconds.

`execution.sandbox_create_concurrency` defaults to 8 VM pairs. Preparation and
actual creation have separate limits of this size, so waiting for memory does
not occupy an actual-create slot. Tool calls have no corresponding global
single-worker limit. See [execution options](reference.md#execution) for Cube
request, lifecycle-transfer, package-install, and Runtime readiness limits.

Keep `execution.control_timing_details: false` for normal throughput runs.
Required traces, errors, resource measurements, and compact timings remain
enabled; expensive diagnostic detail is opt-in.

Keep `execution.model_request_content_retention: failures` for normal runs.
The Model Gateway still uses complete requests and responses in memory, while
successful result records retain only fingerprints, replay indices, timing,
status, retry, admission, and delivery evidence. Set it to `all` only when an
exact successful conversation audit is required. Replay rejections and failed
requests retain their exact diagnostic content in either mode. Changing this
field requires a new run ID, but no host apply, image rebuild, or retraining.

## 5. Validate, launch, and inspect

```bash
bash scripts/clawbox experiment validate "$HOME/clawbox-specs/eval.yaml" --inputs
bash scripts/clawbox experiment describe "$HOME/clawbox-specs/eval.yaml"
bash scripts/clawbox experiment plan "$HOME/clawbox-specs/eval.yaml"
bash scripts/clawbox --sudo experiment launch "$HOME/clawbox-specs/eval.yaml" \
  --run-id eval-01 --detach
bash scripts/clawbox experiment status eval-01
bash scripts/clawbox experiment collect eval-01
bash scripts/clawbox --sudo experiment report eval-01
```

`status` reads the compact run index. `collect` expands its canonical arm-result
references when the complete machine-readable result is needed; the run does
not retain another embedded copy in `summary.json`.

`validate --inputs` parses and hashes input files. Use it when preparing or
replacing inputs. Normal `launch` reuses saved identities, checks host readiness,
reuses a matching qualification receipt or qualifies, and starts the supervisor.
`--detach` returns after launch; use `status` to check the outcome.

Host readiness also projects Cube's physical CoW growth for all offered Runtime
and Tool VMs. If the projection would reach Cube's 85% storage rejection point,
launch fails before creating a VM and reports the required headroom. Remove only
verified obsolete templates, or edit `resources.cubelet_storage_root` and the
measured `resources.cube_cow_headroom_per_vm_mib`; either edit requires a new
run ID but no image rebuild.

Qualification executes a real Tool call for every case and checks required
templates and capabilities. Its default worker bound is the smallest of target
concurrency, creation concurrency, and case count. Use
`--qualification-concurrency N` to change that bound. Qualification establishes
capability and case coverage; formal runs measure performance at target load.
For custom Tool images, set `inference.configuration.openclaw_tool_home` to the
Tool user's home (default `/root`) so shell commands read the intended Git and
Conda settings. Replay requires the current Runtime image patch to avoid
unrecorded compaction requests; rebuild that image after updating the patch
and launch with a new run ID. See [workload preparation](workloads.md).

For replay, verify the [recorded tool behavior](workloads.md#import-replay-traces)
and case-specific Tool images before launch. `execution.policy_progress_timeout_seconds`
sets the total start/end acknowledgement deadline (30 seconds by default).
Use matching Runtime and Tool bridge images when changing that capability;
the deadline retries notifications only. Changes to images, replay behavior or
sampling settings require a new run ID and fresh qualification when requested
by `launch`.

An explicit `--qualification FILE` must pass the receipt coverage checks. Omit it
for automatic receipt management, or use `--force-qualify` to replace it. Receipt
reuse checks implementation, input hashes, image identities, target node, case
coverage, policy names, and required probe evidence. Re-registering the same
Runtime image can reuse its receipt; Tool sharing proofs bind exact templates.
Changing only offered concurrency or arrival timing does not require repeating
the capability smoke; `launch` decides whether to
reuse it.

For template-aware placement, prepare reusable
[physical-sharing evidence](workloads.md#prove-template-sharing). A new Tool
template requires a new proof; ordinary predictor-only changes do not.

## 6. Resume or stop a run

Repeat `launch` with the same frozen specification and run ID to resume an
interrupted run. If it is active, the command reports that state; if it already
succeeded, it prints the report. Any specification change requires a new run ID.

Plain `experiment resume RUN_ID` reuses the saved attempt and its host KB.
Completed traces are retained. Traces whose Tool filesystem or Agent progress
cannot be recovered are marked incomplete and are never replayed into learned
state. Unstarted traces may proceed. To restart all traces cold, explicitly use
`experiment resume RUN_ID --attempt-id NEW_ID`. Prior attempt evidence is retained;
resumed results identify recovered traces and cannot represent uninterrupted
throughput timing.

```bash
bash scripts/clawbox --sudo experiment launch \
  /data/clawbox-results/eval-01/experiment.yaml --run-id eval-01 --detach
bash scripts/clawbox --sudo experiment abort eval-01
bash scripts/clawbox experiment status eval-01
```

`abort` stops the supervisor and cleans up its owned VMs. After verified cleanup,
pending/running arms become `cancelled`; already-terminal outcomes are retained.
`destroy` performs
owned-VM cleanup without deleting result files. Both default to 8 concurrent
delete requests and a 180-second deadline; use `--cleanup-workers` and
`--cleanup-timeout-seconds` for a larger cleanup window. They fail if remaining
ownership cannot be verified. Retry with the same run ID after resolving the
reported service or transport error.

After an ambiguous VM-creation error, run `abort` before starting a replacement
run. Preserve the failed run directory. See [results and measurements](results.md)
for output files and timing interpretation.

## Reproduce the shared-cache placement comparison

On a host with the frozen workload's images and topology:

```bash
bash scripts/run-llc-shared-reproduction.sh --prepare-only
bash scripts/run-llc-shared-reproduction.sh
```

The first command checks inputs without starting VMs. The second qualifies and
runs both orders of the full-pool (`c0`) versus one-LLC-cluster (`c2`) comparison.
The helper reports missing image digests or topology prerequisites explicitly.
Its frozen examples retain their original VM shapes.
