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

New `host init` plans propose `cubelet_tap_init_num: 2560`, enough for the
Runtime and Tool VMs of 1280 simultaneously live sessions. Set this target to
at least twice your planned live session count for larger runs. Creating TAP
devices ahead of arrivals moves network preparation out of VM creation; it
increases service startup time. Existing YAML files and imported profiles keep
their settings; `null` leaves Cube's target unchanged. Apply a changed target
on an idle host before launching a new run ID. No image rebuild or retraining
is required.

For subsequent kunpeng runs, retain the verified creation environment: official
host kernel `6.6.0-145.1.28.165.oe2403sp1.aarch64`, the patched CubeSandbox IRQ
route batching and requested legacy IRQFD activation, compatible registered
templates, and [CubeAPI's open-file limit](#set-cubeapis-open-file-limit) of at
least 65536. Keep its display-off boot settings host-specific. CPU/memory
budgets, workload, and policies remain experiment choices.

Before timing arrivals, require the intended TAP count to be **Ready and
unowned**, as reported by Cubelet's local `/v1/network/taps` endpoint on port
9998. Setting `cubelet_tap_init_num` or completing `host apply` alone does not
prove this prewarming has finished. Keep preparation outside the timed run.
After a host-kernel change, verify snapshot compatibility; register replacement
templates from the same immutable images if required, retain old templates,
and refresh the workload's template mapping and sharing evidence. Do not replace
task-specific templates with the host profile's default Tool template.

When upgrading from ClawTune with EdgeKappaKB, first follow
[Runtime predictor and KB refresh](installation.md#refresh-the-runtime-predictor-and-kb).
Use the updated seed, refreshed Runtime template, and a new run ID; old KB
manifests containing EdgeKappa are not supported by the new ClawTune.

### Prewarm shared template RAM

Use this optional step when multiple sessions reuse the same immutable base.
Keep the SSD originals. Install the patched native release described in
[installation](installation.md); existing guest templates remain reusable.

```bash
bash scripts/clawbox --sudo experiment base-cache init base-cache.yaml \
  --spec experiment.yaml --capacity-mib 8192 \
  --template YOUR_RUNTIME_TEMPLATE --template YOUR_SELECTED_TOOL_TEMPLATE
# Review the resolved files and budget in base-cache.yaml.
# If this is a Shim-only native update, install the tested build into the
# exact vmm_binary path printed by init, while the VM pool and cache are idle:
# sudo bash scripts/install-incremental-cubesandbox.sh --shim-only BUILT_SHIM INSTALLED_SHIM
bash scripts/clawbox --sudo experiment base-cache prepare base-cache.yaml
bash scripts/clawbox --sudo experiment base-cache prepare base-cache.yaml --apply
bash scripts/clawbox --sudo experiment base-cache status --verify-bytes
```

Preparation copies each unique SSD base once to NUMA-bound, non-swapping tmpfs,
verifies its bytes and activates a fixed cache. Repeat prepare reuses an identical
cache. Keep the host idle during prepare/cleanup; do not launch VMs concurrently.
`init` reads the native XFS catalogs and does not change templates. For manually
reviewed paths, `examples/base-template-cache.yaml` shows the same plan format.
In the experiment YAML set `environment.resources.base_template_cache: required`
and use the same WARM root/node and an adequate shared-memory budget. Cached
bases count once in the existing WARM occupancy, alongside snapshots and shared
live memory. The preparation time is separate from VM arrival/creation timing.
Budget for the full RAM-file sizes of the selected unique bases, including zero
pages; allocation is completed during preparation. Unselected templates keep
using SSD. Choose the frequently reused bases that fit in WARM, leaving room
for checkpoint deltas and shared live memory.
When cases override the Tool template, select their actual template IDs; the
generic `environment.sandbox` template may not be used by any trace.

Create/restore select the WARM file only for private memory mappings. Snapshot
lineage retains the SSD identity, so COLD spill still reuses the durable base.
The cache stays fixed for the experiment; no runtime eviction is performed.
If physical template-sharing evidence was measured on cached files, regenerate
that [evidence](workloads.md#prove-template-sharing) when replacing the cache.
Use a new run ID and qualify the new native binary. After a reboot, apply the
host plan to restore WARM and rerun prepare before starting an experiment.

```bash
# After all VMs have been destroyed; preview first. SSD templates are preserved.
bash scripts/clawbox --sudo experiment base-cache cleanup
bash scripts/clawbox --sudo experiment base-cache cleanup --apply
```

If preparation was interrupted or cached files were damaged, use `cleanup --config
base-cache.yaml` to preview its owned partial files, then repeat with `--apply`
and prepare again. This never removes the source SSD templates.

For a cache-off comparison, clean up first and set
`environment.resources.base_template_cache: disabled`. Use the same new native
binary for both arms; it also buffers lineage JSON reads. Comparing an old
binary against a cached new binary would mix both changes in the result.

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

Page-local Zstd snapshot storage is enabled by default. Install or update its
host helper once after checking out the corresponding ClawBox revision:

```bash
sudo bash scripts/build-zstd-page-snapshot.sh /usr/local/bin/clawbox-zstd-page-snapshot
```

Generated specifications therefore use `snapshot_compression: zstd-page` and
`/usr/local/bin/clawbox-zstd-page-snapshot`. Set `snapshot_compression: none`
explicitly only for an uncompressed comparison, with a different run ID.
Run `experiment qualify` or ordinary `launch` after changing the encoding. No
VM image rebuild or retraining is required. Results report compression inside
checkpoint snapshot metrics and decompression inside restore metrics. Interpret
WARM savings as parked tmpfs DRAM released after raw files are removed; interpret
COLD savings as disk space, not DRAM.

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
Memory queries initially use the seed's deployment prior. After a pair commits
its first eligible guest-memory measurement, that pair queries the guest-memory
domain so its observations can affect later predictions. The seed is not
relabelled. This host-side query correction requires a new run ID; existing
guest images and the seed remain reusable.
Default logs include client prediction round-trip timings in each collected
`openclaw/SESSION/sidecar.log`; see [measurement scope](results.md). The launcher
ships this instrumentation for new runs, so it requires no image rebuild,
host apply, or retraining. Existing run logs are not retroactively instrumented.

The generated YAML exposes these settings under `execution`:

```yaml
kb_scope_policy: pair
kb_http_timeout_seconds: 30
kb_observation_retries: 2
kb_processes: 4
kb_queue_capacity: 1024
retain_clause_telemetry_artifacts: false
```

Missing training targets retain ClawTune's explicitly marked imputed zeros.
Host-live queries and updates run in `kb_processes` host child processes. A pair
stays with one owner; different owners can compute concurrently. Each owner
accepts at most `kb_queue_capacity` outstanding requests. Queue exhaustion is
reported as an error, never as a prediction fallback. The existing control port
handles idle connections asynchronously; resource-admission callbacks use a
separate thread pool so they cannot occupy KB compute processes. Journal I/O
can overlap another pair's computation, but an update is acknowledged only
after its journal is durable and its new generation is published. Host process
count changes require a new run ID, not host apply, image rebuilding or retraining.
Linux uses forkserver seed preloading; physical sharing depends on copy-on-write.
Windows loads a seed per owner. Include child processes when assessing memory.
KB owners honor `worker_cyclic_gc_enabled` and
`worker_thread_switch_interval_seconds`, just like the experiment worker.
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

For a corpus scan, set `workload.session_assignment: unique`, set
`execution.concurrency_levels` to the number of cases, and set
`execution.max_active_sessions: 32` to run at most 32 live VM pairs at once.
Every case still executes; completion releases a slot for the next case.
Set `inference.configuration.time_scale: 0` to skip recorded model waits while
preserving Tool commands. This is an accelerated workload profile, not an
end-to-end Agent latency comparison. These settings require a new run ID,
but no host apply, image rebuild, or retraining.

For simultaneous independent rollouts of each base case, use
`workload.session_assignment: grouped` and `workload.rollouts_per_case: 16`.
An ordered manifest of 80 cases requires `execution.concurrency_levels: [1280]`.
Session indices 0–15 use the first case, 16–31 the second, and so on; each
session creates its own Runtime/Tool pair and replays that case's original trace.
Set `execution.arrival_schedule: normal`, `execution.normal_arrival_std_seconds: 60`
and a fixed `execution.random_seed` to draw 80 normally distributed group arrivals,
sort them, and shift the earliest to zero. All 16 sessions in a group are offered
at exactly the same instant. Freeze the resulting 1280 offsets in
`execution.session_arrival_offsets_seconds` for comparisons across policies.
Preserve `inference.configuration.time_scale: 1.0` for original model delays.
For uncapped offered sessions, keep `execution.max_active_sessions: null` and
`mechanisms.placement.session_cpu_slots: []`; ordinary CPU/memory admission still
controls when a VM can become runnable. Grouping adds no active-group limit.
Qualification uses one rollout per case and does not establish 1280-session
performance. Use a new run ID and qualification after changing the grouping or
arrival inputs; these changes do not require host apply, image rebuild or retraining.

To keep concurrent traces on disjoint cores, set
`mechanisms.placement.session_cpu_slots` to a list such as
`[{node_id: node0, cpus: "0,2,4,6"}, {node_id: node1, cpus: "80,82,84,86"}]`.
Use CPU IDs verified from your host's `thread_siblings_list` and
`cluster_cpus_list`; these example IDs are not portable. A slot is leased for
the entire trace, and both Runtime and Tool vCPU threads use its mask. With
two slots, at most two traces run; the next trace takes whichever slot finishes.
Use managed resident C0 `round_robin` placement. Configure enough slots for the
desired concurrency and keep `execution.max_active_sessions` at or below that
count. Separate CPU clusters can still share the outer L3 cache. Use a new run
ID and qualification; apply the host configuration first if adding allowed
CPU/NUMA pools. Images and trained models do not need rebuilding.

For an LS/BE CPU QoS comparison, label each workload case with
`qos_class: ls` or `qos_class: be`, including cases supplied through a manifest.
Keep unique case IDs, trace inputs, VM resources, CPU pools, and memory policies
fixed. Compare `mechanisms.placement.cpu_qos_policy: "off"`, `idle`, and
`idle_core` in separate arms; the arm override key is
`placement.cpu_qos_policy`. Keep `tool_call_cpu_policy: c0` while isolating this
effect. The allowed CPU pool must include SMT siblings for an SMT-interference
experiment.

`idle` lowers BE Runtime and Tool host vCPU threads to `SCHED_IDLE`.
`idle_core` additionally gives LS and BE different Linux core-scheduling
cookies; all host vCPU threads in one class share its cookie. The worker
verifies these settings after VM creation and restore. Run with `--sudo`,
managed placement, and the configured experiment cgroup. `idle_core` requires
a host kernel built with `CONFIG_SCHED_CORE=y` and working `PR_SCHED_CORE`;
unsupported hosts fail rather than substituting static CPU partitions. If a
kernel change is needed, reboot and complete [host recovery](installation.md#recover-after-reboot)
before qualification.

VMs in separate CPU cgroups are also scheduled according to their group weights.
Lowering only BE vCPU thread priority does not lower the whole VM group's weight.
To compare group-level background scheduling, set
`mechanisms.placement.cpu_qos_group_idle: true` together with `idle` or
`idle_core` (arm override: `placement.cpu_qos_group_idle`). This also marks
BE Runtime and Tool VM cgroups as idle, including their helper threads, and
verifies the setting after creation and restore. It requires writable
`cpu.idle` files under the configured cgroup v2 CPU controller. The default
is `false`; keep this setting fixed across the arms of a comparison, or label
its change as a separate ablation. Use a new run ID and qualification; no
host apply, image rebuild, or retraining is needed if the CPU controller is
already enabled. Cleanup restores the original group settings.
The runtime memory controller retains memory protection for these groups,
but does not apply CPU release boosts to idle BE groups.

Prepare a separate no-BE reference containing the same LS cases. Set
`execution.session_arrival_offsets_seconds` to the frozen offered-arrival
offsets, in seconds from arm start. The list must be nondecreasing and contain
one entry per offered session, with one configured concurrency level. For the
no-BE reference, retain each LS case's original offset when removing BE cases;
do not regenerate Poisson arrivals for the smaller session count. Launch each
configuration through the validation and launch commands below with a new run
ID and qualification. These changes need no image rebuild or retraining;
host apply is needed only if the allowed CPU/NUMA pools change.

Calibrate background-load levels from `cpu_qos_usage.classes.be` in the memory
events, using complete intervals with no new or departing threads. Its load
fraction uses the configured logical CPU pool as the denominator and covers
Runtime and Tool vCPUs only. Keep the offered BE workload fixed across compared
policies and report the resulting measured load separately; an offered level
does not establish the actual CPU utilization under `SCHED_IDLE` or core scheduling.

After collecting the runs, analyze the no-BE arm first, then each mixed arm
(`RUN_ROOT` is the collected run directory; select `--arm-id` if it contains
multiple arms):

```bash
python3 scripts/analyze_cpu_qos.py BASE_RUN_ROOT --output baseline-analysis.json
python3 scripts/analyze_cpu_qos.py MIXED_RUN_ROOT \
  --baseline-json baseline-analysis.json --slo-multiplier 1.20 \
  --timeseries-bin-seconds 10 --timeseries-csv qos-timeseries.csv \
  --output qos-analysis.json
```

This checks LS Tool execution p95 against 1.20 times its no-BE reference and
reports Tool steps/s plus completed, failed, and incomplete task counts for LS,
BE, and LS+BE using the same window. Completed steps require a successful or
exactly matched expected Tool outcome; a completed step does not make its task
complete. Final validated-session step rates and useful session throughput are
also retained. Host admission-to-completion latency is reported separately.

The same analysis command works while an arm is running: it reads the frozen
specification, existing events, and session progress, marks the report incomplete,
and stops at the last observed timestamp. It does not declare the SLO satisfied
before completion. CSV rows contain 10-second intervals and cumulative task
counts; a shorter final interval is marked explicitly. Inspect failures and
arrival matching alongside p95; a finite batch does not establish steady-state
capacity. See the [analysis options](reference.md#cpu-qos-analysis) to select
an explicit measurement window or supply a frozen cohort mapping.

For a time-bounded study, record the last `session_dispatched` event's
`wall_time_ns / 1e9` as `SUBMITTED_AT`, choose `CUTOFF_AT = SUBMITTED_AT + 1200`,
and run `clawbox experiment abort RUN_ID` at that deadline. After cleanup,
analyze the retained evidence with `--observation-start-unix-s SUBMITTED_AT
--observation-end-unix-s CUTOFF_AT`. These are analysis timestamps; neither
option changes replay timing. Compare `observed_tool_steps_per_second`,
`completed_tasks_in_window`, and `observed_window_execution` across classes,
alongside nonzero exits, incomplete tasks, and timing coverage. Use the same
window rule for the no-BE reference and every policy, and label partial-run SLO
comparisons as observations rather than completed-trace guarantees.

For a four-replica placement study, configure `execution.tool_placement` with
the measured target inventory, recorded prefix sequence, and host-specific CPU
layouts described in the [option reference](reference.md#tool-placement-study).
Use one parent session slot per group, on cores outside every replica layout.
Groups reserve disjoint domains, including their parent slots. The default
`isolation_domain: llc` reserves outer LLC domains; `cluster` permits more groups
on disjoint hardware clusters that still share the upper LLC. List groups in
parent-slot order. Set `repetitions: 1` for one packed/spread pair per tool;
this gives no estimate of repeat variability.

After a run, summarize preserved trials with
`python scripts/analyze-tool-placement.py RUN_ROOT --output NEW_DIRECTORY`
(requires NumPy, SciPy and Matplotlib in the analysis environment). The output
includes raw paired timing summaries, standalone plots and memory-footprint
strata. Nonzero exits and network/environment flags remain visible; mismatched
exit outcomes are excluded from timing comparisons. Memory footprint is a proxy
and does not directly measure the cache working set.
The `clean_success` subset also excludes network-command candidates, including
common install/download commands detected in the saved command text. This
conservative screen does not prove that a download occurred.
The summary also separates profiling run IDs, so baselines collected under
different isolation settings can be reviewed separately.
Keep resident VMs and admission/runtime control off. Launch the edited YAML with
the usual `clawbox experiment launch placement.yaml --run-id UNIQUE_ID` command.
Qualification checks native snapshot forks, memory/filesystem restoration and
four-event PMU coverage; normal corpus smokes check image and SSH capability.
Every selected tool is reached through its recorded prefix. Its pre-tool snapshot
is reused for paired trials, with fresh replicas each time. Native snapshots are
released after those trials; invocation inputs, boundary/state witnesses, raw
measurements and snapshot identities remain under the run's `tool-placement`
directory. This needs a new run ID and qualification, but no image rebuild,
retraining, or host apply when the configured CPU/NUMA pools already exist.

`execution.sandbox_create_concurrency` defaults to 8 VM pairs. Preparation and
actual creation have separate limits of this size, so waiting for memory does
not occupy an actual-create slot. Tool calls have no corresponding global
single-worker limit. See [execution options](reference.md#execution) for Cube
request, lifecycle-transfer, package-install, and Runtime readiness limits.

For a pressure run that has a large preparation queue while CPU is already
saturated, reduce offered `--concurrency` first and keep arrival policy
unchanged so only one workload dimension changes. If WARM is underused because
borrowed VMs are charged at their configured maxima, enable bounded resident
accounting in the generated YAML:

```yaml
mechanisms:
  shared_memory:
    shared_memory_resident_headroom_mib: 8192
  checkpoint:
    storage: tiered
```

The 8192 MiB value is a starting safety reserve, not measured usage. The
controller uses one aggregate shared-NUMA sample, keeps a full-VM reservation
during each rebind, and spills eligible WARM generations to COLD when observed
growth consumes the reserve. Use a new run ID. No host apply, image rebuild, or
retraining is required. Confirm the chosen reserve against the run's peak WARM
growth between samples before reducing it further.

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

For repeated package downloads, set `execution.apt_cache_proxy_url` and
`execution.pypi_cache_index_url` to your reachable host caches. Explicit cache
settings apply even with `sandbox.allow_internet_access: true`; other network
access follows that policy. Omit the cache fields to use direct package access
in open VMs. Verify an actual APT update/install and pip install before a large
run. See [package network setup](installation.md#package-network-setup).
Changing these fields requires a new run ID, but no image rebuild or retraining.

For high-concurrency runs, leave `execution.worker_cyclic_gc_enabled: false`
(the default) to defer host-worker cyclic garbage collection until execution
and cleanup finish. This avoids whole-worker pauses while callbacks are due.
Reference counting remains active; unreachable cycles can retain extra host
memory until worker exit. Set the field to `true` to restore automatic cyclic
collection. Use a new run ID after changing it; host apply, image rebuild,
retraining, and template requalification are unnecessary.

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
state. Traces still waiting for arrival or a VM preparation slot may proceed
because their VM work has not started. To restart all traces cold, explicitly use
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
Failed-session cleanup cancels pending model responses and replay waits, retaining
them as errors instead of waiting for the recorded model delay to finish.
`destroy` performs
owned-VM cleanup without deleting result files. Both default to 8 concurrent
delete requests and a 180-second deadline; use `--cleanup-workers` and
`--cleanup-timeout-seconds` for a larger cleanup window. They fail if remaining
ownership cannot be verified. Retry with the same run ID after resolving the
reported service or transport error.

After an ambiguous VM-creation error, run `abort` before starting a replacement
run. Preserve the failed run directory. See [results and measurements](results.md)
for output files and timing interpretation.

## Local SSD domains and peer restore

Use two independent SSD-backed XFS pools for compute nodes `node0` and `node1`,
and the existing NUMA-bound shared-DRAM pool for WARM snapshots, base RAM and
live borrowing. The peer network transports missing immutable objects over TCP.
The destination imports the complete snapshot into its native storage domain
before restoring it. A single host with two NUMA nodes is a logical-node study;
it does not establish performance across two physical machines.

Start with the [installation workflow](installation.md), then apply the ordinary
host plan on an empty VM pool. After reboot, run
`sudo bash scripts/recover-cubesandbox-after-reboot.sh 1200` and repeat host apply
before writing WARM data. Install matching domain-capable CubeAPI, CubeMaster,
Cubelet and SDK components before preparing replicas.

Create an editable peer host plan:

```bash
bash scripts/clawbox experiment snapshot-storage init peer-host.yaml
```

Replace every `REVIEW` value with observed host facts. Set each backing filesystem
UUID, image path, mount, capacity, CPU/NUMA domain and unused namespace/address.
TCP helpers use the same declared CPU sets and local NUMA memory policy as their
logical compute node; install `numactl` before applying the plan.
Set each node's `direct_io: true` for SSD performance measurements so the outer
loop backing file cannot satisfy COLD reads from another page cache. Apply
verifies the actual loop-device setting. Reset inner SSD file residency before
matched arms as described below.
Choose capacities after counting all template RAM, PMEM images, kernel, frozen
rootfs, snapshot generations, live CoW growth and transfer staging. The pools
must remain below Cube's 85% rejection threshold, with additional free space on
each backing filesystem. `apply` preallocates the new image; it does not format
an existing disk or replace an existing image. The Kunpeng example is
`examples/snapshot-storage-kunpeng.yaml`; verify its identities and free space
before reuse.

If available capacity is inside a linear LVM XFS filesystem, first inspect its
physical allocation groups. Two arbitrary files on that filesystem do not prove
independent SSDs:

```bash
bash scripts/clawbox --sudo experiment snapshot-storage inspect-backing \
  --backing-device /dev/mapper/REVIEW > backing-inspection.json
bash scripts/clawbox --sudo experiment snapshot-storage reserve-image \
  --image /REVIEW/node0.image --capacity-mib 1572864 \
  --backing-device /dev/mapper/REVIEW --physical-device /dev/REVIEW_SSD0 \
  --allocation-group REVIEW_FIRST --allocation-group-end REVIEW_LAST \
  > node0-reservation.json
```

Review the observed free space and choose groups wholly on the intended SSD.
Repeat with a different SSD and a new image for node1. The command reserves and
initializes only new owned files; it never writes directly to filesystem blocks.
If chunk preparation stops, repeat its command with
`--resume-receipt /path/to/the/image.allocation-ID.json`; it verifies and retains
completed chunks before continuing.
Wait for both results to say `status: verified`, then add their `physical_device`
and `receipt` paths as each node's `physical_device` and
`image_allocation_receipt`. Set `backing_mount` to the actual common filesystem
mount and retain its UUID. The shared filesystem metadata/log must be reported
as a single-host limitation even though all pool payload extents are disjoint.

```bash
bash scripts/clawbox --sudo experiment snapshot-storage check peer-host.yaml
bash scripts/clawbox --sudo experiment snapshot-storage apply peer-host.yaml
bash scripts/clawbox --sudo experiment snapshot-storage qualify-network peer-host.yaml \
  --output network-qualification.json
```

The network test temporarily uses a lower rate to check that two overlapping
transfers share one directional limit, then restores the configured rate. Its
fixture timings are transport qualification, not workload performance. The
configured Mbit/s is an upper bound; report the measured rate separately.
Run this check before workload measurements. It uses two temporary 128 MiB
objects by default, verifies concurrent duplicate requests transfer an object
only once, records interface bytes, and removes its fixtures. `--output` must
name a new file; `--size-mib` changes each object's size within 16–1024 MiB.

Before preparing templates, complete the existing-host build/install sequence in
[Add two local SSD storage domains](installation.md#add-two-local-ssd-storage-domains).
It includes both the matching peer components and the patched versioned Shim:
`--peer-components` alone leaves the installed Shim unchanged. Direct COLD
checkpoints require the patched Shim's `fadvise64` seccomp permission. Install
the peer components first to preserve the complete original backup, then clean
the active base cache and use `install-incremental-cubesandbox.sh --shim-only`
with the built and installed versioned paths shown there. Keep the host idle
through installation and cache preparation; reprepare the cache after changing
the Shim because the registry records its binary identity.

Prepare the exact Runtime and case-specific Tool templates. Supply the native
control socket configured at installation and a new receipt filename:

```bash
bash scripts/clawbox --sudo experiment snapshot-storage prepare peer-host.yaml \
  --control-socket "$PEER_CONTROL_SOCKET" \
  --template "$RUNTIME_TEMPLATE" --template "$TOOL_TEMPLATE_A" --template "$TOOL_TEMPLATE_B" \
  --receipt template-preparation.json
```

Preparation defaults to one template at a time. Add `--workers 4` to prepare four
templates concurrently when importing a larger case set; immutable objects shared
by templates are copied once per SSD. This changes preparation throughput only,
outside the measured replay, and requires a new receipt filename. It does not
limit experiment sessions or change their arrivals. Repeat preparation completes
missing component version files in owned packages while preserving existing RAM
base paths used by an active base cache.

Copy the applied receipt's `storage` fields into
`environment.resources.snapshot_scheduling_storage`, add `host_receipt` and
`native_control_socket`, and match its node IDs to `compute_nodes`. Set the
experiment's WARM budget no larger than the applied pool. A global
`cold_snapshot_root` is unnecessary: each node uses its local domain. Keep
`execution.snapshot_branching` unset for ordinary replay traces.

Use `mechanisms.idle_reclaim.policy: wait` with `scope: agent_and_tool` and
`mechanisms.placement.resume_policy: home`, `load-aware`, or `cost-aware`.
All three use the same snapshot and transfer machinery. Migration is permitted
only when both roles are fully paused and no conflicting Tool claim or lifecycle
operation remains. Runtime resumes before model-response delivery; Tool resumes
on its next actual request. Cost-aware selection additionally requires measured
`restore_costs` with provenance. Calibrate before freezing the comparison; do not
change replay times or arrivals between arms.

For a shared base cache, run the existing `base-cache init --spec ... --template
... --capacity-mib ...`, review the plan, then `base-cache prepare ... --apply`.
Peer initialization resolves RAM in both domain catalogs. `aliases` maps each
template to its other SSD RAM paths; preparation verifies identical content and
registers both local identities against one charged WARM file.

Continue with `validate --inputs`, `doctor --probe-vm`, `qualify`, `launch
--qualification ... --run-id NEW_ID`, `status`, `collect`, and `report` as in the
ordinary workflow above. Qualification verifies bidirectional restore and target
backing files, WARM-to-COLD relocation, and a subsequent native checkpoint
directly into COLD followed by Runtime-first recovery. These exercise distinct
paths. Separately run an ordinary trace calibration to establish actual
pressure and migration opportunities; the short qualification replay does not
establish those conditions. Freeze replica inventory and costs before running
the policy comparison.

For a bounded comparison, set `execution.measurement_window_seconds` identically
in every arm and inspect `peer-summary.json` after `collect` and `report`.
A planned cutoff can leave the general run status `failed`/`TimeoutError`;
check that the measurement window closed at its cutoff, the peer report marks
`fixed_window_observed: true`, and VM cleanup is verified. Keep unfinished
sessions in the denominator; an early infrastructure failure is not usable as
a full-window throughput result.
The peer report counts agent-tool SSH operations, including command execution
and filesystem operations; backend-maintenance is excluded. Do not interpret
that rate as LLM tool calls or completed traces per second.

After aborting or finishing a run, stop its worker and wait for VM cleanup.
Use that run's ID as `RUN_ID`, then preview and apply replica cleanup before
the next comparison. This requires an idle VM host and no active peer transfers:

```bash
bash scripts/clawbox --sudo experiment snapshot-storage cleanup-replicas peer-host.yaml \
  --run-id "$RUN_ID" --control-socket "$PEER_CONTROL_SOCKET"
bash scripts/clawbox --sudo experiment snapshot-storage cleanup-replicas peer-host.yaml \
  --run-id "$RUN_ID" --control-socket "$PEER_CONTROL_SOCKET" --apply
```

Native references are released before deleting that run's replicas. Prepared
templates, other runs, and objects with remaining references are retained.
Record the retained template/WARM inventory when preparing the next comparison;
replica cleanup does not reset the shared base cache.

For matched cold-SSD comparisons, run cache reset after qualification and
replica cleanup, immediately before each formal arm. Preview first, then apply:

```bash
bash scripts/clawbox --sudo experiment snapshot-storage reset-cache peer-host.yaml
bash scripts/clawbox --sudo experiment snapshot-storage reset-cache peer-host.yaml --apply
```

This requires the applied owned mounts, an idle VM host and no active peer
transfers. It syncs and requests page-cache eviction only for regular files
inside the two SSD pools and their exact owned loop-image files, counting
device/inode identities once. Inner files are synced first, then the backing
images; their requested bytes and residency are reported separately. This also
removes eligible preparation remnants when formal loops use direct I/O. Shared WARM bases remain
warm; original templates outside the pools are untouched. The output reports
requested bytes and actual `mincore` resident-page counts before/after without
reading file contents. Eviction advice is not guaranteed: compare the measured
remaining residency and retained template inventories across arms before
claiming equal starting conditions. Save each command's JSON with that run's
evidence. Reset requires no image rebuild, retraining or new host apply; every
comparison arm still uses a new run ID.

When finished using the peer pools, preview and apply host cleanup:

```bash
bash scripts/clawbox --sudo experiment snapshot-storage cleanup peer-host.yaml
bash scripts/clawbox --sudo experiment snapshot-storage cleanup peer-host.yaml --apply
```

Cleanup stops owned helpers, removes their network namespaces and unmounts the
owned loop pools. It preserves images, results, original templates and the
shared-DRAM mount. Repeating `apply` recovers the saved pools and helpers, and
loads changed helper code after verifying that no peer transfer is active.
Storage identity/capacity changes require a new pool/plan; shaping changes require
apply and a new run ID. Scheduling/budget edits require a new run ID, not guest
image rebuild or ClawTune retraining. Native interface changes require matching
component installation and fresh qualification.

Historical branching results remain **Simulated Cross-Node Restore**: their
copied files were not consumed by Cube. They cannot serve as peer-restore
evidence. The former NFS host-plan schema is not accepted by this workflow.

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

## Diagnose VM creation under load

On the experiment host, start the bounded kernel and Cube log capture in one
terminal. Use a new output directory for each capture:

```bash
sudo python3 scripts/capture-creation-kernel.py \
  --output /data/clawbox-create-diagnostic-001 --seconds 3600
```

Wait for `Creation trace ready`, then run the normal `clawbox experiment launch`
command in another terminal with the frozen experiment YAML and a new run ID.
The capture records IRQFD assignment, IRQ-bypass registration/unregistration,
routing-table submission and cgroup attachment. It also copies new Cube log
bytes. It does not launch or stop experiments. Ctrl-C ends capture and removes
only its own trace instance and probes; stop the experiment separately with
`clawbox experiment abort RUN_ID` when its diagnostic window is complete.

The host needs root access and tracefs with `CONFIG_KPROBE_EVENTS`. Missing
kernel functions fail capture before the ready marker. No image rebuild or
host apply is needed. Correlate host-monotonic trace times, thread identities,
Cube sandbox IDs and `sandbox_created` spans. Kernel observations cover the
whole host and include nested calls: do not sum `kvm_irqfd_assign` and its
IRQ-bypass child. Inspect `coverage.json` for ring-buffer overruns and missed
return probes before calculating durations. In-flight calls at capture end
are incomplete observations, not zero-duration operations.

## Set CubeAPI's open-file limit

Before large concurrent runs, check CubeAPI's per-process file-descriptor limit.
A low limit can produce `Too many open files` and fail creation or updates.
After installing/configuring CubeSandbox, run on the host:

```bash
bash scripts/configure-cube-api-nofile.sh --minimum 65536
sudo bash scripts/configure-cube-api-nofile.sh --minimum 65536 --apply
```

The first command previews the change. The second preserves higher existing
limits, installs a persistent systemd drop-in, and raises the running CubeAPI
process's limit without restarting it. It does not start an inactive service.
`--minimum` is a descriptor count per process (default 65536); it must not exceed
the host's `fs.nr_open`. No image rebuild, retraining, or `host apply` is needed.
If an experiment already failed, keep its evidence and use a new run ID when
rerunning it. See the [option reference](reference.md#cubeapi-open-file-limit).
