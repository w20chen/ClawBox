# Command and configuration reference

Use the [experiment walkthrough](guide.md) for command order and the
[installation guide](installation.md) for host setup and update requirements.
This page describes the supported schema-4 interface. The complete machine-readable
field catalog and command flags are available from the installed code:

```bash
bash scripts/clawbox experiment schema > /tmp/clawbox-schema.json
bash scripts/clawbox experiment --help
bash scripts/clawbox experiment configure --help
```

Defaults below are model defaults; recipes, the base YAML, and host profiles can
set explicit values. `experiment describe SPEC` shows the effective comparison.
Unknown YAML fields and invalid combinations fail validation.

## Commands

All commands in this table follow `bash scripts/clawbox experiment`.
Use wrapper option `--sudo` before `experiment` for privileged VM/host operations.
Root option `--output-root DIRECTORY` also precedes `experiment`.
Use `--sudo` for `collect` and `report` when the run was launched as root, because
they rewrite result artifacts.

| Command | Purpose and main options |
| --- | --- |
| `recipes [--json]` | List schema-4 comparison families. |
| `schema` | Print the full editable JSON Schema, including defaults and constraints. |
| `host inspect` | Read NUMA topology, CPUs, and memory. |
| `host init FILE [--detect] [--from-profile PROFILE]` | Write a new editable host YAML; existing files are preserved. |
| `host check FILE` | Check the plan and prerequisites without applying it. |
| `host apply FILE [--profile PROFILE]` | Apply a reviewed plan on an idle host; default profile is `~/.config/clawbox/host.json`. |
| `host clean [--apply]` | Preview or remove recognized crash dumps; `--root`, `--older-than-hours` (24), `--min-size-mib` (100). |
| `setup` | Direct single-compute-node setup using template/node and LOCAL/WARM options; the editable `host` workflow is preferred. |
| `images [--role runtime\|sandbox]` | Build/register integration images using an existing profile. Repeat `--role` or omit it for both. |
| `configure BASE OUTPUT` | Generate an editable specification; `--profile`, `--recipe`, `--set`, `--force`, and the options below. |
| `import-trace SOURCE --output FILE` | Convert a recording to replay schema 6; workspace mapping and `--python` are optional. |
| `import-corpus SOURCE --output DIRECTORY` | Import unique tasks; optional `--count`, repeated `--prefer`, workspace mapping, and `--python`. |
| `trace FILE` | Inspect a replay without creating VMs. |
| `validate SPEC [--inputs]` | Validate the design; `--inputs` also parses and hashes workload/prediction inputs. |
| `describe SPEC [--json]` | Show VM shapes, policies, budgets, and arm count. |
| `plan SPEC` | Print the expanded trial matrix. |
| `doctor SPEC [--probe-vm]` | Check services, templates, and resources; optionally probe a real VM. |
| `prove-template-sharing SPEC --output FILE` | Prove physical RAM-base sharing; `--artifact-root`, repeated `--resume-result`. |
| `qualify SPEC [--concurrency N] [--receipt FILE]` | Run capability/case probes and save a reusable receipt. |
| `launch SPEC [--run-id ID] [--detach]` | Validate, qualify as needed, and run/resume. Also accepts `--apply-host HOST.yaml`, `--qualification FILE`, `--qualification-concurrency N`, `--force-qualify`. |
| `run SPEC` | Lower-level execution with a valid receipt; `--run-id`, `--attempt-id`, `--owner-id`, `--qualification`, `--detach`. Prefer `launch`. |
| `resume ID [--attempt-id ID] [--detach]` | Resume unfinished arms from the frozen run. |
| `status ID` | Read supervisor, trial, and cleanup state. |
| `collect ID`, `report ID` | Expand canonical arm results or regenerate reports without rerunning work. The stored `summary.json` is a compact arm index. |
| `abort ID`, `destroy ID` | Stop/clean owned VMs while preserving results; `--cleanup-workers` (8), `--cleanup-timeout-seconds` (180). |
| `train RUN_DIRECTORY... --trace FILE --repository NAME --output FILE` | Fit frozen KBs and measured host-memory calibration from completed training runs. |

`baselines` and the old suite/baseline configure selectors belong to the
[deprecated schema-3 interface](deprecated.md). New designs use recipes and
explicit mechanism overrides.

### Configure options

`configure` reads `BASE`, applies the host profile and explicit options, and writes
`OUTPUT`. A file already present requires `--force`. Use YAML for fields without a
dedicated CLI flag.

| Group | Options |
| --- | --- |
| Design | `--experiment-id`, `--recipe`, `--fixed-methods`, `--checkpoint-axis encoding\|storage\|residency`, repeated `--set GROUP.FIELD=YAML_VALUE` |
| Workload | `--trace`, `--workload-manifest`, `--case-id`, `--prompt`, `--repository`, `--base-commit`, `--validation-command`, `--repetitions`, `--session-assignment single_case\|round_robin\|unique` |
| VM identity | `--runtime-template-id`, `--tool-template-id`, `--runtime-image-reference`, `--tool-image-reference`, `--runtime-image-digest`, `--tool-image-digest`, `--target-node` |
| VM shape | `--runtime-vcpu`, `--tool-vcpu`, `--runtime-memory-gib`, `--tool-memory-gib`, `--tool-workspace` |
| Prediction | `--prediction-mode runtime\|frozen\|cold-start`, `--prediction-artifact`, `--prediction-reservation-scale`, `--static-tool-memory-mib INTEGER\|auto`, `--admission-margin-mib` |
| Resources | `--pool-memory-gib`, `--emergency-free-memory-gib`, `--checkpoint-headroom-gib`, `--warm-root`, `--warm-memory-gib`, `--cold-root` |
| Placement/control | `--placement-policy`, `--session-compute-nodes` (comma-separated IDs), `--runtime-poll-interval-seconds`, `--memory-scheduling` |
| Snapshots/evidence | `--snapshot-mechanism full-copy\|incremental-cow`, `--snapshot-storage tiered\|warm-only`, `--template-base-sharing-artifact` |
| Scheduling | `--concurrency` (comma-separated levels), `--arrival-schedule burst\|fixed_stagger\|poisson`, `--stagger-seconds`, `--poisson-arrival-window-fraction`, `--poisson-arrival-window-seconds`, `--random-seed` |
| Deadlines | `--arm-timeout-seconds`, `--command-timeout-seconds` |
| Inference | `--inference-backend replay\|api`, `--model`, `--base-url`, `--api-key-env`, `--time-scale` |
| Launch immediately | `--launch`, with `--run-id`, `--qualification-concurrency`, `--force-qualify`, `--detach` |

Schema-4 `--set` accepts mechanism groups and `conditions.FIELD`. Other fields
are edited in YAML. Arm overrides take precedence and are reported by
`configure` when they replace an explicit common setting.

## Files, units, and schema layout

| Location | Meaning |
| --- | --- |
| `machine.env` | Shell settings for API addresses, source paths, image/template IDs, and default output root |
| `host.yaml` | Editable host topology, budgets, mounts, and service settings |
| `host.json` | Applied host profile and reusable image-build settings |
| Experiment YAML | Editable input before a run |
| `RUN/experiment.yaml` | Frozen input for one run ID |

Host-plan capacities and CLI options ending in `-gib` use GiB. VM YAML and
admission fields ending in `_mib` use MiB. One GiB is 1024 MiB; one MiB is
1,048,576 bytes. Durations are seconds unless named `_ms` or `_ns`. CPU lists use
Linux logical CPU IDs; vCPU counts do not reserve physical cores.

| Schema-4 section | Contents |
| --- | --- |
| `environment.runtime`, `environment.sandbox` | Shared Runtime/Tool VM identities and shapes |
| `environment.resources` | Physical scope, compute nodes, LOCAL/shared capacities, snapshot paths |
| `workload` | Input/manifest, cases, assignment, repetitions |
| `agent`, `inference` | OpenClaw driver and replay/API model configuration |
| `conditions` | Prediction source common to the comparison |
| `mechanisms` | Six policy dimensions plus deployment and coordination |
| `comparison` | Recipe label and explicit arm overrides |
| `evidence` | Prediction artifacts, measured cost models, sharing proof |
| `execution` | Offered concurrency, arrivals, timeouts, concurrency bounds, diagnostics |
| `measurement` | Sampling, stabilization, final validation, worker output fallback |

Resolved results also contain internal `resources` and `policies` objects. Use
the schema-4 locations above when editing inputs.

## Host plan

Paths must be absolute Linux paths without spaces or `..`. WARM storage must not
overlap COLD, Cubelet, source, or result paths. `host apply` requires an idle Cube
VM pool and writes a profile only after checks succeed.

| Field | Default | Meaning |
| --- | --- | --- |
| `runtime_template`, `tool_template`, `node` | Placeholders until configured | Actual registered identities |
| `cube_source` | `/home/you/src/CubeSandbox` | Patched SDK/server source; replace for this host |
| `compute_nodes[]` | Empty | Named compute domains with `node_id`, `numa_node`, `cpus`, `memory_gib`, `low_gib`, `high_gib` |
| `compute_nodes[].cpus` | Node's CPU list | Disjoint logical CPU ranges; e.g. `0-15,32-47` |
| `memory_gib`, `low_gib`, `high_gib` within a node | 36, 28, 32 | Require `0 < LOW < HIGH < capacity` |
| `local_node`, `local_gib`, `low_gib`, `high_gib` | 0, 36, 28, 32 | Single-domain alternative; omit when using `compute_nodes` |
| `warm`, `warm_node`, `warm_gib` | `true`, 1, 128 | Shared NUMA/WARM pool; node must differ from compute nodes |
| `shared_borrow_percent` | 50 | Maximum live-VM share of the common pool, 0–100%; snapshots and borrowing share one hard capacity |
| `warm_root`, `cold_root` | `/mnt/clawbox-warm`, `/data/clawbox-cold` | Snapshot paths |
| `cubelet_data_root`, `output_root` | `/data/cubelet`, `/data/clawbox-results` | Existing Cubelet storage and experiment outputs |
| `minimum_disk_free_gib` | 20 | Required filesystem headroom |
| `tcp_tw_reuse` | `null` | Preserve current value; explicit 0, 1, or 2 applies that kernel mode |
| `cube_proxy_connect_timeout_seconds` | `null` | Preserve Cube default; explicit 3–300 seconds bounds guest connection setup |
| `cubelet_tap_init_num` | `null` | Preserve Cube's warm TAP target; positive explicit value is a preallocation target, not a VM limit |
| `cube_scheduler_overcommit` | `null` | Map actual Cube `InstanceType` to positive `cpu_ratio` and `mem_ratio` |
| `cube_scheduler_vm_count_ratio` | `null` | Positive VM-count quota multiplier by actual `InstanceType` |
| `cube_master_log_level` | `null` | Preserve Cube logging; `debug`, `info`, `warn`, or `error`. `warn` avoids verbose inventory logs in large runs. |

Cube's quotas cover configured VM resources and differ from ClawBox's measured
admission limits. Size them from reported node quotas and requested VM shapes,
including existing allocations and headroom. Quota changes affect every node of
the selected instance type. Apply changes on an idle host; the helper restarts
affected services when needed. Detection is a starting proposal, not an allocation
of the host's physical capacity.

## Environment and workload

Each VM specifies exactly one of `template_id` or `template_alias`. Optional
`source_image_reference` and `image_digest` identify its immutable image.
`architecture` is `arm64`, `vcpu` defaults to 2, `memory_mib` defaults to 1024
for Runtime and 3072 for Tool, `workspace` defaults to `/workspace`, and
`allow_internet_access` defaults to `true`. `preflight_command` optionally checks
guest prerequisites. Template shape must match the specification. For Tool VMs,
the top-level `sandbox.allow_internet_access` is the experiment network policy;
it also applies to case-specific Tool sandboxes while their template, image,
shape and workspace remain case-specific. Changing it requires a new run ID and
qualification, but no host apply or image rebuild.

`environment.resources` holds the target node, `pool_memory_budget_mib`,
`emergency_free_memory_mib`, `compute_nodes`, `local_memory_cgroup`, LOCAL
capacity/LOW/HIGH values, and shared/WARM capacities and NUMA paths. Use the
applied profile to populate them. Node entries use `node_id`, `numa_node`,
`cpus`, `memory_capacity_mib`, `low_watermark_mib`, and `high_watermark_mib`.
Without `compute_nodes`, use `local_numa_node` and the aggregate LOCAL fields.
`warm_memory_capacity_mib` defaults to 0; `local_memory_cgroup` defaults to
`/sys/fs/cgroup/cube_sandbox/sandbox`.

`resources.cubelet_storage_root` defaults to `/data/cubelet/storage`.
Before launch, host doctor projects physical Cube CoW growth as offered Runtime
and Tool VM count times `resources.cube_cow_headroom_per_vm_mib` (default 144
MiB), and refuses a run that would reach Cube's 85% filesystem rejection
threshold. The default includes headroom over the latest Full-1280 observation;
it is a physical-space allowance, not the VM's logical writable-layer size.
Changing either field needs a new run ID, but no host apply or template rebuild.

`workload.repetitions` defaults to 1 and `session_assignment` to `unique`.
`single_case` repeats one case; `round_robin` cycles at least two cases; `unique`
requires enough distinct cases for the offered concurrency. Each case records
its `case_id`, source, prompt, repository/revision, replay reference, optional
validation command, and metadata. A case-level `sandbox` is a complete Tool VM
description, not a partial override. Optional `snapshot_reuse_probability` is a
value in `[0,1]` used by value-based snapshot residency.

## Mechanisms

All fields below are under `mechanisms.GROUP`. Groups are independently editable;
validation checks their dependencies.

| Group | Principal fields and defaults |
| --- | --- |
| `admission` | `tool: off` (`off`, `fixed`, `predicted`); `startup_headroom_mib: 256`; `admission_margin_mib: 256`; fixed/non-command budgets unset; `prediction_reservation_scale: 1.0`; `small_tool_admission_bypass_mib: 4` |
| `runtime` | `memory_protection: false`; `cpu_release_boost: false`; `proactive_memory_reclaim: false`; `runtime_poll_interval_seconds: 0.5`; nested `pressure_control` below |
| `placement` | `policy: round_robin` (`os_default`, `round_robin`, `explicit`, `load_aware`); `template_affinity: false`; `placement_cpu_overcommit: 1024.0`; `template_balance_slack: 0.10`; `session_compute_nodes: []`; Tool-call CPU fields below |
| `idle_reclaim` | `policy: off` (`off`, `wait`); `scope: tool` (`tool`, `agent_and_tool`); `checkpoint_restore_headroom_mib: 1024` |
| `shared_memory` | `borrow: off` (`off`, `hard`, `pressure`); `borrow_cooldown_seconds: 5`; `return_cooldown_seconds: 5` |
| `checkpoint` | `mechanism: incremental-cow` (`full-copy`, `incremental-cow`); `storage: disk` (`disk`, `warm-only`, `tiered`); `residency: lru` (`lru`, `value`); transfer limits below |
| `deployment` | `mode: managed` (`managed`, `os-default`); `resident_capacity_accounting: false`; `lifecycle_reservation_scale: 1.0` |
| `coordination` | `action_selection: reclaim_first` (`reclaim_first`, `measured_cost`) |

Predicted reservations are `ceil(predicted_MiB * prediction_reservation_scale)`,
with minimum zero. `static_tool_memory_mib` sets fixed admission;
`non_command_tool_memory_mib` sets filesystem/maintenance admission and falls
back to the static budget. These explicit fixed budgets are not scaled by the
prediction multiplier. A positive bypass threshold skips ledger accounting for
reservations at or below it; zero disables bypass. It does not cap actual memory.

The below-LOW routing shortcut is automatic for resident Tools whose sampled
LOCAL use, nominal claims, new reservation, and headroom fit below LOW. It skips
pressure routing but retains admission accounting unless the small-call bypass
also applies.

Resident-capacity accounting requires managed deployment, Tool admission off,
and no shared-memory borrowing or proactive reclaim. Its scale must be greater
than zero and at most one. The resident-capacity recipe holds fixed VM charges
until cleanup; ordinary managed admission tracks measured consumption and
outstanding growth claims.

### Pressure control

These fields are under `mechanisms.runtime.pressure_control`:

| Field | Default | Unit / meaning |
| --- | --- | --- |
| `release_history_samples` | 3 | Samples, range 3–8 |
| `release_forecast_ttl_factor` | 3 | Dimensionless validity multiplier, greater than 1 |
| `psi_ratio` | 0.10 | Pressure threshold, fraction in `(0,1]` |
| `exit_hysteresis_seconds` | 1 | Seconds before leaving pressure mode |
| `cooldown_seconds` | 10 | Seconds between eligible actions |
| `action_window_seconds` | 2 | Seconds per action window |
| `protection_budget_mib` | 256 | MiB of memory-protection budget |
| `max_boosted_vms` | 1 | VMs, range 0–4 |
| `boost_cpu_weight` | 200 | cgroup weight, range 100–400 |
| `min_release_mib` | 64 | Minimum predicted release, MiB |
| `amortization_factor` | 2 | Dimensionless cost multiplier, at least 1 |

### Tool-call CPU placement

Fields are under `mechanisms.placement`:

| Field | Default | Meaning |
| --- | --- | --- |
| `tool_call_cpu_policy` | `c0` | Full selected NUMA CPU pool; `c2` allows temporary one-LLC-cluster placement |
| `tool_call_cpu_min_duration_seconds` | 1 | Delay before an eligible `agent-tool` `exec` may narrow |
| `tool_call_cpu_group_max_active` | 2 | Maximum narrowed VMs per group; not exclusive core ownership |
| `tool_call_cpu_pressure_ratio` | 0.10 | CPU PSI threshold for expansion |
| `tool_call_cpu_pressure_samples` | 2 | Consecutive pressure samples before expansion |
| `tool_call_cpu_include_smt` | `false` | Include allowed SMT siblings in the selected cluster |

`c2` requires readable Linux topology and at least two eligible LLC clusters.
Calls begin on the full mask and return to it on pressure, completion, or failure.
Filesystem, maintenance, and short calls retain full-pool placement.

### Snapshot and lifecycle limits

Fields are under `mechanisms.checkpoint`:

| Field | Default | Meaning |
| --- | --- | --- |
| `pool_transfer_concurrency` | 1 | Shared limit on concurrent Cube lifecycle operations |
| `snapshot_transfer_concurrency` | `null` | Optional smaller checkpoint/restore/spill limit; defaults to the shared limit and cannot exceed it |
| `pool_transfer_timeout_seconds` | 120 | Seconds to acquire an operation slot |

WARM and live borrowing share one capacity. Pre-existing shared-node occupancy
is measured and withheld from new reservations. `warm-only` has no disk fallback;
`tiered` can use COLD or spill eligible WARM snapshots. Incremental CoW uses lazy
restore. These snapshot choices take effect when idle reclaim is enabled.

## Prediction and evidence

`conditions.prediction_source` maps `kb` to CLI mode `runtime`, `cold-start-kb`
to `cold-start`, and `command-table` to `frozen`. Its schema default is `kb`.
`evidence.prediction_artifact` points to the selected immutable seed or artifact.
See the [training walkthrough](guide.md#train-a-workload-specific-kb).

Measured-cost policies require evidence with a nonempty `provenance` string:

| Evidence field | Required content |
| --- | --- |
| `memory_action_costs` | Live-borrow service and runtime-penalty seconds/GiB, WARM checkpoint/restore seconds/GiB, expected restore probability; optional COLD costs, remote latency ns, and bandwidth MiB/s |
| `snapshot_residency_costs` | WARM and COLD restore seconds/GiB |
| `idle_reclaim_costs` | WARM checkpoint/restore seconds/GiB; optional COLD costs |
| `template_base_sharing_artifact` | Path to reusable proof for the exact Tool templates and node |

Use measured values from the intended deployment. The JSON Schema lists exact
field names and required ranges; `describe` reports which arms need each model.

## Execution

Fields are under `execution`:

| Field | Default | Meaning |
| --- | --- | --- |
| `concurrency_levels` | `[1]` | Positive offered session counts |
| `sandbox_create_concurrency` | 8 | Independent preparation and actual-create limits, each in VM pairs; a pair can issue two concurrent creates, subject to lifecycle limits |
| `dependency_install_concurrency` | `null` | Optional arm-wide bound on active apt/apt-get/pip installs |
| `apt_cache_proxy_url` | `null` | Tool-only `http://host:port` APT proxy used when Tool internet access is disabled |
| `pypi_cache_index_url` | `null` | Tool-only `http://host:port/.../+simple/` pip index used when Tool internet access is disabled |
| `cube_request_timeout_seconds` | 130 | Per-request Cube HTTP deadline, seconds |
| `policy_progress_timeout_seconds` | 30 | Total deadline in seconds for each Tool start/end acknowledgement; at most three idempotent notification attempts share it. Commands are never retried and policy rejection remains fatal. Requires the matching Runtime shim and Tool bridge images; a changed value needs a new run ID. |
| `cube_create_capacity_retry_seconds` | 60 | Retry window for definite Cube `130597` rejection; 0 disables retries |
| `worker_thread_switch_interval_seconds` | 0.001 | Python handoff interval, `(0,0.1]` seconds |
| `randomized_order`, `random_seed` | `true`, 0 | Arm-order randomization and reproducible seed |
| `arm_timeout_seconds` | 3600 | Arm execution deadline, seconds |
| `arm_finalization_timeout_seconds` | 300 | Additional supervisor allowance for cleanup/results, seconds |
| `command_timeout_seconds` | 300 | Tool command deadline, seconds |
| `replay_tool_timeout_multiplier` | 1.0 | Unitless multiplier, 1.0–100.0, applied only to recorded replay Tool deadlines after admission. Fast commands do not wait longer; only expiry is extended. Use a new run ID after changing it. |
| `arrival_schedule` | `poisson` | `burst`, `fixed_stagger`, or `poisson` |
| `stagger_interval_seconds` | 0 | Positive for fixed staggering; zero for burst/Poisson |
| `poisson_arrival_window_fraction` | 2.0 | Expected window divided by median scaled trace duration |
| `poisson_arrival_window_seconds` | `null` | Positive explicit expected window; overrides the fraction |
| `control_timing_details` | `false` | Enable nested control spans, GC tracing, raw lifecycle dumps, duplicate payloads, and Runtime working-KB exports |
| `model_request_content_retention` | `failures` | `failures` keeps compact successful Model Gateway records and exact request/response content only for incomplete, errored, non-200, or undelivered requests. `all` retains exact content for every request for a full-audit run. The setting changes result retention only; use a new run ID. It requires no host apply, image rebuild, or retraining. |

Creation retry uses backoff and releases lifecycle slots while waiting. Ambiguous
HTTP failures use ownership reconciliation rather than blindly creating another
VM. The API and SDK must support the corresponding lifecycle deadlines.

## Inference and measurement

The collector refresh helper's `--retire-replaced-templates` option defaults to
off. When enabled, it deletes each recorded old Cube template only after checking
its distinct replacement is READY and image identities match. It preserves
Docker images; deleted template IDs require using the new workload manifest and
a new run ID. See the [collector refresh walkthrough](workloads.md).
Its `--minimum-free-gib` option defaults to 64 GiB and applies to every checked
filesystem. This is a build stop floor, not a guarantee of Cube admission or
enough space for the full corpus. Set it above the host's snapshot storage
reserve, allowing for concurrent builds; verify reclaimed space with `df`
because reflink copies can share physical blocks.

`agent.driver` defaults to `openclaw`; `inference.backend` defaults to `replay`.
Replay serves recorded model responses and executes Tools normally. The CLI and
worker accept `api` provider settings, but current qualification requires replay;
live API inference has no complete supported launch path. Fields below are in
`inference.configuration`:

| Field | Default | Meaning |
| --- | --- | --- |
| `model`, `base_url` | Explicit configuration or saved environment | Provider model and endpoint |
| `api_key_env` | `OPENCLAW_API_KEY` | Name of the host environment variable holding the credential |
| `time_scale` | 1.0 | Nonnegative multiplier on replayed model delays |
| `runtime_startup_timeout_seconds` | 60 | Integer 1–3600; Runtime setup and sidecar readiness deadline |
| `runtime_completion_watch_seconds` | 10 | 1–60 seconds per completion notification wait; events wake it immediately |
| `openclaw_bin` | `openclaw` | Guest executable |
| `openclaw_exec_yield_ms` | Unset | Optional integer from 10 to 120000 milliseconds for background-capable OpenClaw exec calls. Synchronous replay recordings disable automatic background execution; see [workloads](workloads.md). |
| `openclaw_tool_home` | `/root` | Absolute home directory inside the Tool VM. OpenClaw shell commands use it to find the image's Git and Conda configuration. Change for a custom Tool user; takes effect with a new run ID, without host apply or retraining. |
| `clawtune_config`, `clawtune_root` | Unset | Reuse a ClawTune model configuration and source root |

Put credential values in the environment or selected credential configuration,
not experiment YAML. A selected `clawtune_config` supplies the endpoint, model,
and credential in preference to direct provider fields. Model-wait prediction/oracle settings are unsupported;
checkpoint decisions use actual outstanding model requests.

`measurement.memory_sample_interval_seconds` defaults to 0.2,
`stabilization_seconds` to 1, and `observe_predictions` to `false`.
The sampler targets this start-to-start interval and skips missed ticks instead
of issuing catch-up bursts. Core memory readings remain on the sampler thread;
rich queue, filesystem, CPU and policy reporting runs in a bounded latest-only
worker so it cannot delay the next reading. C2 maintenance runs in its own
serial loop at the same interval. Resource records include observation times,
report coalescing and cached-demand age; see [result interpretation](results.md).
Changing the interval or host sampling implementation requires a new run ID,
without host apply, image rebuild or retraining.
`measurement.validation.command` optionally checks the final task result.
`measurement.output.directory` defaults to `/results` and is a worker fallback.
The supported CLI supplies the host output root through `--output-root` or
`CLAWBOX_OUTPUT_ROOT` (default `results`). Diagnostic logging does not disable
the Tool collector, required measurements, or final validation.

## Image build options

`experiment images` reads the applied profile. Explicit options override saved
`image_build` settings, followed by supported environment defaults.

| Option | Default / requirement |
| --- | --- |
| `--profile` | `~/.config/clawbox/host.json` |
| `--role runtime\|sandbox` | Both when omitted; repeat to select roles |
| `--registry` | Saved setting, otherwise `127.0.0.1:5000/clawbox` |
| `--go` | Saved setting, `CLAWBOX_GO`, or `go` on PATH |
| `--pip-index` | `https://pypi.org/simple` |
| `--direct-network`, `--proxy-network` | Explicit override of saved proxy handling |
| `--kernel-source`, `--kernel-build` | Required for first Tool build; matching guest source and generated headers |
| `--tool-prewarm enabled\|disabled` | Saved setting, otherwise `enabled`; attach Tool BPF before template readiness |
| `--tool-prewarm-timeout-seconds` | Saved setting, otherwise 120 seconds |

Runtime builds also need Node.js/npm. The selected seed comes from
`CLAWTUNE_KB_SEED` or ClawTune's default. The command saves successful image and
template identities to the profile. See [update requirements](installation.md#update-an-existing-host)
before rebuilding a role or adopting new KB state.

`CLAWTUNE_ROOT` and `CLAWTUNE_SIDECAR_SRC` must select the updated ClawTune
source with ToolKB, TrieKB, and LatticeKB only. `CLAWTUNE_KB_SEED` defaults to
that source's `bootstrap-v2`; a custom seed must validate against its contracts.
EdgeKappa seed/state manifests are no longer accepted. Changing a seed requires
fresh session state and a new run ID; changing the Runtime predictor or seed
requires `--role runtime`, without rebuilding Tool templates.

For recorded Conda environment creation, `scripts/cache-recorded-conda-environments.py`
requires `--manifest FILE`, `--output-dir DIR`, and `--registry REPOSITORY`.
Optional `--workload FILE` writes replacements for subsequent template registration;
`--proxy URL` defaults to empty and affects build downloads only. See
[workload preparation](workloads.md#refresh-tool-collectors) for manifest fields,
ARM64 Docker prerequisites, validation, and registration before a new run.
