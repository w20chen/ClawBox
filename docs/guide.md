# Run experiments

ClawBox's supported interface is `clawbox experiment`. Each session has a
**Runtime VM**, which runs OpenClaw, and a **Tool VM**, which executes commands
in the task workspace. Complete the [host installation](installation.md) before
following this walkthrough.

Start new experiments with `clawbox experiment launch --baseline B.yaml
--workload W.yaml`; use `resume` to continue a saved run. The supervisor starts
the internal experiment worker.

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

## Daily use: prepare once, check, launch

On an installed host, create a directory from a compatible baseline/workload
pair and the **explicit** machine environment used by that deployment:

```bash
bash scripts/clawbox --machine-env /absolute/machine.env \
  --output-root /data/clawbox-results experiment prepare /data/my-experiment \
  --baseline /absolute/baseline.yaml --workload /absolute/workload.yaml \
  --machine-env /absolute/machine.env --host-config /absolute/host.yaml
cd /data/my-experiment
# Review baseline.yaml and workload.yaml, then:
bash experiment.sh --sudo preflight
bash experiment.sh --sudo launch --run-id my-new-run --detach
bash experiment.sh status my-new-run
```

For kunpeng, also pass `--creation-standard` with the saved
`.artifacts/experiment-standards/kunpeng-creation-20261108/manifest.json` when
preparing. Copy that manifest to the host if it is absent there. The generated
wrapper binds its path, the checkout, machine environment, host plan, and result
root; it calls the existing CLI. It does not install services or copy credentials.
An existing preparation directory is never overwritten. Relative input paths
become absolute; per-case Tool templates, arrivals, replay times and policies
are retained. Host profiles do not silently replace experiment resources.

`preflight` checks actual Ready/unowned TAPs, CubeAPI and experiment-process
NOFILE, idle cgroup **and its lease**, disk reserve, service/template/resource
checks, input readiness, and managed GPU simulator paths/CPU affinity/ports.
It also validates a configured template-sharing proof against the exact current
Tool template set. After changing cases/templates, use `prove-template-sharing`
with matching raw `--resume-result` files to reuse prior measurements, then set
`evidence.template_base_sharing_artifact` to the new proof.
It prints all failed checks with remedies and exits nonzero. It does not create
VMs or repair the host. `--json` saves the same report for audit. `launch`
rechecks transient readiness and reuses a matching qualification receipt;
otherwise it runs the existing real-VM qualification. Qualification finishes
before `--detach` returns. Readiness does not prove Full1280 performance.

After interruption, use the same directory and run ID:

```bash
bash experiment.sh --sudo abort my-new-run  # stop/clean only this run if needed
bash experiment.sh --sudo preflight
bash experiment.sh --sudo resume my-new-run --detach
```

Resume preserves completed work according to the saved run's resume rules.
Changed experiment inputs require a new run ID. If another run owns the VM
domain, wait for it or have its owner abort it; do not remove its lock file.
After reboot use [host recover](installation.md#recover-after-reboot), then
preflight again. For a new host, complete installation first; `host init --detect`
proposes an editable plan and `host init --from-profile` reuses an explicit
saved profile. Review template identities and resources before host apply.

Before launching an edited configuration, inspect its effective settings:

```bash
bash experiment.sh validate
bash experiment.sh describe --json
bash experiment.sh resolve --output /data/my-experiment/resolved.yaml
```

Use a new destination for each `resolve`; it never overwrites a frozen snapshot.
`describe --json` includes the simulator's actual per-process CPU and port layout:
one cluster uses the configured CPU pool; multiple clusters each use one CPU,
cycling through that pool. `native` and `final` persistence currently share the
same native write/finalization behavior and are not different I/O ablations.

Inference settings now reject unknown keys and require a complete managed launch
for `serving_sim`. Remove unused `evidence.idle_reclaim_costs` and
`mechanisms.runtime.pressure_control.amortization_factor`; the context-first
policy never used them. Select the result root with `--output-root` when preparing,
instead of `measurement.output`. Put Tool network access only in baseline
`environment.sandbox.allow_internet_access`, not each case's template.
Remove a conflicting `CLAWBOX_WORKER_NODE` from machine.env; baseline
`environment.resources.target_node` is the node actually checked and used.
Replace hidden `qualification_max_parallel_sessions` with workload
`submission.max_active_sessions`, or `qualify --concurrency` for a smoke.
These edits need a new run ID, but do not by themselves need host apply, image
rebuild or retraining. Preserve archived configurations and results.
For serving_sim, removing case network fields changes the task identity in the
CPU binding manifest. Edit a copy of the original replay workload (including its
case manifest), then run `experiment prepare-serving-sim` into a new bundle as
shown in the [GPU preparation walkthrough](#connect-the-gpu-serving-simulator).
Use that bundle's new workload/manifest/catalog paths. Do not edit old binding
hashes or overwrite frozen bundles; the recorded responses and times stay intact.

## 1. Check the host

```bash
cd "$HOME/src/ClawBox"
bash scripts/clawbox experiment host check "$HOME/.config/clawbox/host.yaml"
```

After a reboot, follow [host recovery](installation.md#recover-after-reboot).
After changing the host plan, apply it on an idle VM pool and copy the verified identities and resources into the baseline. The editable
plan is `host.yaml`; `host.json` records applied settings. Neither file silently
overrides a baseline during experiment loading.

New `host init` plans propose `cube_api_nofile: 65536` and
`cubelet_tap_init_num: 2560`, enough for the
Runtime and Tool VMs of 1280 simultaneously live sessions. Set this target to
at least twice your planned live session count for larger runs. Creating TAP
devices ahead of arrivals moves network preparation out of VM creation; it
increases service startup time. Existing YAML files and imported profiles keep
their settings; `null` leaves Cube's target unchanged. Apply a changed target
on an idle host before launching a new run ID. No image rebuild or retraining
is required.

For high-concurrency lifecycle runs, set
`cube_master_db_timeout_seconds: 30` in the reviewed host plan when the
CubeMaster journal shows MySQL read/write operations crossing its default
5-second timeout. Run `host check`, then `host apply` while the Cube VM pool is
idle. Apply backs up `conf.yaml`, changes only the read/write timeouts, restarts
CubeMaster when needed, and records the value in `host.json`. This prevents an
already completed pause or restore metadata update from being reported as an
invalid connection; it does not make slow SQL faster or change experiment CPU,
memory, workload, templates, or replay timing. Use a new run ID; no image
rebuild or predictor retraining is required.

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
  --baseline baseline.yaml --workload workload.yaml --capacity-mib 8192 \
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

### Update checkpoint and restore on an existing host

On an idle standalone v0.7.0 host, prepare a new source directory with the
current patch set. Reuse the installed builder image and the existing guest
templates. Set the two storage paths below to the roots in your host plan.
After installation, set `cube_source` in your editable host plan/profile to
the new source directory so future host checks use the matching sources.

```bash
export CUBE_SOURCE_DIR="$HOME/src/CubeSandbox-lifecycle"
bash scripts/build-cubesandbox-release.sh --lifecycle-components --source "$CUBE_SOURCE_DIR" --jobs 8
sudo bash scripts/install-incremental-cubesandbox.sh --lifecycle-components \
  "$CUBE_SOURCE_DIR" "$PWD" /absolute/warm-root /absolute/cold-root
```

The installer preserves backups, updates Cubelet/Shim/runtime and the Zstd
helper, and enables managed cross-tier lineage. COLD-to-WARM reuses SSD layers;
WARM-to-COLD also shares ancestors in place: retained tmpfs pages stay in WARM,
and only newly modified pages are written to COLD. A COLD generation can therefore
depend on WARM files and cannot survive their loss. Those managed WARM layers
remain charged to the shared-memory pool for their actual allocated bytes, even
after the original snapshot package has been retired. Retiring or deleting a
checkpoint triggers a scan of both tiers. It frees pages no remaining checkpoint
or rollback record references, including pages inside shared ancestor files;
the last reference disappearing allows the managed file itself to be removed.
A running VM keeps its source checkpoint protected until it is replaced.
Reclamation runs during cleanup, not on every guest write or a background timer.
Pending publication and unregistered hardlinks conservatively retain pages.
The WARM pool refreshes freed-space accounting on its next observation (ordinary
refreshes are limited to once per second).
It requires separate WARM
tmpfs and COLD disk filesystems, no live VMs, and no active base cache. It
restarts Cubelet when its binary or settings change; rebuilding the prepared
TAP pool can take several minutes. If only Shim/runtime/helper changes, the
idle host keeps its existing Cubelet process and TAP pool.
Service startup alone is not readiness: use `experiment launch` below, which
refreshes host state and runs real VM qualification. Use a new run ID and retain
`environment.resources.snapshot_compression: zstd-page`. No guest rebuild,
kernel change or predictor retraining is needed. This update does not enable
crash rollback or peer storage; those require their matching API/Master setup.

## 2. Run a smoke experiment

An experiment has two editable inputs. The **baseline** owns VM identities,
resources, mechanisms, prediction/evidence, inference and execution controls.
The **workload** owns tasks, assignment, repetitions, arrivals, offered session
counts, active-session limits and validation. Compare methods by changing only
the baseline and keeping the same workload file.

Copy these files without overwriting existing configurations:

```bash
mkdir -p "$HOME/clawbox-specs"
cp -n examples/baselines/neutral.yaml "$HOME/clawbox-specs/baseline.yaml"
cp -n examples/workloads/smoke.yaml "$HOME/clawbox-specs/workload.yaml"
```

Edit the baseline's `environment` using actual registered templates and the
applied `host.json`. The example identities are placeholders. Set the workload's
`tasks.input`, `tasks.cases[].source_reference` and `replay_trace_reference` to
`$HOME/src/ClawBox/examples/traces/smoke.jsonl` expanded to an absolute path.
YAML does not expand shell variables. Alternatively preserve the example's
relative directory layout. Input file paths are relative to their owning YAML;
host storage paths remain explicit absolute Linux paths.
For explicit snapshot-branching cases, `tasks.cases[].source_reference` is a
scenario file relative to workload.yaml, including cases with `source: synthetic`.
The resolved run snapshot retains its absolute path for launch and resume.

```bash
cd "$HOME/clawbox-specs"
bash "$HOME/src/ClawBox/scripts/clawbox" experiment describe --baseline baseline.yaml --workload workload.yaml
bash "$HOME/src/ClawBox/scripts/clawbox" experiment validate --baseline baseline.yaml --workload workload.yaml --inputs
bash "$HOME/src/ClawBox/scripts/clawbox" --sudo experiment launch --baseline baseline.yaml --workload workload.yaml --run-id smoke-01
bash "$HOME/src/ClawBox/scripts/clawbox" --sudo experiment report smoke-01
```

The smoke creates `/workspace/result.txt` and checks its content. Template shapes
must match registered images; editing YAML does not resize or rebuild them.
`launch` uses the existing qualification and worker path. It freezes the complete
resolved settings under the run ID. Repeated launch of the same unchanged run
returns its status or resumes it; changed settings require a new run ID.

To inspect every default or save a resolved snapshot without launching VMs:

```bash
bash scripts/clawbox experiment schema baseline
bash scripts/clawbox experiment schema workload
bash scripts/clawbox experiment resolve --baseline baseline.yaml --workload workload.yaml --output resolved.yaml
```

Run those checkout-relative commands from the checkout. `resolve` refuses to
overwrite an existing output. The resolved file is execution evidence, not a
third editable input. Compare policies using separate baseline files with the
same workload.

## 3. Select tasks and compare baselines

Use [workload preparation](workloads.md) to import traces or a task manifest.
Set `tasks.source`, `tasks.input`, and optional `tasks.cases` in workload.yaml.
Explicit cases take precedence over loading the manifest. Keep per-task template
identities in `tasks.cases[].sandbox` when tasks need different images. The
baseline's `environment.sandbox` supplies defaults for cases without overrides.

Create a separate baseline per method. For C0/C2/B, copy the same reviewed
baseline and change `mechanisms.placement.tool_call_cpu_policy`. For B supply
`evidence.prediction_artifact` even when `mechanisms.admission.tool` is `off` or
`fixed`. The duration prediction and memory admission are independent consumers.
B with `off`/`fixed` accepts an unavailable memory estimate in all prediction
modes; the native model targets are retained and the projected memory estimate
is `null`. Fixed admission still reserves its configured MiB budget. Predicted
admission still requires a valid host-memory estimate. A missing duration target
skips CPU binding; invalid command identity or prediction artifacts remain errors.
Use a new run ID and qualification. For serving_sim, prepare a new bundle so its
prediction-validation receipt records the selected consumers; no host apply,
image rebuild or retraining is required for this change.
C2 and B both use `mechanisms.placement.tool_call_cpu_poll_interval_seconds`
(default 0.25 s); set it to 1 s when reproducing the historical Full C2 cadence.
Measurement frequency no longer changes this control interval.

For predicted memory admission set `mechanisms.admission.tool: predicted` and
provide an independently validated prediction artifact. For fixed admission set
`static_tool_memory_mib` in the same admission group to an explicitly calibrated
MiB value. No implicit `auto` calibration changes the baseline. The reference
contains [prediction options](reference.md#prediction-and-evidence); training
uses `experiment train --help` and requires separate training evidence.

Future WARM/tiered baselines use `mechanisms.checkpoint.residency: next-use`;
keep `lru` and `value` only for explicit comparison arms. Set storage
`tiered`, and idle reclaim `wait` or `gpu`; use `inference.backend: serving_sim`.
Both Tool-only and pair scope are supported. Prewarming is separately selected by
`mechanisms.restore.prewarm: eta` with `prewarm_lead_seconds: 2.0`;
the default `off` leaves restore reactive. The lead is an explicit restore-time
budget including margin, not an automatically measured restore cost. Native snapshot, WARM budget and measured
snapshot support prerequisites still apply; no borrow/checkpoint cost model is
used. See [option relationships](option-relations.md).

Keep `mechanisms.coordination.action_selection: context-first`. Tool admission
and active Tool progress first try to borrow shared memory for that Tool, then
may checkpoint one safe idle model-wait Session. Periodic/model-wait pressure
uses the reverse order. Each path tries each action at most once, and an active
requester is excluded from checkpoint fallback. Changing either residency or
coordination policy requires a new run ID, but no image rebuild or retraining.

Retain the verified kunpeng creation environment unless studying a component of
it. The historical creation reference is provenance, not a loadable new input.
Transcribe its verified environment and selected mechanism values into a baseline;
do not overwrite that reference or regenerate its templates merely for this change.

## 4. Set arrival and execution limits

To publish ClawTune prediction and Session signals to an existing receiver,
first [refresh the pinned Runtime predictor](installation.md#refresh-the-runtime-predictor-and-kb).
Then add this group to baseline `execution`, replacing the example address with
an HTTP(S) receiver reachable from the Runtime VMs:

```yaml
execution:
  clawtune_signals:
    admission_eta: true
    endpoint: http://192.0.2.10:9000/v1/signals
    token_env: CLAWBOX_SIGNAL_RECEIVER_TOKEN
    tool_events: true
    queue_size: 1024
    timeout_seconds: 1.0
```

Set the named token environment variable in the shell running the experiment;
omit `token_env` if the receiver requires no credential. The secret is passed
to Runtime VMs and is not written into the frozen experiment configuration.
ClawBox permits the configured receiver destination in Runtime network rules.
Run the normal `experiment validate --baseline baseline.yaml --workload workload.yaml --inputs`,
qualification and launch sequence with a new run ID. Changing only signal
settings needs no host apply, image rebuild or retraining after the updated
Runtime is installed. An unset endpoint disables external publication.

`admission_eta` (default `true`) includes host admission waiting in Session ETA
for host-live shell tools. Install the updated Runtime adapter and a Tool image
whose bridge supports execution progress events; qualification checks this and
reports when an image rebuild is needed. It records policy HTTP arrival through
the Tool bridge's execution-start notification, including worker queueing,
memory/CPU admission, dependency-install limits, VM restoration and SSH startup.
Only after that boundary does it subtract elapsed execution time from the KB
duration. The start notification follows guest payload release, so its delivery
latency remains a timing error; it is not a synchronized guest timestamp.

Waiting estimates learn online within the current host policy domain, comparing
queue depth/age, active workload, measured memory pressure, requested memory,
restoration and dependency limits. Ongoing waits count as censored observations;
they are not zero-duration successes. At least three comparable completed waits
are required. Cold starts, unseen requests, changed conditions and expired
evidence produce `remaining_ms: null`, not a zero wait. The adapter refreshes
once per second and expires host evidence after two seconds. It does not assume
that finishing a command frees the VM's memory. Tools allowed to return a
background handle also have unknown Session ETA: process duration is not their
logical return time. No KB retraining or host apply
is required; use a new run ID. Setting `admission_eta: false` disables this host
correction and uses upstream ClawTune's execution-only estimate.

The receiver must accept `clawtune.signal.v1`; see
[ClawTune's signal contract and receiver rules](https://github.com/w20chen/ClawTune/blob/main/docs/signals.md).
Session previews query LatticeKB then ToolKB without reserving an execution
or learning a sample. Formal tool decisions retain all three independent model
branches. Cube's SSH execution path supplies logical tool/model lifecycle and
Session observations; it does not supply ClawTune managed-launcher process
progress or fabricate `execution.finishing_soon`. Host-live non-shell calls
have explicit unavailable predictions. Signals are best effort, and Session
forecasts may be null; they do not automatically enable a GPU or KV scheduling
policy. Existing serving_sim lifecycle signals keep their separate meaning.

Session snapshots also carry direct active children and the parent wait set after
`sessions_yield`. One-shot completion delivery is `any`; uncertain thread-bound
delivery remains `unknown`. ClawBox preserves the logical Session, Session key,
Agent, run and tool-call identities through Tool admission and result artifacts,
so a CPU policy can use these facts without changing ClawTune. The current
serving_sim request still belongs to the physical ClawBox pair: OpenClaw does not
put a trustworthy logical Session identifier on the model HTTP request, so
ClawBox does not infer a Subagent GPU identity from arrival order.

For native OpenClaw subagents, merge the following into the baseline, retaining
your reviewed host/template configuration, API endpoint/model and validated KB seed:

```yaml
agent:
  driver: openclaw
  subagents: true
  subagent_max_concurrent: 8
  subagent_max_spawn_depth: 1
inference:
  backend: api
  configuration:
    qualification_case_limit: 1
conditions:
  prediction_source: host-live
mechanisms:
  idle_reclaim:
    scope: tool
```

Use the existing `experiment qualify` and `experiment run` commands below with
a new run ID. API qualification executes the selected complete live tasks and
uses provider credentials; choose a small representative case that actually
spawns children. Replay and serving_sim inputs describe one recorded trajectory
and cannot enable this option. Refresh the Runtime predictor/plugin and host
ClawBox together before qualification; no KB retraining or host apply is needed.

This starts OpenClaw's native Gateway inside the same Runtime VM; the ordinary
default remains embedded `--local`. Parent and child Sessions share the VM pair
and Tool workspace, but retain independent call identities and forecasts. The
Runtime stays resident; Tool admission and idle reclamation still apply. A child
Session's next-LLM ETA is not its completion time or the parent's resume time.
The driver waits for child cleanup and all observed Sessions to finish before
destroying the pair. `final-answer.json` contains `initial_root_response` and
`shared_sessions`; later parent replies remain in the collected Runtime traces.
Completion timing includes a five-second stable-idle check and polling/transport.
The result does not by itself prove that the model chose to spawn children.

`submission.concurrency_levels` specifies the number of **offered sessions**, not guaranteed
simultaneous residency. Each offered root session needs two VMs; its subagents
share that pair and do not add offered sessions. Memory budgets, placement,
and creation limits determine how many can run at once.

Poisson arrivals are the default. The expected arrival window is twice the
median selected trace duration after replay time scaling. Set
`submission.poisson_arrival_window_seconds` for a fixed expected window, or choose
`submission.arrival_schedule: burst` or `fixed_stagger` with
`submission.stagger_interval_seconds`.
Identical cases, concurrency, repetition, and seed produce the same arrival
sequence across policy arms. The realized Poisson window differs from its
expectation.

For a target rate of `lambda` sessions/second and `N > 1` sessions, use an
expected window of `(N - 1) / lambda` seconds. Set the arm timeout beyond arrival
and execution time. The default arm timeout is 3600 seconds.

`execution.dependency_install_concurrency` belongs to the baseline and defaults
to `null` (unlimited). An explicitly saved positive limit is preserved. A small
limit can serialize package installation independently of VM or Tool admission;
`describe` shows the effective setting.

For a corpus scan, set `tasks.session_assignment: unique`, set
`submission.concurrency_levels` to the number of cases, and set
`submission.max_active_sessions: 32` to run at most 32 live VM pairs at once.
Every case still executes; completion releases a slot for the next case.
Set `inference.configuration.time_scale: 0` to skip recorded model waits while
preserving Tool commands. This is an accelerated workload profile, not an
end-to-end Agent latency comparison. These settings require a new run ID,
but no host apply, image rebuild, or retraining.

For simultaneous independent rollouts of each base case, use
`tasks.session_assignment: grouped` and `tasks.rollouts_per_case: 16`.
An ordered manifest of 80 cases requires `submission.concurrency_levels: [1280]`.
Session indices 0–15 use the first case, 16–31 the second, and so on; each
session creates its own Runtime/Tool pair and replays that case's original trace.
Set `submission.arrival_schedule: normal`, `submission.normal_arrival_std_seconds: 60`
and a fixed `submission.random_seed` to draw 80 normally distributed group arrivals,
sort them, and shift the earliest to zero. All 16 sessions in a group are offered
at exactly the same instant. Freeze the resulting 1280 offsets in
`submission.session_arrival_offsets_seconds` for comparisons across policies.
Preserve `inference.configuration.time_scale: 1.0` for original model delays.
For uncapped offered sessions, keep `submission.max_active_sessions: null` and
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
desired concurrency and keep `submission.max_active_sessions` at or below that
count. Separate CPU clusters can still share the outer L3 cache. Use a new run
ID and qualification; apply the host configuration first if adding allowed
CPU/NUMA pools. Images and trained models do not need rebuilding.

For an LS/BE CPU QoS comparison, label each workload case with
`qos_class: ls` or `qos_class: be`, including cases supplied through a manifest.
Keep unique case IDs, trace inputs, VM resources, CPU pools, and memory policies
fixed. Compare `mechanisms.placement.cpu_qos_policy: "off"`, `idle`, and
`idle_core` in separate baseline files. Keep `tool_call_cpu_policy: c0` while isolating this
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
`idle_core`. This also marks
BE Runtime and Tool VM cgroups as idle, including their helper threads, and
verifies the setting after creation and restore. It requires writable
`cpu.idle` files under the configured cgroup v2 CPU controller. The default
is `false`; keep this setting fixed across the arms of a comparison, or label
its change as a separate ablation. Use a new run ID and qualification; no
host apply, image rebuild, or retraining is needed if the CPU controller is
already enabled. Cleanup restores the original group settings.
Policy verification reads `cpu.idle`; the reported `cpu.weight` is diagnostic
and can differ between kernels (for example, 0 or 1 for an idle group).
The runtime memory controller retains memory protection for these groups,
but does not apply CPU release boosts to idle BE groups.

Prepare a separate no-BE reference containing the same LS cases. Set
`submission.session_arrival_offsets_seconds` to the frozen offered-arrival
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
saturated, reduce offered `submission.concurrency_levels` first and keep arrival policy
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

Normal Full experiments should also keep the content and Runtime-log defaults:

```yaml
execution:
  runtime_trace_content_retention: failures
  runtime_log_retention: failures
  retain_standalone_pmu_artifacts: false
  retain_tool_resource_timelines: false
  prediction_record_detail: compact
  memory_log_interval_seconds: 10
```

This retains structured span identities, scheduling/admission evidence,
cgroup-resource evidence with embedded PMU data, replay-completeness fields,
failed/timeout Tool output, 10-second host resource/queue trends, prediction
values/errors/coverage/provenance, and compact host-KB client timing. It omits raw hook
payloads, complete successful LLM and Tool bodies, duplicate standalone PMU
JSON, per-poll Tool timelines, repeated full prediction models/commands, and
full logs from successful Runtime sessions. The internal 0.2-second sampler
still drives peaks, scheduling, watermarks, and simulator feedback. Agent failure, replay
invalidity, or a failed Tool call retains the full Runtime logs automatically.
For a directed full audit, set both retention fields to `all`,
`retain_tool_resource_timelines: true`, and `prediction_record_detail: all`.
Changing these
settings requires a new run ID, but no host apply, image rebuild, or retraining;
rebuild the Runtime package/template only when its packaged ClawTune source was
changed.

Keep `execution.model_request_content_retention: failures` for normal runs.
The Model Gateway still uses complete requests and responses in memory, while
successful result records retain only fingerprints, replay indices, timing,
status, retry, admission, and delivery evidence. Set it to `all` only when an
exact successful conversation audit is required. Replay rejections and failed
requests retain their exact diagnostic content in either mode. Changing this
field requires a new run ID, but no host apply, image rebuild, or retraining.

For a small, directed snapshot-page access study, add an explicit profiling
block and launch through the same experiment command:

```yaml
execution:
  snapshot_page_profile:
    case_ids: [tobymao__sqlglot-3842]
    roles: [runtime, tool]
    interval_seconds: 2.0
    max_windows_per_generation: 1800
    force_model_wait_checkpoints: true
    checkpoint_tiers: [warm, cold, warm]
```

Run one session at a time so another VM cannot reference the same shared Base
PFNs during a page-idle window. The forced option uses real model waits and the
ordinary pair checkpoint/restore path, but deliberately changes snapshot timing;
do not compare its throughput with a formal run. Leave the whole block absent
for normal experiments. Launch must use `--sudo`; validation fails before VM
arrival when the kernel page-idle bitmap or pagemap access is unavailable. The
result's `page-access` directory contains compressed exact per-page
`accessed_windows` and `present_windows` arrays plus the lineage/range map.

To run task-independent GPU-event page preparation, add:

```yaml
execution:
  snapshot_page_management:
    mode: gpu_event
    candidate_source: shared_lineage
    trigger_event: prefill_start
    prefetch_budget_mib: 1024
    per_vm_prefetch_mib: 32
    max_inflight_mib: 128
    io_workers: 2
    candidate_chunk_kib: 128
    batch_window_milliseconds: 25
    prefetch_bandwidth_mib_s: 256
    max_ranges_per_vm: 512
    signal_max_age_seconds: 5
```

No case list, workload name, access profile, or generated hotset is used. The
controller resolves each VM's current incremental lineage newest-first, removes
ancestor page versions overwritten by a newer layer, and batches imminent COLD
restores for 25 ms. Physical ranges are ranked by the number of waiting VMs that
reference the same device/inode/offset divided by bytes newly read. Equal-value
ranges use a deterministic hash order, avoiding a file-prefix bias. One physical
read can therefore prepare all waiters sharing an immutable ancestor, while
private deltas naturally rank lower.

This mechanism expands the ordinary zstd-page snapshot early, preserves its
compression-reuse manifest, and reads the bounded selection into the Linux page
cache. WARM accounting and lifecycle state remain unchanged; demand restore
cancels that VM's remaining benefit. `per_vm_prefetch_mib` limits bytes selected
for each waiter; `prefetch_budget_mib` and `max_inflight_mib` bound concurrent
working set and reads. Use a new run ID. This requires
`serving_sim`, snapshot pause and incremental-COW COLD/tiered storage. Wait/GPU
selection, either scope and compressed/uncompressed snapshots are supported.
No image rebuild or host apply is required.
Leave the block absent for the baseline and normal experiments.

For preparation that survives a GPU event arriving before checkpoint completion,
use the following block in a copy of the same baseline. Keep the workload and
host budgets fixed, then use the resolve/validate/launch sequence below with a
new run ID. Full-VM prewarm remains a separate choice.

```yaml
mechanisms:
  restore:
    prewarm: 'off'
    prewarm_lead_seconds: 2.0
    promotion: 'off'
execution:
  snapshot_page_management:
    mode: state_driven
    runtime_trigger_event: prefill_start
    tool_trigger_event: first_token
    candidate_source: shared_lineage
    prefetch_budget_mib: 1024
    expansion_budget_mib: 1024
    per_vm_prefetch_mib: 32
    max_inflight_mib: 128
    io_workers: 2
```

The state-driven mode prepares data while the VM remains paused. It remembers
accepted phase boundaries until the actual model wait ends and rechecks when
the checkpoint commits. Runtime and Tool use separate boundaries; choose
`tool_trigger_event: disabled` for a Runtime-only preparation arm. Selected
`prewarm: eta` cancels preparation when a fresh response forecast enters the
configured restore lead time and full VM restoration starts.
`promotion: off` avoids whole-lineage copying on demand; `if_fit` explicitly
reproduces the earlier next-use promotion behavior and also works with LRU.

`expansion_budget_mib` limits outstanding prepared expansion allowances; read
budgets do not pin or bound Linux's total page cache. Compare demand-only and
state-driven arms with the same physical memory/CPU budget and normal cache
behavior. Check preparation completion/cancellation, response-delivery wait,
first Tool completion and throughput. Application prefetch bytes alone do not
prove SSD I/O savings. These changes require a new run ID, but no host apply,
guest rebuild or ClawTune retraining.

For a source-of-benefit experiment that must not depend on incidental memory
pressure, set `execution.snapshot_checkpoint_gate` identically in a demand arm
and a preparation arm. Select the same `case_ids` and finite `checkpoint_tiers`;
leave `snapshot_page_management` null only in the demand arm. Run counterbalanced
AB/BA order, use `snapshot-storage reset-cache --apply` before every arm, and
compare matching COLD generations. This gate intentionally changes when selected
sessions checkpoint, so it isolates preparation/decompression from the demand
path but is not production-throughput evidence.

## 5. Validate, launch, and inspect

In commands below, `experiment.yaml` is the output of `resolve`. Alternatively
replace that positional input with `--baseline baseline.yaml --workload workload.yaml`.

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

Under a large pause/restore wave, Cube may transiently reject an idempotent
sandbox resume while its state transition settles. The baseline field
`execution.cube_request_timeout_seconds` (seconds, default 130) bounds both an
individual Cube HTTP request and the total resume retry window. Increase it only
when retained logs show a longer transient transition; a change requires a new
run ID, not host apply, image rebuild or predictor retraining. Permanent resume
failure still fails the session after this budget.

Qualification executes the first model step and its real Tool calls for up to
16 cases, selected at evenly spaced positions in workload order (including both
ends). It validates all input cases and keeps the required snapshot, placement,
borrowing and template-sharing probes. This sample proves basic capability,
not execution of every template or performance at target load. Its default
worker bound is the smallest of target concurrency, creation concurrency and
sample count. Use `--qualification-concurrency N` to change simultaneous sessions.
Set `inference.configuration.qualification_case_limit` to change sample size;
set it at least as large as the corpus to execute every case. Managed simulator
qualification uses one cluster by default; set
`inference.configuration.qualification_simulator_cluster_count` to test more
(capped by the formal cluster count). Formal startup still verifies every
configured cluster before admitting VMs. These options require a matching new
receipt and a new run ID when editing a frozen spec, but no host apply, image
rebuild or retraining. Full-corpus template-sharing proofs remain reusable.
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
reuse checks implementation, input hashes, image identities, target node, source
case inventory, sampled case coverage, policy names, and required probe evidence. Re-registering the same
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
  /data/clawbox-results/eval-01/experiment.yaml --run-id eval-01 \
  --cleanup-timeout-seconds 180 --detach
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

`launch`, lower-level `run`, and `resume` also accept
`--cleanup-timeout-seconds` (seconds, default 180). This is the supervisor's
deadline for final owned-VM cleanup; it does not change workload timing or VM
lifetimes. Set a larger value for dense runs when the host cannot destroy every
owned VM within 180 seconds. A new value affects only that invocation and does
not require host apply, image/template rebuild, retraining, or a new run ID.

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
an existing disk or replace an existing image. The generated `peer-host.yaml`
contains the editable fields; fill in the actual filesystem identities and
available capacity before applying it.

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

For a shared base cache, run `experiment base-cache init base-cache.yaml
--baseline baseline.yaml --workload workload.yaml --template ID --capacity-mib N`,
review the plan, then `experiment base-cache prepare base-cache.yaml --apply`.
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

For a bounded comparison, set `submission.measurement_window_seconds` identically
in every arm and inspect `peer-summary.json` after `collect` and `report`.
A planned cutoff can leave the general run status `failed`/`TimeoutError`;
check that the measurement window closed at its cutoff, the peer report marks
`fixed_window_observed: true`, and VM cleanup is verified. Keep unfinished
sessions in the denominator; an early infrastructure failure is not usable as
a full-window throughput result.
The peer report counts agent-tool SSH operations, including command execution
and filesystem operations; backend-maintenance is excluded. Do not interpret
that rate as LLM tool calls or completed traces per second.
VM CPU samples count increments after the first verified cgroup observation;
pooled counters can contain earlier usage. Missing or reset counter intervals
are unknown, recorded as `unobserved_counter_intervals` in `peer_cpu_sample`
events and `memory_sample.peer_cpu`, rather than zero CPU.
Audit older per-node CPU results before comparison if their recorder did not
establish this baseline.

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

The preferred persistent configuration is `cube_api_nofile: 65536` in
`host.yaml`. `host check` previews it; `host apply` on an idle host invokes the
same helper below and verifies the running process. Higher limits are retained.
Set `CLAWBOX_NOFILE_LIMIT=65536` in the selected `machine.env` separately for
the experiment process. Full1280 preflight verifies both actual soft limits.

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

## Connect the GPU serving simulator

Start from a working baseline/workload pair and its verified host profile and
per-case templates. Keep the kunpeng creation baseline. The simulator runs on
ordinary CPUs; its native environment must independently pass the adapter's `check` on
the chosen host architecture before a joint run. A service that starts without
passing the simulator's native checks is not ready for this experiment.

Copy the CPU baseline before changing inference settings. Saved template IDs
remain explicit; loading a baseline never imports a different host profile.

Prepare the bundle offline on a host with the selected traces and prediction artifacts.
Both the existing workload's schema-6 CPU replay and
the original format-5 source are needed: schema 6 alone has no reliable recorded
completion/reasoning token counts. Explicit saved `source_trace` metadata or the
workload's separate `source_reference` supplies the original path. Otherwise
create `sources.json` with `{ "CASE_ID": "/absolute/original.trace.jsonl" }`.

```bash
clawbox experiment prepare-serving-sim --baseline cpu.yaml --workload workload.yaml --source-map sources.json --output /absolute/gpu-bundle
```

In the new workload set `tasks.source: swe_rebench`,
`tasks.input: /absolute/gpu-bundle/workload.json`, and clear `tasks.cases` so the
manifest is loaded. In joint.yaml set `inference.backend: serving_sim`, and under
`inference.configuration` set `model`, `base_url`, `api_key_env` and
`serving_sim_catalog_manifest` to the verified simulator and bundle values.
Choose idle reclaim and prewarm independently; GPU reclaim requires serving_sim.

```bash
clawbox experiment validate --baseline joint.yaml --workload workload.yaml --inputs
```

Leave `CLAWBOX_SIM_API_KEY` unset for a local simulator without authentication,
or set it to the simulator's configured key. Remove any provider-specific
`inference.configuration.clawtune_config` from this spec, since that field
overrides direct model and endpoint settings.

If you select a different set of cases, its reusable template-sharing proof must
match that exact Tool template set. Set `evidence.template_base_sharing_artifact`
to `null` to let qualification verify the selected templates with real VMs.

The CPU runtime needs only `workload.json` and `manifest.json`; these can be copied
to its host without either input trace or the GPU catalog. The GPU service needs
`catalog.json` and `traces/`. `gpu-provenance.json` records offline source identities.
The CPU manifest contains task bindings, total call counts, background-execution
settings, arrival-reference durations and prediction-validation identities, but no
recorded conversations or per-call model durations. Preparation validates recorded
Tool command coverage against the selected prediction artifacts; changing those
artifacts or a task requires a new bundle. Runtime validation verifies the CPU
manifest, and the connected service must report its matching GPU catalog digest.

The bundle keeps the CPU replay's adapted tools and workspace paths, original
output lengths and finish reasons, and the action-ID mapping. It never replaces
unknown reasoning counts with zero. Recorded source-model outputs evaluated with
a different simulated model are a workload prediction, not model accuracy or
original-model performance evidence. Use a new bundle directory for changed
inputs; repeat preparation with an existing output fails without overwriting it.

Keep the GPU repository unmodified. Run the ClawBox-owned launcher from the
ClawBox checkout (or use the installed `clawbox-serving-sim` command). It imports
the original simulator application, adds ClawBox management routes, and attaches
current-request process-identity metadata to chat responses. Native token timing,
model outputs and per-session signals remain owned by the simulator. ARM dependency selection,
tokenizer-cache reuse and `GLOO_SOCKET_IFNAME=lo` belong to this launcher;
an explicit interface setting is preserved. No upstream source patch is applied.
Reserve its actual CPU budget separately from experiment VM CPUs.
`--cores` bounds real simulator CPUs; `--topology` selects simulated GPUs.

For sim-release `ab16142d9835052d5966f3c19679ce6d22789004`, upgrade using a new
GPU checkout and a separate simulator environment; retain the previous checkout,
environment and run outputs. Clone on Linux to preserve native build scripts:

```bash
git clone --branch sim-release https://github.com/HCAHOI/agent-sched-bench-gpu.git /absolute/agent-sched-bench-gpu
git -C /absolute/agent-sched-bench-gpu checkout --detach ab16142d9835052d5966f3c19679ce6d22789004
export SIM_VENV=/absolute/simulator-venv
python3 -m clawbox.integrations.serving_sim setup --sim-root /absolute/agent-sched-bench-gpu --cores 4
python3 -m clawbox.integrations.serving_sim check --sim-root /absolute/agent-sched-bench-gpu --cores 4
```

Use your authorized Git transport for this repository. Setup builds the new
Rust simulator extension and the native shims. Existing pinned tokenizer caches
can be reused. Point the new experiment at this checkout/environment and use a
new run ID with fresh qualification; the simulator upgrade alone needs no VM
image rebuild, host apply or retraining.

This release changes the standard `8A` GPU topology: shared-prefix checkpoints
are enabled by default. Record the new simulator revision and topology digest;
do not mix its results with an older GPU baseline. The Mooncake native shim also
changed, so run `setup` for the new checkout before `check` (a Python-only source
update is insufficient). Existing OpenClaw bundles remain usable. A TraceLab
synthetic source must contain `history_tokens` for every model call; if absent,
regenerate the source with current sim-release and prepare a new bundle instead
of filling missing history with zero. The OpenClaw importer does not import
TraceLab synthetic workloads.

ClawBox keeps actual Agent prompts for prefill/KV simulation and normal `k=1`
pacing. It does not enable the upstream trace-prompt or accelerated warm-up
modes, which would change these experiment inputs. Upstream many-cluster replay
capacity results do not establish this workflow's capacity.

For automatic GPU startup and finalization, add the following to the existing
experiment YAML. Use verified local paths and CPU IDs reserved outside the VM
compute sets; the values below are examples. Keep `base_url` local and choose an
unused port. The simulator environment must already be installed with `setup`.

```yaml
inference:
  backend: serving_sim
  configuration:
    model: qwen36-r2
    base_url: http://127.0.0.1:8000
    api_key_env: CLAWBOX_SIM_API_KEY
    serving_sim_catalog_manifest: /absolute/gpu-bundle/manifest.json
    serving_sim_diagnostic_interval_seconds: 1.0
    serving_sim_diagnostic_detail: compact
    serving_sim_keepalive_expiry_seconds: 2.0
    serving_sim_launch:
      sim_root: /absolute/agent-sched-bench-gpu
      venv: /absolute/simulator-venv
      trace_catalog: /absolute/gpu-bundle/catalog.json
      tokenizer_dir: /absolute/downloaded-tokenizer-snapshot
      cache_dir: /absolute/existing-simulator-cache
      hf_hub_cache: /absolute/huggingface/hub
      cpu_affinity: [240, 241, 242, 243]
      cores: 4
      topology: 8A
      decode_time_scale: null
      tpot_ms: null
      cpu_refill_mode: off
      cpu_refill_feedback_interval_seconds: 1.0
      cpu_refill_activation_floor_utilization: 0.0
      cpu_refill_enter_utilization: 0.90
      cpu_refill_exit_utilization: 0.95
      cpu_refill_enter_samples: 2
      cpu_refill_stale_seconds: 3.0
      cpu_refill_throttle_steps: 3
      cpu_refill_release_steps: 1
      cluster_count: 1
      routing: sticky_balanced_by_case
      persistence: final
      nofile: 65536
      startup_timeout_seconds: 180
      shutdown_timeout_seconds: 720
```

Leave both speed fields `null` for calibrated timing. To study a different GPU
decode speed explicitly, set **one** field: `decode_time_scale: 2.0` multiplies
the decode part of each step by two; `tpot_ms: 20.0` targets 20 milliseconds per
output token. Both require positive finite values and use the upstream hybrid
step model. Prefill, block copy/zero work, queueing and transport still contribute
latency, so `tpot_ms` is not an exact client-observed token interval or whole-call
duration. Both are non-physical settings, recorded in service metadata and native
`validity.json`; they are not calibrated hardware predictions. Manual `serve`
accepts the matching `--decode-time-scale` or `--tpot-ms` option. A speed change
requires a new run ID and qualification, but no host apply, VM image rebuild or
retraining. Keep the same explicit speed setting across compared arms.

`cpu_refill_mode: decode-first` enables the experimental CPU-aware GPU policy;
the default `off` path uses the native simulator scheduler unchanged. Once per
configured feedback interval, ClawBox sends every cluster the same actual VM
cgroup CPU sample and Tool activity/admission snapshot. Two consecutive samples
between the configured activation floor (zero by default) and 90% CPU enter
refill mode. CPU below the activation floor, at or above 95%, or feedback older
than three seconds restores native scheduling. A nonzero floor keeps cold-start
prefills native until the CPU/Tool pipeline has formed. Ready and resource-blocked Tool
counts remain recorded evidence but do not globally veto measured CPU headroom.
For an engine with both decode and prefill candidates, refill mode calls vLLM's
native prefill-throttle option for three scheduling steps, then allows one native
release step so prefills continue to progress. Pure-decode and prefill-only steps
remain native.

The feedback endpoint shares the simulator listener and API-key authentication.
Effective settings, feedback transitions, active time, throttle/release counts,
affected engines and batch composition are retained in the service receipt and
native `validity.json`. Compare `off` and `decode-first` with identical settings;
do not tune thresholds separately by arm. A policy or threshold change requires
a new run ID and a bounded simulator smoke, but no host apply, guest image rebuild
or ClawTune retraining. Manual `serve` exposes matching `--cpu-refill-*` options.

Current sim-release records lag measurements without the former fixed-threshold
warning verdict. Inspect `lag` in each cluster's `validity.json` and the retained
`correctness.gpu_validity` reports; successful finalization alone does not establish
timing fidelity.

With `serving_sim_launch`, run `clawbox experiment qualify` and then
`clawbox experiment run` as below; skip the manual `serve` commands and manual
shutdown. Each qualification/formal arm gets a fresh service and output under
its `gpu-service/` artifact directory. CPU failure and timeout still close every
service and check every final report. A single cluster keeps
`gpu-service/output` and `gpu-service/service.log`; a multi-cluster pool uses
`gpu-service/clusters/000/`, `001/`, and so on. Inspect the aggregate
`gpu-service/process.json` plus the affected cluster log on failure. Use
`clawbox experiment abort RUN_ID` for a
running experiment and `clawbox experiment destroy RUN_ID` after an
interruption; preserve failed evidence and use a new run ID, since a partially
consumed GPU service cannot resume. After reboot, restore and verify the saved
host configuration before starting the new run. External services remain
manually managed when `serving_sim_launch` is absent.

To approximate an effectively unconstrained LLM tier, launch independent native
clusters instead of changing one cluster's topology. For example, 16 independent
`8A` clusters expose 128 A-role engines:

```yaml
    base_url: http://127.0.0.1:18096
    serving_sim_launch:
      # Keep the path/cache fields from the managed example above.
      cpu_affinity: [240, 241, 242, 243, 244, 245, 246, 247]
      cores: 8
      topology: 8A
      cluster_count: 16
      routing: sticky_balanced_by_case
      startup_timeout_seconds: 600
```

`base_url` is the first of 16 consecutive local ports. `cores` is the total real
CPU budget for the pool: cluster 0 uses the first CPU, cluster 1 the second, and
clusters wrap round-robin when `cluster_count` exceeds `cores`. The topology is
still per cluster; do not replace the pool with one `16x8A` topology. Cases are
sorted deterministically, each unique trace is assigned to one cluster, and all
rollouts of that trace remain there. An explicitly grouped 80-base × 16-rollout
workload gives five base traces per cluster. `concurrency: 1280` alone does not
select that grouping: round-robin assignment repeats the configured corpus.
All signal streams are
consumed and logged with `cluster_id`; one failed stream or incomplete native
close invalidates the arm.

Before using 16 clusters, reserve the CPU set outside VM compute CPUs, verify all
16 ports are unused, and leave memory for independent simulator processes. A
practical starting reserve is 32--48 GiB (warmed processes account for roughly
20 GiB before workload growth). Changing `cluster_count`, routing, ports, or CPU
placement requires qualification and a new run ID. It does not require host
apply, VM image rebuild or retraining unless the reserved host CPU/service limits
themselves change. Multi-cluster cold startup may need a larger
`startup_timeout_seconds` (600 seconds in the example); this deadline is outside
workload execution.

Reserve the listener ports against automatic ephemeral source-port allocation
before starting the pool, especially on hosts whose ephemeral range begins below
the simulator port. From the checkout on the Linux host:

```bash
python3 scripts/configure-simulator-ports.py --base-port 18096 --cluster-count 16
sudo python3 scripts/configure-simulator-ports.py --base-port 18096 --cluster-count 16 --apply
```

The first command previews the merged range. Apply preserves existing reserved
ports, updates the live sysctl and saves `/etc/sysctl.d/99-clawbox-simulator-ports.conf`
for reboot. Repeat after changing the range. Reservations prevent automatic source
port allocation, not another service explicitly binding those ports. No image
rebuild or retraining is needed. The pool still checks that its ports are free.

Both `persistence: final` and `persistence: native` retain the current simulator's
incremental input flushes and step-memory release; summaries are written at
startup and graceful shutdown. Preserve the final `realized.json` with its
`realized-inputs.jsonl` and `realized-tokens.bin` sidecars. A finalized report
does not imply valid timing: inspect `validity.json` separately.

For lower logging overhead use `serving_sim_diagnostic_detail: compact` and a
process sampling interval such as 5 seconds. Request stage records still contain
every request ID and submit/header/first-SSE/terminal/DONE times. Under GPU-aware
reclaim, signal records keep every phase/decision edge plus a global one-second
summary of repeated queued updates; result metrics retain accepted and suppressed
counts. Every queued estimate still updates scheduling state. Retained records
include virtual/scheduled/emitted/received times and callback duration. Join CPU
`serving-sim/ARM_ID/requests.jsonl` and `observations.jsonl` with the managed GPU's
`output/clawbox-diagnostics.jsonl`, or each multi-cluster
`clusters/NNN/output/clawbox-diagnostics.jsonl`, by `cluster_id` and request ID
(and signal sequence for signals).
Unix timestamps are seconds; `monotonic_ns` is nanoseconds on the same host.
Server `first_sse_emitted` and `done_emitted` mark handoff to the HTTP stream;
compare them with client `times.first_sse` and `times.done`. These deltas include
HTTP/event-loop delivery, not just network propagation. CPU submit to server
`request_received` also includes client encoding and request handling.
This changes diagnostics only, not arrivals or simulated LLM timing.

Set `tokenizer_dir` to the existing model snapshot printed by simulator setup
when running with `--sudo` or another account whose Hugging Face cache differs.
This lets fresh shells reuse those assets without relying on an earlier shell's
`HF_HOME`; no model download or image rebuild is needed.
Set `hf_hub_cache` to the Hugging Face hub cache used during simulator setup;
native processor initialization uses that cache independently of `tokenizer_dir`.
Set `cache_dir` to the `SERVING_SIM_CACHE` directory used during setup for native
caches. Omitting these fields uses the launcher's environment/default lookup.

Recorded inference (`replay` and `serving_sim`) requires the current Runtime
context guards: OpenClaw must not add summary requests when its local context
estimate grows. Refresh an older Runtime with
`clawbox experiment images --profile /absolute/host.json --role runtime`, adopt
`runtime.template_id`, `runtime.image_digest` and `runtime.source_image_reference` from the updated profile
in the new experiment's `environment.runtime`, and qualify it again.
Preserve the old templates and case-specific Tool mappings. This update needs
no Tool rebuild or ClawTune retraining; live inference keeps its context guards.

The following commands describe that external-service alternative:

```bash
export SIM_VENV=/absolute/simulator-venv
python3 -m clawbox.integrations.serving_sim setup --sim-root /absolute/agent-sched-bench-gpu --cores 4
python3 -m clawbox.integrations.serving_sim check --sim-root /absolute/agent-sched-bench-gpu --cores 4
python3 -m clawbox.integrations.serving_sim smoke --sim-root /absolute/agent-sched-bench-gpu --cores 4 --out /absolute/native-smoke
python3 -m clawbox.integrations.serving_sim serve --sim-root /absolute/agent-sched-bench-gpu \
  --cores 4 --topology 8A --trace-catalog /absolute/gpu-bundle/catalog.json \
  --k 1 --port 8000 --out /absolute/gpu-qualification
```

In another shell, qualify the joint experiment:

For a matched topology comparison, read `/v1/clawbox/simulator/info` and copy its
`topology_digest` into `inference.configuration.serving_sim_topology_digest`
before qualification. Without this optional field, the actual topology is
recorded but not compared against an expected value. Freeze the simulator
revision, tokenizer and cost-model assets separately; the topology digest
does not cover those files.

```bash
clawbox experiment qualify --baseline joint.yaml --workload workload.yaml --concurrency 1 --receipt /absolute/joint-qualification.json
```

Stop that simulator with Ctrl-C. Start a fresh service with the same settings and
`--out /absolute/gpu-formal-01`, then run the single configured arm:

```bash
clawbox --output-root /absolute/results experiment run --baseline joint.yaml --workload workload.yaml \
  --run-id joint-01 --qualification /absolute/joint-qualification.json
```

Use another fresh simulator process and output directory for each comparison
arm. Stop the service after the arm so it saves its realized inputs and validity
report. For large runs, add `--persistence final` to both qualification and formal
simulator launches to avoid rewriting all accumulated requests after every call.
The default `native` retains those intermediate reports. With `final`, use Ctrl-C
or SIGTERM after the CPU arm exits, wait for shutdown, and check that
`clawbox-persistence.json` has `finalized: true`; abrupt termination loses deferred
GPU reports. This changes reporting only, with no host apply, image rebuild or
retraining. Keep both GPU output and the CPU run directory. The CPU records actual
Tool work, VM lifecycle and transport observations; the simulator records its
predicted GPU timeline. Inspect wall-clock delivery lag before interpreting a
large-concurrency result.

For traces without reasoning counts (including the 502-trace workload), use:
```yaml
mechanisms:
  idle_reclaim: {policy: gpu, scope: agent_and_tool}
  checkpoint: {mechanism: incremental-cow, storage: tiered, residency: next-use}
  restore: {prewarm: eta, prewarm_lead_seconds: 2.0, promotion: 'off'}
execution:
  snapshot_page_management:
    mode: state_driven
    runtime_trigger_event: prefill_start
    tool_trigger_event: first_token
```
Merge these fields into the existing baseline; keep its qualified environment,
WARM capacity and page-preparation budgets. Replace the removed `prewarm_events`
field. Validate the baseline/workload, qualify at small concurrency, then launch
with a new run ID using the commands above and the current ClawBox adapter.

The sequence is queue → prefill admission → first output token → decoding →
complete response. Prefill admission prepares Runtime snapshot pages; the first
token prepares Tool pages. Neither starts full VM restore. GPU-side
`response_forecast` combines the **recorded current call's total output length**
with observed token progress and rolling generation speed. This is a trace-length
oracle experiment, not a predictor of unknown live-model output length. No future
call or recorded LLM duration is consulted. The ETA targets native complete-response
readiness, including the simulator's return delay, excluding HTTP delivery and CPU
restore. It is not an ETA for the next Tool command.

Fresh response ETA drives all three decisions: GPU reclaim chooses the idle
pair with the longest remaining time above the threshold; next-use ranks snapshot
residency; ETA prewarm starts Runtime then Tool when remaining time is at most
the configured lead. Ordinary memory admission and Tool bridge readiness still
apply. A deferred Tool prewarm retries on subsequent fresh forecasts. Actual
response delivery always waits for Runtime readiness; Tool commands retain their
normal admission path.

Queue ETA is available only with both a usable native queue/prefill estimate and
a recent engine generation rate. Prefill admission invalidates it until output
progress establishes a rate. KV waits, preemption, recomputation and expired
observations invalidate ETA; pending restores recheck it before starting.
Already-started restores finish. Missing reasoning counts need no special fallback.
Short calls may finish before a usable forecast; their actual response restores
Runtime and their actual command restores Tool.

See the [inference and GPU policy options](reference.md) for defaults and which
changes require a new run, qualification, image build or retraining.
