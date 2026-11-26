# Command and configuration reference

Use the [experiment walkthrough](guide.md) for command order and the
[installation guide](installation.md) for host setup and update requirements.
This page describes the baseline/workload interface. The complete machine-readable
field catalog and command flags are available from the installed code:

```bash
bash scripts/clawbox experiment schema baseline > /tmp/clawbox-baseline-schema.json
bash scripts/clawbox experiment schema workload > /tmp/clawbox-workload-schema.json
bash scripts/clawbox experiment --help
bash scripts/clawbox experiment resolve --help
```

Defaults below are model defaults. Only explicit values in the two files replace
them; host profiles do not override them. Unknown fields and invalid
combinations fail validation. `SPEC` below means `--baseline baseline.yaml
--workload workload.yaml`, or a positional internal snapshot produced by `resolve`.
Explicit recorded-trace and snapshot-branching case `source_reference` paths
are relative to workload.yaml, including synthetic branching cases. They are
frozen as absolute paths; changing the shell directory does not change the input.

## Commands

`clawbox experiment` is the public experiment interface. Prefer `launch` with a
baseline/workload pair; `qualify`, `run` and `resume` belong to the same workflow.
The worker module is internal to the supervisor.

All commands in this table follow `bash scripts/clawbox experiment`.
Use wrapper option `--sudo` before `experiment` for privileged VM/host operations.
Root option `--output-root DIRECTORY` also precedes `experiment`.
Use `--sudo` for `collect` and `report` when the run was launched as root, because
they rewrite result artifacts.

| Command | Purpose and main options |
| --- | --- |
| `prepare DIRECTORY --baseline B --workload W --machine-env M` | Create editable copies plus `experiment.sh` bound to the selected checkout, environment and output root. Optional `--host-config H`, `--creation-standard S`; never overwrite. Does not apply settings or validate live readiness. |
| `preflight SPEC [--json] [--qualification FILE]` | Aggregate read-only launch checks and remedies; exit 0 if ready, 2 for failed checks, 1 for configuration/read errors. An explicit invalid receipt fails; without one, launch decides reuse/qualification. |
| `schema baseline\|workload` | Print the full editable JSON Schema, including defaults and constraints. |
| `host inspect` | Read NUMA topology, CPUs, and memory. |
| `host init FILE [--detect] [--from-profile PROFILE]` | Write a new editable host YAML; existing files are preserved. |
| `host check FILE` | Check the plan and prerequisites without applying it. |
| `host apply FILE [--profile PROFILE]` | Apply a reviewed plan on an idle host; default profile is `~/.config/clawbox/host.json`. |
| `host recover FILE --profile PROFILE [--apply] [--timeout-seconds 1200]` | Preview reboot recovery; explicit apply starts CubeAPI/CubeMaster dependencies and Cubelet/egress in dependency order, waits for heartbeat, then runs host apply and its VM probe. Timeout is a positive integer in seconds for service readiness, separate from host apply. Refuses occupied/leased VM domains. |
| `host clean [--apply]` | Preview or remove recognized crash dumps; `--root`, `--older-than-hours` (24), `--min-size-mib` (100). |
| `snapshot-storage init FILE` | Write a schema-2 peer-local-ssd host plan with explicit disk identities; never overwrite. |
| `snapshot-storage check FILE` | Show the reviewed storage/network plan and verify root, tools, space, and NUMA prerequisites. |
| `snapshot-storage inspect-backing --backing-device DEVICE` | Read XFS allocation-group free space and linear device mapping; does not reserve space. |
| `snapshot-storage reserve-image --image FILE --capacity-mib N --backing-device DEVICE --physical-device SSD --allocation-group N [--allocation-group-end N] [--chunk-mib 16384] [--resume-receipt FILE]` | Reserve a new owned image and prove every payload extent resides on the selected SSD. A group range initializes and assembles bounded chunks; output includes the required allocation receipt. |
| `snapshot-storage apply FILE` | Create or recover the plan on an idle VM host. Reuse saved pools; allow reviewed rate/latency/MTU changes. Storage identity/capacity changes require a new pool. |
| `snapshot-storage cleanup FILE [--apply]` | Preview by default; explicit apply removes only recorded mounts/network state and preserves loop images. |
| `snapshot-storage prepare FILE --template ID ... --control-socket PATH --receipt FILE [--workers N]` | Import complete verified templates into both applied native domains; receipt filename must be new. `N` is a positive number of concurrent template preparations, default `1`; shared immutable objects are copied once per SSD. Preparation occurs outside replay and does not cap sessions. |
| `snapshot-storage qualify-network FILE --output FILE [--size-mib 128]` | Verify two concurrent transfers share the aggregate directional limit; restore the configured qdisc afterwards. |
| `snapshot-storage cleanup-replicas FILE --run-id ID --control-socket PATH [--apply]` | Preview by default; release native references and remove only this run's unreferenced replicas. Requires no active VMs or peer transfers. |
| `snapshot-storage reset-cache FILE [--apply]` | Preview SSD file residency by default. On an idle Linux host with applied owned mounts, sync unique regular inodes inside the two local SSD roots, then their exact owned backing images, and request `POSIX_FADV_DONTNEED`; preserve WARM and all other external files. Inner files and backing images have separate requested-byte and actual before/after `mincore` page counts. Advice is not guaranteed, including with direct-I/O loops. Use after qualification/replica cleanup and before each matched formal arm; no host apply or image rebuild required. |
| `setup` | Direct single-compute-node setup using template/node and LOCAL/WARM options; the editable `host` workflow is preferred. |
| `images [--role runtime\|sandbox]` | Build/register integration images using an existing profile. Repeat `--role` or omit it for both. |
| `resolve SPEC --output FILE` | Freeze effective settings without starting VMs; refuses existing outputs. |
| `base-cache init FILE --baseline B --workload W --template ID --capacity-mib N` | Resolve cache inputs using the same configuration pair; prepare/status/cleanup manage the prepared host cache. |
| `import-trace SOURCE --output FILE` | Convert a recording to replay schema 6; workspace mapping and `--python` are optional. |
| `import-corpus SOURCE --output DIRECTORY` | Import unique tasks; optional `--count`, repeated `--prefer`, workspace mapping, and `--python`. |
| `prepare-serving-sim SPEC --output DIRECTORY` | Prepare a new GPU catalog and CPU binding manifest from selected replay cases. Optional `--source-map FILE` maps case IDs to original format-5 paths; relative paths use that JSON file's directory. Existing output directories are rejected. |
| `trace FILE` | Inspect a replay without creating VMs. |
| `validate SPEC [--inputs]` | Validate the design; `--inputs` also parses and hashes workload/prediction inputs. |
| `describe SPEC [--json]` | Show VM shapes, policies, budgets, and arm count. |
| `plan SPEC` | Print the expanded trial matrix. |
| `doctor SPEC [--probe-vm]` | Check services, templates, and resources; optionally probe a real VM. |
| `prove-template-sharing SPEC --output FILE` | Prove physical RAM-base sharing; `--artifact-root`, repeated `--resume-result`. Reuses matching template IDs from prior raw proofs and retains their file hashes; replaced IDs are omitted. |
| `qualify SPEC [--concurrency N] [--receipt FILE]` | Run capability/case probes and save a reusable receipt. |
| `launch SPEC [--run-id ID] [--detach]` | Validate, qualify as needed, and run/resume. Also accepts `--apply-host HOST.yaml`, `--qualification FILE`, `--qualification-concurrency N`, `--force-qualify`, and `--cleanup-timeout-seconds N` (180 seconds by default). |
| `run SPEC` | Lower-level execution with a valid receipt; `--run-id`, `--attempt-id`, `--owner-id`, `--qualification`, `--cleanup-timeout-seconds` (180), `--detach`. Prefer `launch`. |
| `resume ID [--attempt-id ID] [--detach]` | Reopen the saved attempt; preserve completed traces, continue traces that never began VM preparation, and mark unrecoverable interrupted traces incomplete. A different explicit attempt ID reruns cold. `--cleanup-timeout-seconds` defaults to 180 seconds. |
| `status ID` | Read supervisor, trial, and cleanup state. |
| `collect ID`, `report ID` | Expand canonical arm results or regenerate reports without rerunning work. The stored `summary.json` is a compact arm index. |
| `abort ID`, `destroy ID` | Stop/clean owned VMs while preserving results; `--cleanup-workers` (8), `--cleanup-timeout-seconds` (180). |
| `train RUN_DIRECTORY... --trace FILE --repository NAME --output FILE` | Fit frozen KBs and measured host-memory calibration from completed training runs. |

### Configuration ownership

Commands accepting `SPEC`, plus `resume`, accept `--host-config H` and
`--creation-standard S`. These select readiness constraints, not resource or
template overrides. Defaults are `CLAWBOX_HOST_CONFIG` and
`CLAWBOX_CREATION_STANDARD` when set; otherwise no optional plan/manifest is
selected. Explicit flags win and are inherited by detached execution. The
prepared wrapper passes its saved selections on each invocation. `prepare`
requires explicit paths for the bindings it saves; it does not copy these files.

Creation manifests use schema version 1 and check kernel, boot parameters,
Full1280 TAP reserve and CubeAPI NOFILE. They do not establish the provenance
of a later native build; retain installation evidence and real-VM qualification
for IRQ/snapshot changes. Readiness is checked again at launch; it is not a
reservation against another concurrent launch. Full1280 requires 2560 distinct
Ready/unowned TAPs for 1280 active Runtime/Tool pairs. Smaller active limits use
the actual VM demand. Managed simulator ports must be free and outside the host
ephemeral range, or explicitly listed in `ip_local_reserved_ports`.

Prepared `experiment.sh` accepts `[--sudo] COMMAND ...`, including `host`,
`status`, `resume`, `abort`, and `report`, and fixes the output root for all of
them. Its generated README lists command order. Editing the baseline/workload
requires a new run ID; changing a binding requires preparing a new directory.

Edit the baseline and workload YAML directly. Each baseline selects one policy.
Each baseline defines one policy. Repetitions, cases and offered concurrency
levels can still produce multiple trials using that same policy.

| Input | Sections |
| --- | --- |
| baseline.yaml | `kind: clawbox-baseline`, `name`, `environment`, `agent`, `inference`, `conditions`, `mechanisms`, `evidence`, `execution`, `measurement` |
| workload.yaml | `kind: clawbox-workload`, `name`, `tasks`, `submission`, `validation` |

`execution.dependency_install_concurrency` defaults to `null` and preserves any
explicit positive limit. `describe` reports it. Relative input/evidence paths
resolve against the owning YAML, manifest trace paths against the manifest.
Linux host storage paths must be absolute. Model and endpoint selection must
come from the baseline or its explicit ClawTune configuration reference;
OPENCLAW_MODEL_REF/OPENCLAW_BASE_URL no longer supply hidden defaults.
API credentials still use the named environment variable. YAML does not expand shell variables.
See [option relationships](option-relations.md) for dependencies and exclusions.

`sudo bash scripts/clawbox-pypi-cache {start|status|stop}` manages the host PyPI
cache. Docker and curl are required. `start` waits for its HTTP status endpoint;
verify an actual package install before freezing an experiment.

| Option | Default | Meaning |
| --- | --- | --- |
| `--bind-address` | required for start | Host IP reachable from Tool VMs |
| `--port` | `3141` | TCP port, integer 1–65535 |
| `--data-root` | `/var/lib/clawbox/pypi-cache` | Persistent cache root; reuse the existing path |
| `--upstream-proxy` | unset | HTTP(S) proxy URL used by the cache for upstream downloads |
| `--network-host` | false | Linux host networking; required for a host-loopback proxy, with the server bound to the selected address/port |

Changing container proxy/network settings requires stopping and retaining the
old container under a backup name before replacement with the same data root.
Use a new experiment run ID; no host apply, guest image rebuild, or retraining.

## Files, units, and schema layout

| Location | Meaning |
| --- | --- |
| `machine.env` | Shell settings for API addresses, source paths, image/template IDs, and default output root |
| `host.yaml` | Editable host topology, budgets, mounts, and service settings |
| `host.json` | Applied host profile and reusable image-build settings |
| baseline.yaml + workload.yaml | The two editable experiment inputs |
| `RUN/experiment.yaml` | Frozen input for one run ID |

Host-plan capacities and CLI options ending in `-gib` use GiB. VM YAML and
admission fields ending in `_mib` use MiB. One GiB is 1024 MiB; one MiB is
1,048,576 bytes. Durations are seconds unless named `_ms` or `_ns`. CPU lists use
Linux logical CPU IDs; vCPU counts do not reserve physical cores.

| Baseline section (unless marked workload) | Contents |
| --- | --- |
| `environment.runtime`, `environment.sandbox` | Shared Runtime/Tool VM identities and shapes |
| `environment.resources` | Physical scope, compute nodes, LOCAL/shared capacities, snapshot paths |
| Workload `tasks` | Input/manifest, cases, assignment, repetitions |
| `agent`, `inference` | OpenClaw driver and replay/API model configuration |
| `conditions` | Prediction source common to the comparison |
| `mechanisms` | Admission, runtime, placement, reclaim, shared memory, checkpoint, restore, deployment and coordination |
| Workload `submission` | Offered sessions, arrivals, repetition ordering, active-session bound and measurement window |
| `evidence` | Prediction artifacts, measured cost models, sharing proof |
| `execution` | Timeouts, creation/installation limits and diagnostics |
| `measurement` | Sampling, stabilization and worker output fallback; final validation is workload `validation` |

### Peer-local-SSD restore

Ordinary replay uses `environment.resources.snapshot_scheduling_storage` without
`execution.snapshot_branching`. Its fields are:

| Field | Default / unit | Meaning |
| --- | --- | --- |
| `topology`, `transport` | explicitly `peer-local-ssd`, `peer-network` | Two local XFS domains, no shared SSD/NFS fallback. |
| `local_roots` | required node-to-path map | Native domain roots matching the applied host receipt. |
| `local_capacity_mib_by_node` | required MiB per node, at least 512 | Must match applied loop pools; actual use and headroom are checked independently. |
| `transfer_endpoints` | required node-to-endpoint map | Each endpoint has `netns`, `address`, and TCP `port`. |
| `warm_root`, `warm_capacity_mib` | explicit path and MiB | Existing shared DRAM, bounded by applied capacity. |
| `host_receipt`, `native_control_socket` | required absolute paths | Applied topology evidence and installed native control endpoint. |
| `transfer_timeout_seconds` | 300 s | Timeout for a peer object transfer; positive seconds. |
| `restore_costs` | required for cost-aware | `provenance`, measured `effective_transfer_mib_per_second`, `import_seconds`, `native_restore_seconds`, `resource_wait_seconds`. Unknown costs cannot be represented as measured zeros. |

`mechanisms.placement.resume_policy` defaults to `home`; `load-aware` ranks
resource readiness, and `cost-aware` includes missing objects and active transfer
competition. `migration_threshold_seconds` defaults to 0.25 s and is nonnegative.
Resource readiness includes admission queue precedence and cumulative progress
batch charges, using the existing admission rules. A blocked creation request
can still be bypassed; a nonempty queue alone does not imply that restore waits.
Decision events include `restore_admission_queue` evidence. Predictions remain
estimates; compare them with observed admission waits in the phase report.
Initial `placement.policy` remains a separate decision. Non-home resume requires
managed compute nodes, pair snapshot reclamation and no fixed session CPU slots.
Cold snapshots use each node's local domain. Existing compression, lazy restore,
base cache, admission, CPU policies and memory borrowing retain their own controls.
Tiered peer qualification requires both WARM-to-COLD relocation and a direct
native COLD checkpoint. Requalify after changing the installed native build.

`submission.measurement_window_seconds` defaults to unset. Set positive seconds
for a fixed window starting at the first planned arrival; completion throughput
uses this full denominator and retains unfinished sessions. Early completion
does not shorten it. `arm_timeout_seconds` remains the overall execution bound;
allow setup time beyond the measurement horizon. Resource averages use the
actual observation interval rather than assuming idle samples after early exit.
At the configured cutoff, the general runner can retain `failed`/`TimeoutError`
because some sessions remain unfinished. For the peer comparison, require
`measurement_window_closed.reason: cutoff`, `cutoff_reached: true`, and
`peer-summary.json`'s `fixed_window_observed: true`, plus verified VM cleanup.
An earlier failure is not a completed observation window. Use the peer report's
completed/failed/unfinished counts and fixed-window rates for this comparison.
The peer report's agent-tool SSH operations include `exec` and filesystem bridge
operations, excluding backend-maintenance. This count is not the number of
LLM tool calls; one model-level call can perform several bridge operations.
Peer VM CPU accounting establishes an observed `cpu.stat` baseline for each
newly tracked cgroup, excluding cumulative usage from previous pool leases.
CPU before that observation is not measured. Missing, malformed or reset
counters increment `unobserved_counter_intervals` in `peer_cpu_sample` events
and `memory_sample.peer_cpu`; affected intervals are
unknown and must not be interpreted as zero. Audit older per-node CPU totals
that lack this baseline. Updating the recorder requires a new run ID and
qualification, but no host apply, image rebuild or retraining.

The host plan uses `schema_version: 2`, `topology: peer-local-ssd`, `state_root`,
`receipt`, and exactly two `nodes`. Each node explicitly supplies `numa_node`,
`cpus`, `backing_mount`, `filesystem_uuid`, `image_path`, `local_mount`,
`capacity_mib`, `netns`, `node_veth`, and `transfer_address` (IPv4 CIDR).
`backing_headroom_mib` defaults to 4096; `transfer_port` defaults to 19191.
TCP source helpers and target receivers use that node's `cpus` and a strict
`numa_node` memory policy through `numactl`. Receiver settings come from the
verified host receipt. Changing these fields requires idle host apply and a new
run ID; transfer buffers and newly allocated SSD cache pages use the compute
node's budget. Existing resident cache pages are not relocated by this policy.
`direct_io` defaults to `false`. Set it to `true` for SSD experiments to bypass
the outer backing-image page cache. Apply checks the actual loop device's DIO
flag; fully allocated, aligned files are required. This is separate from inner
filesystem page-cache residency and its reset command. Changing DIO requires a
new pool plan and a new run ID; old correctness-only pools can remain buffered.
For a linear LVM backing filesystem, configure both optional fields
`physical_device` and `image_allocation_receipt` from a successful
`reserve-image` result. Every extent must belong to that SSD, and the two nodes
must use different physical SSDs. The receipt binds image inode, capacity,
filesystem UUID and SSD serial. `apply` formats this owned blank reservation
with discard disabled and verifies placement again. It preserves existing XFS
contents. Filesystem metadata/log sharing remains a limitation of this topology.
`network.bandwidth_mbit_per_direction` is required, in decimal Mbit/s shared by
all transfers in that direction. `latency_ms_one_way` defaults to zero and
`mtu` to 1500. `shared_dram` supplies the existing `root`, `numa_node` (default 2),
`capacity_mib` and saved host-profile `receipt`. Host changes require apply;
capacity changes select a new image, preserving old data. Any experiment change
requires a new run ID. No field here requires prediction retraining or guest
image rebuild; native API changes require matching components and qualification.

`reserve-image` requires root, mounted XFS with reflink, `xfs_db`, `dd`, `lsblk`,
`blkid`, and `dmsetup` for linear LVM. Capacity is MiB and must be at least 512.
Allocation group numbers are inclusive and come from `inspect-backing`; choose
groups mapped wholly to the intended SSD. Without an end group, a single
allocation is attempted. With an end group, chunks start at `--chunk-mib`
(default 16384 MiB), shrinking to 1 MiB near fragmentation. Chunk assembly
writes zeroes outside experiment measurement and can take minutes. An existing
image is never replaced. A failed one-shot allocation removes only its recorded
new inode; a failed chunk assembly preserves owned partial files and receipts
for inspection. Repeat the same range command with `--resume-receipt FILE` to
verify the recorded inode, completed size and physical extents before continuing
the partial reservation. The image lock prevents concurrent reservation writers.
Free-space inspection alone is not proof of capacity: only a
`status: verified` receipt and subsequent host check qualify the reservation.

`snapshot-storage qualify-network` requires applied pools/helpers and a new
`--output` filename. `--size-mib` is per temporary object: default 128, range
16–1024 MiB. Two distinct objects run concurrently at the configured rate, then
at a temporary rate derived from measured throughput. The test also checks
same-object deduplication. It restores the configured network and removes its
fixtures even when qualification fails. These measurements describe transport
qualification, not VM throughput or physical SSD I/O.

`snapshot-storage cleanup-replicas` must run before unmounting the host pools.
`--run-id` names the completed/aborted run; `--control-socket` must expose the
matching native domain roots. Without `--apply`, it only previews. Apply first
requires native confirmation that VM references are released, then removes that
run's packages and collects only their unreferenced objects. Prepared templates,
other runs, results and the shared base cache remain. This command does not reset
the WARM inventory for a comparison; configure and record that inventory separately.

Base-cache plans accept `aliases: {TEMPLATE: [SSD_RAM_PATH, ...]}` (default empty).
Preparation verifies aliases byte-for-byte; local inode identities remain checked
on use. Aliases share one WARM file and one allocation charge. Changing aliases
requires idle-host cache preparation and fresh experiment qualification.

Historical `snapshot_branching` source and reports remain available for interpreting
simulated results; the old NFS storage plan is not a deployment path.

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
| `ip_local_port_range` | `null` | Preserve the current host ephemeral TCP range; explicit `[LOW, HIGH]` applies inclusive port bounds until reboot. Large-concurrency Cube command streams should use a reviewed wide range such as `[10000, 65535]`. |
| `cube_proxy_connect_timeout_seconds` | `null` | Preserve Cube default; explicit 3–300 seconds bounds guest connection setup |
| `cubelet_tap_init_num` | `2560` in new `host init` plans; `null` when omitted | Number of TAP devices to preallocate; 2560 covers 1280 Runtime/Tool pairs. Size for the planned live VM count. `null` preserves Cube's target; profile imports preserve saved settings. Changing the target requires host apply on an idle VM pool and may restart Cubelet/egress. Preparation increases service startup time. Use a new run ID; no image rebuild or retraining. This is a preallocation target, not a VM limit. |
| `cubelet_tap_url` | `http://127.0.0.1:9998/v1/network/taps` | Read-only Cubelet inventory endpoint for preflight. Counts distinct Ready devices without an owner; changing this URL only selects the probe endpoint. |
| `cube_api_nofile` | `65536` in new plans; `null` when omitted/imported without a value | Minimum descriptors per CubeAPI process. Requires installed systemd service and util-linux. Host apply uses the maintained NOFILE helper without restarting CubeAPI and preserves higher limits. `null` leaves configuration unchanged; readiness still checks the actual process. No image rebuild/retraining. |
| `required_kernel` | `null` | Exact host kernel release required by check/apply/preflight when this plan is selected. Does not install or boot a kernel. |
| `required_boot_parameters` | `[]` | Required whitespace-separated kernel command-line tokens, expressed as a YAML list. Check only; changing boot settings requires administrator configuration and reboot. Preserve host-specific display-off settings. |
| `cube_scheduler_overcommit` | `null` | Map actual Cube `InstanceType` to positive `cpu_ratio` and `mem_ratio` |
| `cube_scheduler_vm_count_ratio` | `null` | Positive VM-count quota multiplier by actual `InstanceType` |
| `cube_master_log_level` | `null` | Preserve Cube logging; `debug`, `info`, `warn`, or `error`. `warn` avoids verbose inventory logs in large runs. |
| `cube_master_db_timeout_seconds` | `null` | Preserve Cube's MySQL settings; explicit 5–300 seconds applies both read and write timeouts. Use a reviewed value such as 30 seconds when lifecycle SQL can exceed the upstream 5-second default under high concurrency. Requires idle-host `host apply`, which backs up `conf.yaml` and restarts CubeMaster; use a new run ID. |

Cube's quotas cover configured VM resources and differ from ClawBox's measured
admission limits. Preflight sums both VMs for each session's assigned case and
checks the largest arm demand; one larger case does not enlarge every session.
Size them from reported node quotas and requested VM shapes,
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

`tasks.repetitions` defaults to 1 and `session_assignment` to `unique`.
`single_case` repeats one case; `round_robin` cycles at least two cases; `unique`
requires enough distinct cases for the offered concurrency. Each case records
its `case_id`, source, prompt, repository/revision, replay reference, optional
validation command, and metadata. A case-level `sandbox` is a complete Tool VM
description, not a partial override. Optional `snapshot_reuse_probability` is a
value in `[0,1]` used by value-based snapshot residency.

`grouped` assigns each case a contiguous group of `tasks.rollouts_per_case`
independent sessions (default 1, positive integer).
The only concurrency level must equal case count times rollouts per case.
Each group shares its original replay trace and offered arrival instant; every
session creates separate Runtime and Tool VMs with independent mutable state.
This field does not limit active groups or sessions. Non-grouped assignments
require `rollouts_per_case: 1`. Qualification executes one rollout per case;
formal runs retain the configured group size. Changes require a new run ID and
qualification, but no host apply, image rebuild or retraining.

## Mechanisms

All fields below are under `mechanisms.GROUP`. Groups are independently editable;
validation checks their dependencies.

| Group | Principal fields and defaults |
| --- | --- |
| `admission` | `tool: off` (`off`, `fixed`, `predicted`); `startup_headroom_mib: 256`; `admission_margin_mib: 256`; fixed/non-command budgets unset; `prediction_reservation_scale: 1.0`; `small_tool_admission_bypass_mib: 4` |
| `runtime` | `memory_protection: false`; `cpu_release_boost: false`; `proactive_memory_reclaim: false`; `runtime_poll_interval_seconds: 0.5`; nested `pressure_control` below |
| `placement` | `policy: round_robin` (`os_default`, `round_robin`, `explicit`, `load_aware`); `template_affinity: false`; `placement_cpu_overcommit: 1024.0`; `template_balance_slack: 0.10`; `session_compute_nodes: []`; Tool-call CPU fields below |
| `idle_reclaim` | `policy: off` (`off`, `wait`, `gpu`); `scope: tool` (`tool`, `agent_and_tool`); `checkpoint_restore_headroom_mib: 1024`; `gpu_reclaim_min_remaining_seconds: 1.0`; `gpu_signal_max_age_seconds: 5.0`. GPU policy requires `serving_sim`; scope is independent; see the inference options below. |
| `shared_memory` | `borrow: off` (`off`, `hard`, `pressure`); `borrow_cooldown_seconds: 5`; `return_cooldown_seconds: 5`; `shared_memory_resident_headroom_mib: null` |
| `checkpoint` | `mechanism: incremental-cow` (`full-copy`, `incremental-cow`); `storage: disk` (`disk`, `warm-only`, `tiered`); `residency: lru` (`lru`, `value`, `next-use`); transfer limits below |
| `restore` | `prewarm: off` (`off`, `eta`), `prewarm_lead_seconds: 2.0` (positive seconds, restore budget including margin). Requires snapshot reclaim and serving_sim; independent of victim selection and scope. `promotion: off` (`off`, `if_fit`) independently controls whole-lineage COLD-to-WARM relocation before restore. |
| `deployment` | `mode: managed` (`managed`, `os-default`); `resident_capacity_accounting: false`; `lifecycle_reservation_scale: 1.0` |
| `coordination` | `action_selection: context-first` |

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

`mechanisms.shared_memory.shared_memory_resident_headroom_mib` enables
aggregate-resident WARM accounting. `null` keeps the default conservative rule
that charges every borrowed VM's configured capacity. A positive MiB value
charges sampled shared-NUMA live residency plus that fixed growth reserve;
in-progress rebinds still charge their full configured capacity. The field
requires shared-memory borrowing and must be smaller than
`environment.resources.warm_memory_capacity_mib`. Use it with `tiered` storage
so eligible WARM snapshots can spill to COLD when live growth consumes the
reserve. Changing it requires a new run ID, but no host apply, image rebuild,
retraining, or repeated capability qualification.

`checkpoint.residency: next-use` is active only with tiered storage,
idle reclaim (`wait` or `gpu`, either scope), and
`inference.backend: serving_sim`. Each valid `response_forecast.remaining_s`
is reduced by transport age and converted to an absolute monotonic deadline for
both Runtime and Tool. The deadline carries an independent observation expiry;
expired predictions cannot rank either a candidate or a victim. With concurrent
requests, all waits must have valid forecasts; the earliest deadline is used.

If the new checkpoint already fits, WARM admission does not require an
estimate. Otherwise, only unpinned, non-retained WARM generations with known
deadlines strictly later than the candidate (`ti > t0`) are eligible. They are
relocated latest-deadline first until their actual reclaimable allocated bytes
cover the deficit. Equal, earlier, or missing victim deadlines are never
guessed; if eligible bytes are insufficient, the candidate is written directly
to COLD. Physical-safety and restore-expansion spills bypass this comparison.
Restore promotion is independent of next-use ranking and defaults to `off`.
With `mechanisms.restore.promotion: if_fit`, the pool makes a no-spill reservation
for the complete VM snapshot upper bound. Success relocates the complete lineage
to WARM before Cube connects the VM; failure leaves the mixed/COLD lineage
unchanged. `if_fit` requires snapshot pause, tiered storage, WARM capacity, both
roots and qualified native relocation. To reproduce the former next-use restore
behavior, explicitly select `if_fit`; frozen historical results are not changed.
Changing either setting
requires a new run ID, but no retraining or guest-image rebuild; host apply is
needed only when storage paths/capacity or installed native components change.

`coordination.action_selection: context-first` does not compare borrowing and
checkpointing with a common cost score. For active Tool admission/progress it
tries borrowing that Tool first and one safe idle checkpoint only as fallback.
For idle/model-wait pressure it tries one safe checkpoint first and one live-VM
borrow only as fallback. Each action is attempted at most once. The requester is
explicitly excluded from checkpoint selection; active Tools remain ineligible.

Resident-capacity accounting requires managed deployment, Tool admission off,
and no shared-memory borrowing or proactive reclaim. Its scale must be greater
than zero and at most one. Resident-capacity accounting holds fixed VM charges
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

Fields are under `mechanisms.placement` in the baseline. Select C0, C2 or B
explicitly; comparing methods uses separate baseline files and the same workload.

| Field | Default | Meaning |
| --- | --- | --- |
| `tool_call_cpu_policy` | `b` for Full; `c0` for generic neutral designs | `c0`: full selected-NUMA mask; `c2`: original elapsed-time baseline; `b`: prediction-triggered placement. Arm names do not select behavior. |
| `session_cpu_slots` | `[]` | List of `{node_id, cpus}` slots; `cpus` is a Linux logical CPU list. Each slot serves one complete Runtime/Tool pair at a time. Both VMs' vCPU threads stay in that mask; reuse occurs after session cleanup. Requires managed resident C0 `round_robin` placement, compute nodes and a managed cgroup. Masks must be within their node and must not share physical cores, including SMT siblings. Effective concurrency is also capped by slot count. CPU-cluster masks may still share outer L3 cache. Requires a new run ID and qualification; host apply only if the allowed node/CPU pools change, no image rebuild or retraining. |
| `tool_call_cpu_min_duration_seconds` | 1 | Seconds; for B, a valid pre-call ClawTune Tool duration p50 must be strictly greater than this value to request binding. B has no elapsed-time delay or duration-based retry. C2 instead waits at least this elapsed time before binding. |
| `tool_call_cpu_duration_source` | `tool` | B only. `tool`: use ToolKB only. `lattice`: prefer LatticeKB, falling back to ToolKB only if its duration target is missing/unavailable. Missing predictions skip binding. No units. Change `mechanisms.placement.tool_call_cpu_duration_source` in YAML; requires a new run ID and qualification, no host apply, image rebuild or retraining. Inactive for C0/C2. |
| `tool_call_cpu_max_utilization` | null | B only. Optional fraction in `(0,1]`; when set, a cluster must have measured busy CPU utilization strictly below this limit. YAML `null` disables this utilization veto; fresh load measurements, binding-slot limits and PSI expansion still apply. Includes all tasks running on the cluster. Existing explicit limits remain in effect. |
| `tool_call_cpu_poll_interval_seconds` | 0.25 | C2/B. Seconds between shared CPU-load and bound-VM PSI samples, independent of memory sampling. Admission uses the latest sample only when at most twice this interval old. |
| `tool_call_cpu_group_max_active` | 2 | Maximum slots per group: B counts currently narrowed VMs; C2 retains the slot after expansion until the last active call completes. No exclusive core ownership or execution-concurrency limit |
| `tool_call_cpu_pressure_ratio` | 0.10 | CPU PSI threshold for expansion |
| `tool_call_cpu_pressure_samples` | 2 | Minimum consecutive high-pressure samples; B also requires the duration condition, C2 does not. |
| `tool_call_cpu_pressure_duration_seconds` | 2.0 | B only. Positive seconds of consecutive measured intervals at or above the PSI threshold before expansion. A low-pressure interval or counter reset clears the accumulated duration. Independent of polling frequency; not a minimum binding lifetime. |
| `tool_call_cpu_include_smt` | `true` | Include all allowed SMT siblings in the selected cluster; `false` selects one permitted logical CPU per physical core. This is an affinity mask, not exclusive core ownership. |
| `tool_call_cpu_group_order` | `preferred` | B only. After minimizing currently bound VM count, `preferred` ranks the previously used cluster before utilization; `utilization` ranks measured busy CPU fraction before previous-cluster preference. Stable CPU-mask ordering breaks ties. Only changes initial binding; no live migration. |
| `tool_call_cpu_observe_wait` | `false` | B only. Record host vCPU schedstat runtime and runqueue-wait deltas only during binding, at the existing poll interval and binding boundaries. Diagnostic only; missing/reset counters have no measured delta. `wait_demand_ratio` is wait / (run + wait), not wall-time PSI; idle intervals report null. Kernel counters can settle at scheduling boundaries, so this is not instantaneous pending-wait age. No host apply, image rebuild or retraining; use a new run ID. |

Both `c2` and `b` require managed placement, explicit compute-node CPU pools,
a verified VM cgroup path, readable Linux topology and at least two eligible
LLC clusters. They affect only `agent-tool` `exec` calls, not filesystem or
maintenance calls. SMT defaults to true; the mask permits sibling threads but
does not reserve cores.

C2 is the fixed historical algorithm: elapsed-time binding, selection by slot
count then previous cluster then CPU utilization, historical `/proc/stat`
accounting including guest ticks, and PSI confirmation by sample count only.
Its sampler uses `mechanisms.placement.tool_call_cpu_poll_interval_seconds`
(default 0.25 seconds), independently of memory measurement. Set 1 second
explicitly to retain the historical Full C2 cadence. The B-only fields above are inactive for C2.
Slots are held through expansion until the last active call completes.

B with admission `off` or `fixed` reads native duration targets without requiring
a memory estimate or host-memory calibration. The projected memory estimate is
`null`; native model targets remain in the prediction record. Fixed admission
uses `static_tool_memory_mib`. With `predicted` admission, memory selection and
calibration remain mandatory. Artifact, repository, environment and command
identity checks still apply. This holds for command-table, runtime, cold-start
and host-live sources. Use a new run ID and qualification, and prepare a new
serving_sim bundle for its prediction-validation receipt; no host apply, image
rebuild or retraining is required.

B binds before execution, after resource admission and VM restore, using the
verified pre-call ClawTune prediction without a second query. By default it selects
the `duration_ms` p50 from `models.tool`, without falling back to LatticeKB.
With `tool_call_cpu_duration_source: lattice`, it selects `models.lattice`, falling back to `models.tool` only when
the LatticeKB duration target is absent or unavailable. A valid short or zero
prediction does not trigger fallback. Selection is independent of the memory
target and its admission fallback; TrieKB and tool-class predictions are not
duration fallbacks. A single supplied `call_prediction` is used only when the
canonical model collection is absent. Missing estimates,
missing/stale CPU samples, full slots, or the optional utilization veto skip
binding without waiting for a slot. Invalid available predictions and
prediction-service failures are errors. Without a prediction provider, B cannot
initiate binding. B excludes guest/guest_nice from the total because they are
already included in user/nice.

B releases slots on expansion and suppresses rebinding until the current batch
of calls finishes. A short call can share an existing long call's VM affinity,
but cannot retain it after the final eligible long call finishes. Expansion
returns to the full selected-NUMA pool; expanded VMs still contribute to real
CPU utilization.

Decision events record `duration_prediction_model`, `duration_prediction_backend`
and `duration_prediction_unavailable_models`. B's summary records
`duration_prediction_source` and `duration_prediction_policy: [tool]` by default,
or `[lattice, tool]` for the LatticeKB option. These identify the duration source
without reinterpreting memory-admission metadata.
Results record mode, trigger, slot accounting and effective polling interval in
`performance.tool_call_cpu_policy`. New replays of earlier prediction-based
trials must use `b`; `c2` now selects the original baseline. Changing any policy
requires a new run ID and qualification. Unchanged CPU pools and templates need
no host apply, image rebuild or retraining.

### LS/BE CPU QoS

| Field | Default | Meaning |
| --- | --- | --- |
| `mechanisms.placement.cpu_qos_policy` | `"off"` | `off` keeps ordinary scheduling; `idle` applies `SCHED_IDLE` to BE host vCPU threads; `idle_core` also applies distinct Linux core-scheduling cookies to LS and BE. Quote `"off"` in YAML. |
| `mechanisms.placement.cpu_qos_group_idle` | `false` | With `idle` or `idle_core`, also set each BE VM's cgroup `cpu.idle=1` and verify LS groups use `cpu.idle=0`. This lowers the whole VM group, including its helper threads, relative to sibling LS groups. Policy readback uses `cpu.idle`; `cpu.weight` is recorded as a kernel-dependent diagnostic. Requires explicit LS/BE labels, writable cgroup v2 `cpu.idle`, and host privileges. Use a new run ID and qualification; no image rebuild or retraining. Original group settings are restored during cleanup. Runtime memory protection remains enabled; CPU release boosts do not override idle BE groups. |
| `tasks.cases[].qos_class` | `neutral` | `ls`, `be`, or `neutral`; also accepted on each manifest task/case. `idle_core` requires every offered session to be explicitly `ls` or `be`. |

Policies cover host vCPU threads of both Runtime and Tool VMs; auxiliary VMM
threads retain their normal policy. LS/neutral vCPUs use `SCHED_OTHER`.
`idle_core` uses one shared cookie per class and verifies cookie/policy readback
after creation and restore. It requires managed placement, a verified experiment
cgroup, host scheduler privileges (`--sudo`), and native `PR_SCHED_CORE` support
from a kernel built with `CONFIG_SCHED_CORE=y`. Include SMT siblings in the CPU
pool when measuring SMT interference. Missing capability or failed application
is an error; there is no static-partition fallback.

Changing QoS or case classification requires a new run ID and qualification,
with no image rebuild or retraining. Apply host configuration only if CPU/NUMA
pools change. Installing a supporting kernel requires a reboot and host recovery
before running the experiment.

Labelled sessions also emit `cpu_qos_usage` in memory samples. This measures
verified Runtime and Tool vCPU `utime + stime`, excluding auxiliary VMM threads.
Per-class `observed_load_fraction` is observed CPU seconds divided by elapsed
seconds and the number of logical CPUs in the configured compute-pool union;
multiply by 100 for percent. The event records that CPU mask and denominator.
Use intervals with `coverage: complete` for load calibration. A thread that
disappears before its next reading sets `lost_exit_tail` and makes cumulative
coverage partial; missing CPU time is not replaced with zero. Partial intervals
retain an observed lower bound when available. Registrations that cross a sample
boundary set `unaligned_interval_threads` and leave load fractions unavailable
until aligned readings resume. Cumulative CPU seconds cover only observed ticks.

### CPU QoS analysis

`python3 scripts/analyze_cpu_qos.py RUN_ROOT` analyzes existing results and
writes JSON to stdout. It does not execute workloads.

| Option | Default | Meaning |
| --- | --- | --- |
| `--arm-id ID` | sole result or current running arm | Select an arm when several results exist. |
| `--cohorts FILE` | session result `qos_class`; frozen cases while running | JSON object mapping every offered session index to `ls` or `be`, e.g. `{"0":"ls","1":"be"}`. Include failed/missing sessions. |
| `--baseline-json FILE` | none | This script's analysis of the same unique LS case IDs/traces/commands with no BE. Session indices may differ; retained arrival offsets are compared. |
| `--slo-multiplier FACTOR` | required with baseline | Positive permitted p95 factor; `1.20` allows 20% inflation. |
| `--slo-metric` | `execution` | `execution` measures Tool execution; `critical_path` spans host admission callback entry through completion callback return; `queued_to_completion` uses host queue/completion events. Missing timings remain unavailable. |
| `--window-origin` | `arrival` | Throughput origin: earliest offered arrival, earliest `agent` execution, or `arm` start. |
| `--warmup-seconds` | 0 | Nonnegative offset from the throughput origin. |
| `--window-seconds` | observed batch | Positive fixed window duration. Default ends at final completion, or arm end for an incomplete batch. |
| `--observation-start-unix-s` | none | Explicit window start in Unix seconds, overriding origin/warmup; useful for the final session submission timestamp. |
| `--observation-end-unix-s` | none | Analysis cutoff in Unix seconds. Excludes later Tool/task completions and cleanup; does not stop a run or change LLM timing. |
| `--timeseries-bin-seconds` | 10 | Positive interval length, seconds, for LS/BE/LS+BE step rates and cumulative task counts; the final bin may be shorter. |
| `--timeseries-csv FILE` | none | Also export the JSON report's interval rows as CSV. |
| `--output FILE` | stdout | Analysis JSON destination. |

The selected window affects throughput and the `observed_window_*` latency
fields; the original `execution`, `critical_path`, and `queued_to_completion`
fields cover the whole observed arm. `classes.ls`, `classes.be`, and
`classes.all` share the throughput denominator.
`tool_steps_per_second` counts completed agent-tool calls with successful or
exactly matched expected outcomes. `observed_completed_tool_steps` also includes
unverified outcomes; `validated_session_tool_steps_per_second` additionally
requires final task validation. Task counts separately identify completed,
failed, and incomplete sessions. Useful session throughput counts qualifying
sessions completing in the window, including those started earlier.

For bounded observations, `observed_tool_steps_per_second` counts all completed
agent-tool calls, including nonzero exits, and `nonzero_exit_tool_steps_in_window`
reports those exits separately. `completed_tasks_in_window` counts validated
tasks finishing in that interval. `observed_window_execution` reports available
Tool execution timings in the interval; compare its count with completed Tool
steps because unfinished sessions may not yet have persisted their timings.
Unfinished calls are censored, not assigned zero latency. A window p95 comparison
is an observed result and does not establish a whole-trace SLO guarantee.

Without an arm result, analysis reads `run-state.json`, the frozen experiment,
events, and session progress. Its `in_progress` report is provisional and ends
at the last observed timestamp, including when a longer window was requested.
Unfinished tasks never count as completed, and missing exit codes do not count
as successful steps. An incomplete run cannot satisfy the SLO verdict. JSON and
CSV time series use the same bins for all three cohorts and mark a shorter final
bin. These are finite-batch metrics, not a maximum sustainable throughput
estimate. Analysis changes require no new run, host apply, image rebuild, or
retraining.

### Fixed shared-DRAM base cache

`environment.resources.base_template_cache` is `disabled` by default; `required`
requires an active, validated cache and incremental-CoW lazy restore. A host/spec
mismatch fails before VM arrivals. The worker records the prepared cache receipt
in `shared_memory_pool_initialized`; it does not copy or hash bases per VM.

`experiment base-cache init PLAN.yaml --baseline BASELINE.yaml --workload WORKLOAD.yaml --capacity-mib N
--template ID [--template ID ...]` resolves RAM paths from native XFS catalogs.
It refuses to overwrite a plan. The Shim is resolved from each template's
recorded component version; `--vmm-binary` is for nonstandard component roots.
The global `/usr/local/bin` Shim may differ from the version used by templates.
`experiment base-cache prepare PLAN.yaml` previews; `--apply` prepares/activates.
`status` verifies identities; `status --verify-bytes` also checks SHA-256.
`cleanup` previews owned cache removal; `cleanup --apply` removes it on an idle
host. Original templates and COLD snapshots are retained.
`cleanup --config PLAN.yaml` also finds interrupted preparations owned by that
exact plan; preview before adding `--apply`.

| Plan field | Meaning |
| --- | --- |
| `warm_root` | Existing tmpfs root with `noswap` and `mpol=bind:<numa_node>`; run host apply first |
| `numa_node` | Shared DRAM NUMA node ID |
| `capacity_mib` | Positive maximum cache data allocation in MiB; part of, not additional to, the experiment WARM budget. Selected RAM files are fully materialized, including zero pages, before arrivals |
| `vmm_binary` | Actual installed patched Shim file; preparation checks its cache capability and records its identity |
| `templates` | Explicit template identity to immutable SSD RAM-file path mapping; same-inode aliases share one cached copy |

The native registry is `/run/clawbox/base-cache.json`; cache data is under
`warm_root/.clawbox-base-cache/`. Both are prepared state, not template storage.
The cache changes mappings host-wide for selected bases. Select/configure it
before qualification and keep it fixed until all VMs are gone. Original SSD
identities remain in lineage, including after WARM checkpoint or COLD spill.
Source or cache identity changes fail rather than silently weakening the policy.
Reboot clears the registry; restore the WARM mount and repeat preparation.
The native update needs a release rebuild/install and a new run ID, but no guest
image rebuild, predictor retraining, or kernel change.
For a tested Shim-only change, `sudo bash scripts/install-incremental-cubesandbox.sh
--shim-only BUILT_SHIM INSTALLED_SHIM` atomically replaces the versioned binary
identified by the plan, preserves a backup and prints the rollback command.
It refuses live VMs or an active cache and does not restart unrelated services.
Remove the active cache with `experiment base-cache cleanup` followed by
`experiment base-cache cleanup --apply` before installation, then repeat
`experiment base-cache prepare PLAN.yaml --apply` before qualification.

`build-cubesandbox-release.sh --lifecycle-components --source DIR [--jobs N]`
builds Cubelet, Shim and cube-runtime using an existing builder image. `N` is
the positive compiler parallelism, default `1`; this mode needs no guest asset
bundle. `install-incremental-cubesandbox.sh --lifecycle-components CUBE_SOURCE
CLAWBOX_DIR WARM_ROOT COLD_ROOT` installs those components and builds/installs
the Zstd helper on an idle standalone host. It preserves backups and restores
the previous binaries if installation fails. Finish qualification after startup
before launching a new run ID; changing these host binaries requires no guest
image rebuild or predictor retraining.
An already active Cubelet is kept running when its binary and drop-in settings
are identical, so a Shim/runtime/helper update does not rebuild the TAP pool.

This mode enables `CLAWBOX_CROSS_TIER_LINEAGE=1` in a Cubelet systemd drop-in,
retains lazy private mappings and sets the explicit WARM/COLD roots. COLD-to-WARM
successors reuse managed SSD layers; exclusively owned delta files remain
compressed. WARM-to-COLD also shares ancestors in place, including managed
tmpfs layers. Dirty pages supersede inherited ranges, including zero-value
updates. A COLD successor can therefore depend on WARM and is not protected
against loss of those tmpfs files.
Crash rollback remains a separate opt-in with matching API/Master
and database prerequisites.

`CLAWBOX_CHECKPOINT_LOG` selects aggregate native phase JSONL (default for this
installer: `/data/log/clawbox-checkpoint-phases.jsonl`); changing it requires a
Cubelet restart. Per-range logging remains disabled. Compression/decompression
metrics include `phase_seconds.read`, `codec`, `write` and `sync`, in wall-clock
seconds. `write` includes sparse-page scanning; `sync` includes final index
publication for compression. Missing helper timings are `null`. These durations
are nested within the lifecycle stage, not additional overhead to add again.
`snapshot_metrics.checkpoint_api_timing` records `pause_rpc` and
`snapshot_metadata` with Unix/monotonic start and finish timestamps, wall time
and caller-thread CPU time. Native pause completion is synchronous; the client
validates the resulting lineage without a second status-poll request.
Incremental checkpoint/spill lifecycle records also expose
`lineage_snapshot_id`, `lineage_parent_snapshot_id`, and a compact
`lineage_layer_map`. Each layer entry carries its backing identity, managed
layer ID when present, declared range count/bytes, path kind and actual
`warm`/`cold`/`template`/`external` tier. This reads native metadata only; it
does not scan RAM or add a per-checkpoint sidecar/fsync.
For an incremental successor, `transferred_bytes` and `dirty_bytes` count
newly dirty bytes. Shared ancestor layers on either tier do not count as
transferred. A missing parent or invalidated dirty history still requires a
full base.

For peer local-SSD restore, `build-cubesandbox-release.sh --peer-components`
builds API/Master/Cubelet, but does not build or install the required Shim.
The matching Shim must permit `fadvise64` in its VMM seccomp filter for direct
COLD checkpoints. Build it from the same prepared source with:

```bash
(
  cd "$CUBE_SOURCE_DIR"
  make builder-run BUILDER_CMD='cd /workspace/CubeShim && cargo build --release --locked --bin containerd-shim-cube-rs'
)
```

Then install peer components before the Shim-only replacement so the peer
backup contains the original complete component set. The built Shim is
`$CUBE_SOURCE_DIR/CubeShim/target/release/containerd-shim-cube-rs`; the standard
release's installed path is
`/data/cubelet/root/component_versions/cube-shim/v0.7.0-clawbox-standalone/bin/containerd-shim-cube-rs`.
The [installation sequence](installation.md#add-two-local-ssd-storage-domains)
provides the ordered commands and full rollback. A full fresh release build
already includes this Shim. These host changes require renewed qualification
and a new run ID, not guest-template rebuilding or predictor retraining.

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

With the current native build and `CLAWBOX_CROSS_TIER_LINEAGE=1` (enabled by the
lifecycle installer), incremental inheritance shares ancestor layers in both
directions. A COLD successor retains references to needed WARM ancestors; it
does not copy them to SSD for durability. Its tier names the new snapshot's
destination, not the placement or persistence of its complete dependency chain.
Losing WARM data invalidates every dependent snapshot, including COLD ones.
`managed_warm_bytes` reports WARM ancestor allocation not already charged through
a WARM generation, deduplicated by device/inode. It remains in `committed_bytes`
until native GC releases pages or removes the files; ordinary pool observations
refresh physical allocation at most once per second. Such ancestors cannot be evicted
independently by the generation-level spill policy.
To use this behavior, rebuild/install the lifecycle components using the guide,
update ClawBox, and use a new run ID with fresh qualification. No guest image
rebuild, kernel change, or predictor retraining is required.

Checkpoint cleanup computes the union of referenced page ranges across both
tiers, including retained rollback records and the source checkpoint of a live
restored VM. It punches holes only in unreferenced pages of shared raw files;
when a managed file has no references, it removes that file. Immutable templates
are excluded. Pending publications and unknown hardlinks retain data until their
ownership is resolved. The filesystem must support `SEEK_DATA`/`SEEK_HOLE` and
hole punching (the supported tmpfs and XFS storage paths do).

Cleanup is the trigger: there is no background timer or per-write reference
counter. The default collection grace is zero. The existing Cubelet environment
variable `CLAWBOX_MANAGED_LAYER_GC_GRACE_SECONDS` accepts nonnegative seconds and
delays whole-file deletion only, not reclamation inside retained files; changing
it requires a Cubelet restart. A failed explicit cleanup returns an error;
automatic predecessor cleanup remains best-effort and may defer reclamation to
a later cleanup. Publishing checkpoint references and collecting pages use the
same ordered filesystem locks. Scanning all checkpoint metadata while holding
those locks can delay publication as checkpoint/range counts grow. Reclamation
skips already sparse holes and never copies retained page contents. Compression
container reuse is invalidated when the decoded raw file changes.

Page-local snapshot compression is configured under `environment.resources`,
because it is fixed for every arm in one run:

| Field | Default | Meaning |
| --- | --- | --- |
| `snapshot_compression` | `zstd-page` | Skips 1 MiB chunks reported entirely as holes by the filesystem and stores every remaining chunk as an independent Zstd level 1 frame, including allocated all-zero data; `none` explicitly selects an uncompressed baseline |
| `snapshot_compression_binary` | `/usr/local/bin/clawbox-zstd-page-snapshot` | Absolute path of the native helper installed by the host setup instructions |

Build the standard helper once per installed ClawBox native-helper revision on
a host that provides `libzstd.so.1`:

```bash
sudo bash scripts/build-zstd-page-snapshot.sh /usr/local/bin/clawbox-zstd-page-snapshot
```

WARM and COLD use the same format and parameters. ClawBox restores Cube's native
sparse files before resume or relocation, and does not compress external template
layers or an inode with references outside the snapshot. Compression changes need
a new run ID and fresh qualification, but no VM image rebuild or KB retraining.
Allocated all-zero chunks count as `zstd_chunks`; `hole_chunks` counts skipped
filesystem holes. Decompression still omits zero-page writes to retain sparsity.
WARM allocated-byte savings are released tmpfs DRAM while parked;
COLD allocated-byte savings are disk savings only. Pool admission remains charged
at the conservative native allocation because restore temporarily recreates it.

## Prediction and evidence

`conditions.prediction_source` maps `kb` to CLI mode `runtime`, `cold-start-kb`
to `cold-start`, `host-live` to `host-live`, and `command-table` to `frozen`. Its schema default is `kb`.
`evidence.prediction_artifact` points to the selected immutable seed or artifact.
Use `clawbox experiment train --help` for training inputs and options. Keep
training evidence separate from evaluation runs.

Evidence models that remain part of the supported workflow require a nonempty
`provenance` string:

| Evidence field | Required content |
| --- | --- |
| `snapshot_residency_costs` | WARM and COLD restore seconds/GiB |
| `idle_reclaim_costs` | WARM checkpoint/restore seconds/GiB; optional COLD costs |
| `template_base_sharing_artifact` | Path to reusable proof for the exact Tool templates and node |

Use measured values from the intended deployment. The JSON Schema lists exact
field names and required ranges; `describe` reports which arms need each model.

## Execution

### Tool placement study

`execution.tool_placement` defaults to `null`. When enabled, it requires OpenClaw
replay, managed resident C0 placement, exclusive parent session slots, and
admission/runtime control off. Each group holds one parent Runtime/Tool pair and
up to four additional Tool VMs; configured capacity checks include these replicas.
Replica restores share `execution.sandbox_create_concurrency` with parent VM
creation; this limits preparation, while ready tool groups can run concurrently.

| Field | Default | Meaning |
| --- | --- | --- |
| `targets_file` | required | JSONL measured-tool inventory; one row per target |
| `prefix_sequences_file` | required | JSONL ordered Agent execution identities per trace |
| `groups` | required | List of `{parent_node_id, packed, spread, within_numa?}`, in parent session-slot order; each layout lists four logical CPU IDs, one physical core per replica |
| `isolation_domain` | `llc` | Reserve disjoint outer LLC domains across groups, or use `cluster` to reserve disjoint four-core hardware clusters while permitting shared upper-level LLC |
| `strata` | `[long, short]` | Inventory strata selected for measurement |
| `repetitions` | `1` | Paired repetitions per target, at least 1; every layout runs once per repetition. A single pair has no repeat uncertainty estimate |
| `submission.random_seed` | `0` | Reproducible randomized layout order within each repetition |
| `control_timeout_seconds` | `86400` | Seconds allowed for a measurement callback; original Tool execution deadlines remain separate |
| `max_targets_per_case` | `null` | Optional target limit for a small real-VM gate; omit for full selected coverage |

Inventory rows require `target_id` (stable hexadecimal identity), `case_id`,
`operation`, `profile_command_sha256`, and `stratum` (`long`, `short`, or an
explicit excluded stratum such as `censored`). Preserve measured baseline fields
and raw artifact paths alongside these keys. Each prefix row is
`{"case_id":"CASE","executions":[{"target_id":"HEX","operation":"exec","profile_command_sha256":"SHA256"}]}`
with all Agent executions in observed order. Matching uses each operation/hash
occurrence within the recorded trace; random execution IDs are not portable
between replays. The observed replay prefix is retained for comparison.
For analysis, preserve `network_command_candidate` when available: the
`clean_success` subset requires a zero exit, no special-case flags and no network
candidate label. Candidate commands remain in the full results.
The analyzer also screens the saved command for common package installation and
download commands; a path containing `conda` alone is not a network command.

The primary makespan is the Runtime controller monotonic interval from common SSH launch
release to the last of four SSH process completions. It includes command dispatch
and collector finalization, excludes restore/artifact copying/cleanup, and retains
launch skew plus raw guest timing for interpretation. Very short calls require
care with this overhead. Different tool groups cannot share their configured isolation domains;
cross-NUMA layouts also change memory locality/bandwidth and are not an LLC-only
causal comparison. All parameters require a new run ID and qualification; none
requires rebuilding task images or retraining.

Fields below are under baseline `execution` except the rows explicitly prefixed
`submission.`, which belong to the workload file:

| Field | Default | Meaning |
| --- | --- | --- |
| `kb_scope_policy` | `pair` | Host-live knowledge isolation; currently only independent pairs are supported. |
| `kb_processes` | 4 | Host-live compute processes, 1–64. Each pair has one fixed owner; all three KBs and the existing admission fallback policy remain active. Uses host control CPU resources. Requires a new run ID; no host apply, image rebuild or retraining. |
| `kb_queue_capacity` | 1024 | Maximum outstanding requests per KB process, positive integer. Includes queued and executing requests; exhaustion fails explicitly. Increase for a new run if the recorded owner queue fills. |
| `kb_http_timeout_seconds` | 30 | Positive finite seconds for prediction and the total observation delivery deadline. |
| `kb_observation_retries` | 2 | 0–5 additional observation delivery attempts within that deadline. Commands are never retried. |
| `clawtune_signals.endpoint` | `null` | HTTP(S) receiver URL reachable from Runtime VMs; unset disables external publication. URLs cannot contain credentials or fragments. The destination is added to Runtime outbound rules. Requires the updated Runtime plugin/sidecar. |
| `clawtune_signals.admission_eta` | `true` | Correct host-live shell Session ETA with observed admission waiting and guest execution boundaries. Requires the updated Runtime adapter and a Tool bridge supporting progress events (qualification checks support; rebuild older Tool images). Online conditional median from up to 512 recent executions, at least 3 comparable completed waits; ongoing waits are censored. Polls every 1 s; evidence expires after 2 s. Cold/unsupported/stale cases return null. New run ID; no host apply or KB retraining. `false` restores upstream execution-only ETA. |
| `clawtune_signals.token_env` | `null` | Name of the worker environment variable containing the receiver bearer token. Missing configured tokens fail the run with a remedy. The frozen spec stores only the variable name. |
| `clawtune_signals.tool_events` | `true` | Publish tool/model signals as well as Session snapshots. `false` publishes Session snapshots only; internal measurement and learning remain active. |
| `clawtune_signals.queue_size` | 1024 | Positive maximum pending tool/model messages per sidecar. Session delivery separately retains the latest snapshot per session. |
| `clawtune_signals.timeout_seconds` | 1.0 | Positive finite timeout per delivery attempt, seconds. Session attempts use the smaller of this value and 0.2 seconds. |
| `retain_clause_telemetry_artifacts` | `false` | Write full Tool clause-telemetry files for diagnostics. Compact learning and integrity records are always retained. |
| `submission.concurrency_levels` | `[1]` | Positive offered session counts |
| `submission.max_active_sessions` | `null` | Optional positive limit on simultaneously live sessions (Runtime/Tool VM pairs). All offered sessions still run; freed slots admit the next session. Host quota and storage preflight use the largest possible active set. A new run ID is required; no host apply, image rebuild, or retraining. |
| `sandbox_create_concurrency` | 8 | Independent preparation and actual-create limits, each in VM pairs; a pair can issue two concurrent creates, subject to lifecycle limits |
| `dependency_install_concurrency` | `null` | Optional arm-wide bound on active apt/apt-get/pip installs |
| `apt_cache_proxy_url` | `null` | Explicit Tool-only `http://host:port` APT proxy, with either internet policy. Requires a reachable proxy; change requires a new run ID, no image rebuild. |
| `pypi_cache_index_url` | `null` | Explicit Tool-only `http://host:port/.../+simple/` pip index, with either internet policy. Requires a reachable cache; change requires a new run ID, no image rebuild. |
| `cube_request_timeout_seconds` | 130 | Per-request Cube HTTP deadline and total retry budget for an idempotent sandbox resume, seconds. A new run ID is required; no host apply, image rebuild or retraining. |
| `policy_progress_timeout_seconds` | 30 | Total deadline in seconds for each Tool start/end acknowledgement; at most three idempotent notification attempts share it. Commands are never retried and policy rejection remains fatal. Requires the matching Runtime shim and Tool bridge images; a changed value needs a new run ID. |
| `cube_create_capacity_retry_seconds` | 60 | Retry window for definite Cube `130597` rejection; 0 disables retries |
| `worker_thread_switch_interval_seconds` | 0.001 | Python handoff interval, `(0,0.1]` seconds |
| `worker_cyclic_gc_enabled` | false | Experiment worker and KB owners: defer automatic cyclic garbage collection until the worker finishes. Reference-counted objects are still released immediately; unreachable cycles may use extra host memory during the run. Set true to use Python's automatic cyclic collector. Changes require a new run ID, but no host apply, image rebuild, retraining, or repeated template qualification. |
| `randomized_order`, `random_seed` | `true`, 0 | Arm-order randomization and reproducible seed |
| `arm_timeout_seconds` | 3600 | Arm execution deadline, seconds |
| `arm_finalization_timeout_seconds` | 300 | Additional supervisor allowance for cleanup/results, seconds. Failed-session cleanup cancels pending model responses/replay waits and preserves their error records. |
| `command_timeout_seconds` | 300 | Tool command deadline, seconds. Each admitted invocation uses a fresh SSH connection so earlier calls cannot consume its connection lifetime. Changing the deadline requires a new run ID, not an image rebuild. |
| `replay_tool_timeout_multiplier` | 1.0 | Unitless multiplier, 1.0–100.0, applied only to recorded replay Tool deadlines after admission. Fast commands do not wait longer; only expiry is extended. Use a new run ID after changing it. |
| `submission.arrival_schedule` | `poisson` | `burst`, `fixed_stagger`, `poisson`, or `normal`; grouped assignment generates one arrival per case and repeats it for all rollouts |
| `submission.normal_arrival_std_seconds` | 60 | Positive standard deviation in seconds. Normal scheduling draws one seeded N(0, sigma) value per group/session, sorts the draws and subtracts the minimum so the first arrival is zero. No clipping or rescaling; the resulting last arrival is not a fixed window. Seed also includes repetition and ordered case IDs; policies share arrivals. New run ID required; no host apply, rebuild or retraining. |
| `submission.stagger_interval_seconds` | 0 | Positive for fixed staggering; zero for burst/Poisson/normal |
| `submission.poisson_arrival_window_fraction` | 2.0 | Expected window divided by median scaled trace duration |
| `submission.poisson_arrival_window_seconds` | `null` | Positive explicit expected window; overrides the fraction |
| `submission.session_arrival_offsets_seconds` | `null` | Explicit nondecreasing finite offsets, in seconds from arm start; one per offered session, with exactly one concurrency level. Overrides generated arrival times. Grouped rollouts of the same case must have identical offsets. Preserve each LS case's offset when removing BE for a no-BE reference. Changing offsets requires a new run ID; no host apply, image rebuild, or retraining. |
| `control_timing_details` | `false` | Enable nested control spans, GC tracing, raw lifecycle dumps, duplicate payloads, and Runtime working-KB exports |
| `runtime_trace_content_retention` | `failures` | `failures` keeps structured trace identities and measurements, failed/timeout Tool output, and content needed for replay completeness, but omits raw hook payloads, LLM messages/model bodies, and successful Tool output. `all` restores those bodies with the 64 MiB full-audit limits. Use a new run ID; no host apply, image rebuild, or retraining is required unless the packaged ClawTune source itself changed. |
| `runtime_log_retention` | `failures` | On successful sessions, keep only whitelisted `host_kb_client_prediction` records in `sidecar-client-timing.jsonl`; omit full Agent stderr, sidecar, policy-SSH, gateway, and model-relay logs. Agent failure, replay invalidity, or any failed Tool call retains full Runtime logs. `all` retains them for every session. |
| `retain_standalone_pmu_artifacts` | `false` | Do not write duplicate `pmu-profile-*.json` files. PMU profiles remain embedded in required cgroup-resource evidence. Set `true` only for compatibility or a directed PMU audit. |
| `retain_tool_resource_timelines` | `false` | Omit per-poll Tool resource timelines after online observation/label construction. CPU and memory peaks, baseline/delta, PMU, sample count/quality, coverage, and execution identity remain. Set `true` only when raw points must be audited or labels must later be reconstructed from archived artifacts. |
| `prediction_record_detail` | `compact` | `compact` retains prediction point values, source/fallback, reservation, actual/error/coverage, telemetry validity, identities, and hashes of the omitted full model structures. `all` retains repeated backend models and Runtime metadata. |
| `memory_log_interval_seconds` | 10 | Interval for writing host resource/queue trend snapshots to the event stream. It does not change the 0.2-second internal sampler, peak calculation, admission/watermark control, or serving-simulator CPU feedback. |
| `model_request_content_retention` | `failures` | `failures` keeps compact successful Model Gateway records and exact request/response content only for incomplete, errored, non-200, or undelivered requests. `all` retains exact content for every request for a full-audit run. The setting changes result retention only; use a new run ID. It requires no host apply, image rebuild, or retraining. |
| `snapshot_page_profile` | `null` | Linux-only, root-only page-idle profiling for explicitly listed `case_ids`; keep disabled in throughput runs. `roles` selects Runtime and/or Tool, `interval_seconds` is the sampling-window width, and `max_windows_per_generation` is at most 65535. The metric is windows in which a present 4 KiB GPA page was referenced, not memory-instruction count. Exact per-page counts are compressed under `page-access/`; aggregate window records remain small. Requires `CONFIG_IDLE_PAGE_TRACKING=y`, readable `/proc/<pid>/pagemap`, incremental-COW lazy restore, and a new run ID; no image rebuild or retraining. |
| `snapshot_page_profile.force_model_wait_checkpoints` | `false` | Profiling-only control that checkpoints the selected single-session case at real simulator-backed model waits through the ordinary pair lifecycle. `checkpoint_tiers` supplies the finite per-generation `warm`/`cold` sequence; after it is exhausted, only normal policy checkpoints remain. This changes snapshot timing and is not throughput evidence. |
| `snapshot_checkpoint_gate` | `null` | Evaluation-only matched-mechanism gate. `case_ids` selects workload cases and `checkpoint_tiers` supplies one finite WARM/COLD destination per generation. At each selected case's real model-wait boundary, both Runtime and Tool are checkpointed through the ordinary pair lifecycle. Use the identical value in control and preparation arms so checkpoint/restore populations match; this intentionally changes workload timing and proves only the isolated source of benefit, not production-policy throughput. Requires `serving_sim`, `agent_and_tool` snapshot pause, incremental-CoW, and a new run ID. |
| `snapshot_page_management` | `null` | Optional COLD snapshot preparation. `mode: gpu_event` preserves event-only submission using `trigger_event` (`queued`, `prefill_start`, default `prefill_start`). `mode: state_driven` latches accepted role boundaries and rechecks after checkpoint commit; each role/request/generation submits at most once. Requires serving_sim, snapshot pause and tiered/COLD incremental-COW storage. Wait/GPU reclaim, either scope, and compressed/uncompressed storage are supported. A new run ID is required; no image rebuild or host apply. |
| `snapshot_page_management.runtime_trigger_event`, `.tool_trigger_event` | `prefill_start`, `first_token` | Used only in `state_driven` mode. Both accept `queued`, `prefill_start`, `first_token`; Tool also accepts `disabled`. These are actual phase boundaries, not response ETAs. A boundary remains latched while its model wait is active. Model completion, selected full-VM prewarm and session cleanup cancel preparation; Tool admission also cancels Tool preparation. |
| `snapshot_page_management.candidate_source` | `shared_lineage` | Ranks authoritative incremental-lineage ranges by shared waiters per byte. Overwritten ancestor pages are excluded. |
| `snapshot_page_management.expansion_budget_mib` | 1024 | Arm-wide allowance for newly prepared expanded files, using compressed-manifest `decompression_allocated_bytes`, held until demand/cancellation or model-wait end. Insufficient allowance skips preparation without waiting under the snapshot lock. This is separate from bounded prefetch reads and is not a hard Linux page-cache residency limit. Whole-layer decompression remains non-preemptible; no page-selective decompression is implemented. |
| `snapshot_page_management.prefetch_budget_mib` | required | Arm-wide concurrent preparation budget and upper bound for `max_inflight_mib`. `per_vm_prefetch_mib` defaults to 32 MiB; `max_inflight_mib` to 128 MiB; `io_workers` to 2; `candidate_chunk_kib` to 128 KiB; `batch_window_milliseconds` to 25 ms; `prefetch_bandwidth_mib_s` to 256 MiB/s; `max_ranges_per_vm` to 512; and `signal_max_age_seconds` to 5 seconds. The worker emits one aggregate record per VM/request with batch waiters, authoritative/stale-version bytes, selected/covered bytes, leader-only physical read bytes, expansion time, cancellation, and errors. |

Prefetch `physical_read_bytes` counts deduplicated application reads, including
page-cache hits; it does not measure SSD block-device traffic. Linux owns cache
replacement. Include cache charges in the same total memory budget across arms.

Creation retry uses backoff and releases lifecycle slots while waiting. Ambiguous
HTTP failures use ownership reconciliation rather than blindly creating another
VM. The API and SDK must support the corresponding lifecycle deadlines.

The KB options require the host-live Runtime/Tool image update once. Subsequent
value changes require a new run ID, with no host apply, image rebuild, or
retraining. Host-live requires OpenClaw and an explicit validated seed directory
in `evidence.prediction_artifact`. The host endpoint uses the existing configured
policy listener and pair credentials; no guest scope-selection option exists.
With `host-live`, default logging filters `host_kb_client_prediction` records
into `openclaw/SESSION/sidecar-client-timing.jsonl`; full `sidecar.log` is kept
only for failed sessions or `runtime_log_retention: all`. Durations use seconds
and include the client request/response path; see [results](results.md) for scope.
This launcher instrumentation needs a new run to collect data, with no host
apply, image rebuild, or retraining.

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
Replay serves recorded model responses and executes Tools normally. `serving_sim`
uses live simulator scheduling and token timing for the adapted responses;
Tools still execute in real VMs. Qualification accepts replay, serving_sim and
API inference. API qualification runs the selected complete live tasks, including
child Sessions, with the configured credentials and arm timeout. It does not
inject the single-step replay stop. Fields below are in
`inference.configuration`:

| Field | Default | Meaning |
| --- | --- | --- |
| `model`, `base_url` | Explicit configuration or saved environment | Provider model and endpoint |
| `api_key_env` | `OPENCLAW_API_KEY` | Name of the host environment variable holding the credential |
| `time_scale` | 1.0 | Nonnegative multiplier on replayed model delays |
| `qualification_case_limit` | 16 cases | Positive integer; maximum distinct real-VM smoke cases, evenly sampled in workload order including the endpoints. All source inputs are still validated and receipt-bound. Increase to the corpus size for full execution coverage. Does not change formal workload size. |
| `qualification_simulator_cluster_count` | 1 cluster | Positive integer; managed simulator clusters used by qualification, capped by formal `serving_sim_launch.cluster_count`. A single-cluster smoke does not prove cross-cluster routing or capacity. Formal startup checks every configured cluster before VM admission. |
| `serving_sim_catalog_manifest` | Required for `serving_sim` | Absolute path to the version-2 CPU `manifest.json` produced by `prepare-serving-sim`; set workload `tasks.input` to its `workload.json` and clear `tasks.cases`. CPU runtime does not read replay/catalog files. The simulator must serve the matching `catalog.json` and `traces/`. |
| `serving_sim_topology_digest` | Unset | Expected `topology_digest` returned by the ClawBox adapter's `/v1/clawbox/simulator/info`. Set this to reject a changed topology during matched comparisons; when unset, the actual digest is recorded without an equality requirement. |
| `serving_sim_diagnostic_interval_seconds` | 1.0 seconds | Positive finite interval for client process/FD/event-loop samples, and managed server samples. Request diagnostics retain stages and exception chains without extra prompt/response copies. |
| `serving_sim_diagnostic_detail` | `full` | `compact` omits verbose native signal payloads and repeated full pool snapshots/cost provenance. With GPU-aware reclaim, it retains phase changes, reclaim-threshold crossings, actions and at most one repeated queued summary per second across the coordinator; suppressed counts are retained in aggregate result metrics. Every signal still updates the live estimate and policy state. Request stages, errors and lifecycle events remain recorded. Diagnostics are buffered, flushed at process samples/errors and closed normally. Use a new run ID when changing this field. |
| `serving_sim_keepalive_expiry_seconds` | 2.0 seconds | Idle HTTP connection reuse lifetime; nonnegative and finite, 0 disables idle reuse. Keep below the server's idle timeout (Uvicorn default: 5 seconds) to avoid racing server-side close. Active model/signal streams are unaffected; failed model requests are not automatically resubmitted. |
| `serving_sim_launch` | Unset | Optional managed local simulator configuration below. Qualification and each formal arm launch a fresh service; success, failure and timeout all finalize it. Omit for a manually managed external service. |
| `runtime_startup_timeout_seconds` | 60 | Integer 1–3600; Runtime setup and sidecar readiness deadline |
| `runtime_completion_watch_seconds` | 10 | 1–60 seconds per completion notification wait; events wake it immediately |
| `openclaw_bin` | `openclaw` | Guest executable |
| `openclaw_exec_yield_ms` | Unset | Optional integer from 10 to 120000 milliseconds for background-capable OpenClaw exec calls. Synchronous replay recordings disable automatic background execution; see [workloads](workloads.md). |
| `openclaw_tool_home` | `/root` | Absolute home directory inside the Tool VM. OpenClaw shell commands use it to find the image's Git and Conda configuration. Change for a custom Tool user; takes effect with a new run ID, without host apply or retraining. |
| `clawtune_config`, `clawtune_root` | Unset | Reuse a ClawTune model configuration and source root |

Fields under `agent` for shared OpenClaw Sessions:

| Field | Default | Meaning |
| --- | --- | --- |
| `subagents` | `false` | Enable native Gateway execution and Session tools, including `sessions_spawn`, inside the existing Runtime. Requires `driver: openclaw`, `inference.backend: api`, `conditions.prediction_source: host-live` with a validated seed, and `mechanisms.idle_reclaim.scope: tool`. Runtime remains resident. |
| `subagent_max_concurrent` | `8` | Integer 1–128; maximum concurrent native child runs across this Runtime's Gateway. This is not a VM count or a CPU reservation. Native OpenClaw also limits each parent's active children to its default of 5. |
| `subagent_max_spawn_depth` | `1` | Integer 1–5; nesting depth of native subagent spawning. Depth 1 allows root children; deeper nesting requires increasing this field. |

These fields require a new run ID and qualification. First install matching
ClawBox host code and refresh the Runtime predictor/plugin; no Tool image rebuild,
KB retraining or host apply is required for these fields. Replay/serving_sim child
trajectories are not synthesized. Concurrent model waits are tracked by request
ID. Host prediction receipts distinguish gateway, runtime, logical Session, run
and tool call; sharing a Tool VM does not merge these invocations. Learning still
uses the pair's shared KB namespace. Equal live API prompts remain separate
requests; only an explicit shared request ID identifies a retry. Native artifact
validation checks physical Runtime ownership and includes all child recordings.

ClawTune Session publications expose `active_child_session_keys` and a nullable
`wait` relation (`subagent_completion`, `any` or `unknown`). ClawBox carries
`logical_session_id`, `session_key`, `agent_id`, `run_id` and `tool_call_id`
through the Tool execution envelope, host admission ledger and worker events.
These are scheduling inputs only; this release does not assign different CPU
priorities. serving_sim remains keyed by the physical pair and request ID because
the model HTTP path has no authoritative logical Session correlation field.

The awaited agent-start hook registers each logical Session with the host. A
pair observed to contain multiple Sessions is protected from Runtime checkpointing;
Tool reclaim retains its activity/admission fences. Child next-LLM forecasts do
not predict parent completion; absent or unknown parent waits stay unknown.
Native child cleanup and stable idle state of every observed Session gate final collection.
Five seconds of stable idle plus polling/transport are included in completion
latency. The first CLI reply is saved as `initial_root_response`, with drain
evidence under `shared_sessions`; subsequent replies are in Runtime traces.

Qualification limit changes require a matching receipt and a new run ID when
editing a frozen specification; no host apply, image rebuild or retraining.
Required native snapshot/NUMA probes and input validation remain enabled.
Use `python3 scripts/configure-simulator-ports.py --base-port PORT --cluster-count N`
on the Linux host to preview port reservations (`N` defaults to 1).
Add `sudo` and `--apply` to merge live reservations, verify readback and persist
them for reboot. This host-only setting needs no guest rebuild or retraining;
repeat it when changing the simulator listener range.

`inference.configuration.serving_sim_launch` fields:

| Field | Default / requirement | Meaning |
| --- | --- | --- |
| `sim_root`, `venv`, `trace_catalog` | Required absolute Linux paths | Verified unmodified GPU source, existing simulator environment, and GPU-side catalog. CPU scheduling does not parse the catalog. |
| `cpu_affinity`, `cores` | Required | Distinct real logical CPU IDs and their count; this is the total real CPU budget across all clusters. IDs must be outside configured VM compute CPU sets and within supervisor affinity. With multiple clusters, each process gets one CPU and assignments wrap round-robin. |
| `topology` | Required | Native simulated GPU topology for each independent cluster, e.g. `8A`; separate from real CPU budget. |
| `decode_time_scale` | `null` (calibrated timing) | Positive finite multiplier for the decode part of each native step. Excludes `tpot_ms`; requires the upstream hybrid step model. Non-physical; prefill and block copy/zero work remain unchanged. Manual `serve`: `--decode-time-scale`. |
| `tpot_ms` | `null` (calibrated timing) | Positive finite target milliseconds per output token, replacing the decode part of each step. Excludes `decode_time_scale`; requires the upstream hybrid step model. Non-physical; queueing, prefill, maintenance and transport still affect observed timing. Manual `serve`: `--tpot-ms`. |
| `forecast_interval_seconds` | 0.5 seconds | Minimum interval between same-phase response forecasts; positive and smaller than validity. Manual `serve`: `--forecast-interval-seconds`. |
| `forecast_window_seconds` | 5.0 seconds | Positive rolling token-speed window and maximum age for reusing an engine rate in queue ETA. Manual `serve`: `--forecast-window-seconds`. |
| `forecast_validity_seconds` | 2.0 seconds | Positive forecast lifetime; expires independently of its predicted completion deadline. Manual `serve`: `--forecast-validity-seconds`. |
| `cpu_refill_mode` | `off`; `decode-first` opt-in | CPU-aware GPU scheduling switch. Off mode preserves the native scheduler path. Decode-first uses only vLLM's native prefill-throttle argument when low CPU and mixed decode/prefill work coincide. |
| `cpu_refill_feedback_interval_seconds` | `1.0` seconds | Minimum interval between latest-only aggregate CPU/Tool snapshots sent to every cluster. Delivery is nonblocking; background delivery errors fail the arm. |
| `cpu_refill_activation_floor_utilization` | `0.0` fraction | Lower bound of the activation band. Below it, native scheduling is retained so decode-first cannot starve cold-start prefills before a CPU/Tool pipeline exists. It must be below the enter threshold. |
| `cpu_refill_enter_utilization`, `cpu_refill_exit_utilization` | `0.90`, `0.95` fractions | Actual experiment-cgroup CPU hysteresis. Enter must be below exit. Ready and resource-blocked Tool counts are retained as evidence; they do not replace the measured CPU trigger. |
| `cpu_refill_enter_samples`, `cpu_refill_stale_seconds` | `2` samples, `3.0` seconds | Consecutive low samples needed to enter and maximum sample age. Staleness must exceed the feedback interval and restores native scheduling. |
| `cpu_refill_throttle_steps`, `cpu_refill_release_steps` | `3`, `1` scheduling steps | Per-engine bounded cadence while active with mixed candidates. Release steps use the ordinary native scheduler to guarantee prefill progress. Both values are positive integers. |
| `cluster_count` | `1` | Number of independent simulator clusters. Managed clusters use consecutive ports beginning at `base_url`; the entire range must fit below 65536 and be unused. Use multiple `8A` clusters to add capacity, not one enlarged topology. |
| `routing` | `sticky_balanced_by_case` | The only supported pool route. Sorted cases assign each unique trace round-robin once; all sessions and Tool audits for that trace remain on the same cluster. |
| `persistence` | `final` | `native` or `final`; current sim-release uses the same native incremental-input flushing and shutdown-only final summaries for both. ClawBox verifies successful native close. |
| `nofile` | 65536 descriptors | Minimum process soft limit. A lower hard limit fails with a remedy; configure the host/service limit before retrying. |
| `startup_timeout_seconds` | 180 seconds | Positive finite readiness deadline; startup failure is recorded and owned resources are closed. |
| `shutdown_timeout_seconds` | 720 seconds | Positive finite graceful-close deadline. Forced termination is a finalization failure. |
| `tokenizer_dir` | Unset | Optional absolute path to the existing native tokenizer assets; otherwise native lookup applies. |
| `cache_dir` | Unset | Optional absolute path to the existing cache used by simulator setup. Sets `SERVING_SIM_CACHE` in the child, including processor metadata lookup; otherwise inherited `SERVING_SIM_CACHE` or the launcher's default `venv.parent/cache` applies. |
| `hf_hub_cache` | Unset | Optional absolute Hugging Face hub cache directory; sets child `HF_HUB_CACHE`. Needed to reuse pinned processor metadata from another account/cache. Independent of `tokenizer_dir`; otherwise inherited Hugging Face cache settings apply. |

Managed launch requires explicit `base_url`, `model` and optional `api_key_env`,
using local HTTP on `127.0.0.1` or `localhost`; `clawtune_config` cannot override
that endpoint. With `cluster_count > 1`, `base_url` names cluster 0 and the
remaining clusters use consecutive ports. Existing listeners/output directories
are never adopted. The
launcher also exposes `--nofile` (65536) and
`--diagnostic-interval-seconds` (1.0 seconds) for manual services. A finalized
report is distinct from timing validity: inspect native `validity.json` and
`correctness.gpu_validity` before making performance claims.
These reports retain native `non_physical` settings and per-cluster lag; no
fixed-threshold warning verdict is synthesized. `/v1/clawbox/simulator/info`
also reports `decode_time`, `non_physical`, and the effective `cpu_refill`
configuration/counters; the first two are empty when speed overrides are
absent. Managed startup verifies the requested decode and policy settings. Speed
overrides do not change `k=1`, recorded output token counts or arrivals.

Changes require a new run ID and matching qualification, but no guest image
rebuild or prediction retraining. Host apply is needed only if the saved host
CPU/resource configuration or service hard limits must change. Reuse the
installed simulator environment; setup/rebuild is required only when its actual
dependencies or native assets change. A partially consumed GPU run cannot resume;
abort/clean it up and preserve its artifacts before starting a new run.

Put credential values in the environment or selected credential configuration,
not experiment YAML. A selected `clawtune_config` supplies the endpoint, model,
and credential in preference to direct provider fields. Model-wait prediction/oracle settings are unsupported;
checkpoint decisions use actual outstanding model requests.

For `serving_sim`, `base_url` points to one fresh simulator service or the first
service in a managed pool. Every cluster uses virtual clock speed `k=1`.
`time_scale` must be absent or 1; recorded delays are not added to simulator
latency. Use a dedicated `api_key_env` such as `CLAWBOX_SIM_API_KEY`;
it may be unset when the simulator has no authentication. A provider-specific
`clawtune_config` must be removed to use the explicit simulator endpoint.
Launch it with `python3 -m clawbox.integrations.serving_sim` from the ClawBox checkout,
or the installed `clawbox-serving-sim` command. This adapter imports the unmodified
GPU application. Adapter protocol 3 adds expiring current-response forecasts and retains process-handle mappings
to the existing chat stream without another per-request round trip. Its management
paths are `/v1/clawbox/simulator/info` and `/request-signals/clawbox/events`.
`/v1/clawbox/simulator/tool-outcomes` audits recorded versus actual Tool failures
only at session finalization when needed. The GPU service retains the recordings;
CPU sessions retain only actual requests, replies and live process identities.
Native GPU scheduling is unchanged. The shared signal feed additionally contains
`response_forecast`; original native events keep their meanings.

The adapter targets sim-release `ab16142d9835052d5966f3c19679ce6d22789004`.
Its standard topology enables shared-prefix checkpoints; retain the revision
and topology digest when comparing runs. The updated Mooncake shim requires
setup/build in the new checkout. Original `history_tokens` values are preserved
in prepared GPU traces. TraceLab synthetic sources missing that field must be
regenerated upstream before preparation; ClawBox never invents missing history.
This does not add TraceLab support to the OpenClaw-only trace importer.
It explicitly uses `prompt_source=actual`: prefill and KV state depend on the
Agent's actual messages. Trace-prompt replay, accelerated warm-up and
`serve-many` are not exposed by this integration. Retain `k=1` and fresh
independent cluster state per arm. Managed pools start all clusters before
waiting for readiness, consume every cluster's signals, and require every
cluster to finalize. Do not interpret upstream trace-prompt capacity measurements
as capacity measurements of this actual-prompt workflow.

`prepare-serving-sim` requires the original format-5 source, its adapted CPU trace,
and the configured prediction artifacts offline. Its new output directory contains
the GPU catalog/traces, CPU workload/manifest, and separate provenance. Existing
version-1 bundles must be prepared again. Changed task inputs or prediction
artifacts require preparation and qualification with a new run ID. These input
changes do not require host apply, image rebuilding, or retraining by themselves.

| Launcher option | Default / requirement |
| --- | --- |
| Command | `setup`, `check`, `smoke`, or `serve`; setup installs dependencies/builds native libraries, check only verifies them |
| `--sim-root` | Required absolute path to the original GPU checkout; do not install ClawBox patches there |
| `--venv` | `SIM_VENV`, otherwise `~/.cache/clawbox/serving-sim/venv`; use a dedicated Python environment |
| `--cores` | 4 real logical CPUs selected from allowed affinity; prefix with `taskset -c CPU_LIST` on a shared host |
| `--topology` | `8A`; simulated GPU topology, independent of real CPU count |
| `--trace-catalog` | Required by serve; use the prepared bundle's catalog |
| `--out` | Required new directory for serve/smoke; preserve completed outputs |
| `--persistence` | `native` (default) or `final`: both preserve the current simulator's incremental input writes, periodic flush/step release and final summaries at graceful shutdown. Stop with Ctrl-C/SIGTERM and verify `clawbox-persistence.json` has both `finalized: true` and `native_close_succeeded: true`. Abrupt termination leaves incomplete reports. |
| `--host`, `--port` | `127.0.0.1`, `8000` |
| `--k`, `--v0` | Virtual clock speed 1 and initial virtual seconds 0; real CPU runs require k=1 |
| `--api-key-env` | `CLAWBOX_SIM_API_KEY`; unset means no authentication |
| `--tokenizer-dir` | Unset; use the simulator's validated pinned metadata cache |
| `--ssl-keyfile`, `--ssl-certfile` | Unset; optional TLS file paths |

Prerequisites are Linux x86-64 or ARM64 (ARM64 glibc >= 2.38), bootstrap Python 3.12+,
uv, git, Cargo/Rust with edition-2024 support, a C/C++ compiler and working Docker.
ClawBox supplies the ARM hash lock with the upstream package versions; x86-64 uses
the upstream lock. Setup reuses complete pinned metadata (`HF_HOME` controls its cache).
`GLOO_SOCKET_IFNAME` defaults to `lo` only in this launcher. Reuse these explicit
environment/cache settings in fresh shells; existing verified environments need only check.
The adapter does not rebuild VM images or retrain ClawTune. Native dependency changes
require setup; changing service settings requires a fresh service and new run ID/qualification.

When upgrading from the older simulator, run setup in a new simulator checkout
and dedicated environment: this release requires the simulator's own Rust
extension in addition to the Dynamo/Mooncake libraries. ARM setup uses matching
package versions with ARM wheel hashes. Preserve `realized.json`,
`realized-inputs.jsonl` and `realized-tokens.bin` together: the summary now refers
to incremental message/token data. The native
`serving_sim.joined.realized.read(output_path, records, messages=True)` reader
reconstructs full inputs offline; CPU scheduling does not consume these files.

Use a separate fresh service for qualification, then restart
it before a formal arm so KV state cannot leak across arms. Qualification uses
the first model call and real Tool execution per case. It does not establish
target-concurrency capacity or validate GPU hardware.
The separate snapshot probe checks the configured VM pair's checkpoint/restore
capability using a known idle pair; it does not establish GPU-triggered reclaim
or ETA prewarm coverage. Use the explicit evaluation checkpoint gate to exercise
restore paths in a bounded smoke; it is not evidence of pressure-policy effectiveness.
The topology digest covers expanded topology settings; it does not identify
native source code, tokenizer files or cost-model assets. Freeze those inputs
separately and retain the simulator's native configuration and validity outputs.

`mechanisms.idle_reclaim.policy: gpu` requires `inference.backend: serving_sim`
with either `scope: tool` or `agent_and_tool`. Its editable options are:

| Field under `mechanisms.idle_reclaim` | Default | Meaning |
| --- | --- | --- |
| `gpu_reclaim_min_remaining_seconds` | 1.0 seconds | Minimum fresh predicted response remaining time for pressure-driven checkpoint selection; nonnegative. |
| `gpu_signal_max_age_seconds` | 5.0 seconds | Maximum age of a usable GPU estimate; strictly positive. Missing, expired or unmodelled estimates cannot authorize GPU-aware reclaim. |

`mechanisms.restore.prewarm` is independent of reclaim policy and scope:
`off` (default) or `eta`. ETA mode requires snapshot reclaim and serving_sim.
`prewarm_lead_seconds` defaults to 2.0 positive seconds, an explicit budget for
restore plus margin, not a measured cost. When fresh remaining time is at most
this lead, restore Runtime first (including its model relay), then Tool and its
bridge using ordinary memory admission. Resident VMs are skipped. Pending work
revalidates ETA before starting; already committed restore is protected from
immediate re-eviction. Blocked Tool admission retries on later fresh forecasts.
Actual response readiness remains authoritative; commands keep normal admission.

The GPU-side adapter emits `response_forecast` for the current request, without
changing native scheduling, simulated clock, arrivals or generation timing:

| Field | Meaning |
| --- | --- |
| `remaining_s` | Seconds until estimated native complete-response readiness; null means unknown. |
| `valid_for_s` | Validity from observation time. CPU subtracts delivery age and also applies `gpu_signal_max_age_seconds`. |
| `phase` | `queued`, `prefill`, `decoding`, `blocked`, `finished`, or `unknown`. Only queued/decoding can carry an ETA. |
| `target` | `native_response_ready`: includes modeled client return delay; excludes HTTP and VM restoration latency. |
| `source`, `output_length_source` | `trace_length_online_rate`, `recorded_current_call`. Explicit current-output-length oracle; no future call, recorded LLM time or reasoning boundary. |
| `output_tokens`, `rate_s_per_token` | Observed progress and rolling seconds per token; absent/null when unavailable. |
| `unavailable_reason` | Why no ETA is available, e.g. preemption, KV waiting or stale observations. |

During decoding, remaining time is
`max(total_output_tokens - observed_output_tokens, 0) * observed_seconds_per_token + modeled_return_delay`.
A rate needs at least two output-progress observations. During queueing, a fresh
engine rate can be combined with native queue/prefill wait (discounted for queue
event age). Prefill admission clears that estimate; full prefill progress prediction
is not implemented. KV-transfer waiting, preemption/recomputation, missing rate
and observation expiry produce null estimates. `think_start`/`think_end` are
not CPU scheduling inputs. First-token Tool metadata from trace replay is not
treated as an early prediction of future Tool work. GPU reclaim selects the
longest valid response ETA; next-use uses the same estimate and its expiry.
Signal loss or epoch changes fail the arm without silently selecting another policy.

Changing the bundle, simulator settings or GPU policy requires a new run ID and
matching qualification and a fresh service running adapter protocol 3. These settings alone require no VM image rebuild, host
apply or ClawTune retraining. A changed Runtime/Tool environment follows the
existing image and qualification rules. Keep original traces and prior runs.

`measurement.memory_sample_interval_seconds` defaults to 0.2 and
`execution.memory_log_interval_seconds` defaults to 10,
`stabilization_seconds` to 1, and `observe_predictions` to `false`.
The sampler targets this start-to-start interval and skips missed ticks instead
of issuing catch-up bursts. Core memory readings remain on the sampler thread;
rich queue, filesystem, CPU and policy reporting runs in a bounded latest-only
worker so it cannot delay the next reading. Only one trend record per log
interval is persisted; peak accounting and serving-simulator feedback continue
at the sampler interval. C2 maintenance runs in its own
serial loop at the same interval; B uses its independent
`tool_call_cpu_poll_interval_seconds` (default 0.25 seconds). Resource records include observation times,
report coalescing and cached-demand age; see [result interpretation](results.md).
Changing the interval or host sampling implementation requires a new run ID,
without host apply, image rebuild or retraining.
`measurement.validation.command` optionally checks the final task result.
For `host-live`, the KB manifest records `prediction_memory_policy`.
`observed_guest_after_seed_proxy_v1` uses the seed measurement until a pair has
an eligible, uncensored guest-memory observation, then queries
`guest_memtotal_minus_memavailable` for that pair. Admission metadata retains
the queried measurement and the host-growth estimate's proxy semantics. Use a
new run ID when updating this host policy; no image rebuild or retraining is needed.
The Runtime SSH shim exposes standard OpenClaw payload clauses to the collector
by using shell builtins for launcher environment setup. Update the host checkout
and start a new run to use this correction; existing guest templates remain valid.
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

Runtime images include the recorded-inference context guards. In `replay` and
`serving_sim`, they disable both automatic compaction and the local prompt/tool
context estimates that would inject an unrecorded summary call. Live inference
retains OpenClaw's normal context handling. Updating these guards requires
`images --role runtime`, adopting its new template, a new run ID and qualification;
it does not require Tool images or predictor retraining.

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

### Creation kernel capture

`sudo python3 scripts/capture-creation-kernel.py` records host kernel stages and
new Cube log bytes alongside the supported experiment runner. See the
[creation diagnostic walkthrough](guide.md#diagnose-vm-creation-under-load).

| Option | Default | Meaning |
| --- | --- | --- |
| `--output` | required | New evidence directory; refuses existing directories |
| `--seconds` | 3600 | Positive maximum capture duration in seconds |
| `--trace-root` | `/sys/kernel/tracing` | Mounted tracefs with kprobe events; requires root |
| `--log-root` | `/data/log` | Parent of CubeAPI, CubeMaster, Cubelet, CubeShim and CubeVmm log directories |

`ready.json` records the trace clock anchor and initial log offsets;
`kernel-trace.txt` contains entry/return events; `thread-identities.jsonl`
records observed thread metadata. `coverage.json` and `finished.json` report
loss counters and cleanup. This diagnostic does not alter workload configuration
or images and requires no host apply. Each accompanying experiment uses a new
run ID. Syntax/help validation alone does not establish successful Linux capture.

### CubeAPI open-file limit

The standard large-concurrency preparation uses a minimum of 65536 for the
CubeAPI service itself; changing the experiment shell's `ulimit` does not
configure this service. See the [creation preparation requirements](guide.md#1-check-the-host).

`bash scripts/configure-cube-api-nofile.sh` previews the limit for the installed
`cube-sandbox-cube-api.service`. See the
[host setup steps](guide.md#set-cubeapis-open-file-limit).

| Option | Default | Meaning |
| --- | --- | --- |
| `--minimum N` | 65536 | Minimum open descriptors per process; positive integer no greater than `fs.nr_open` |
| `--apply` | off | Requires root; persist the limit, reload systemd, and raise the current process limit |
| `--help` | — | Show usage without changing the host |

Apply writes `/etc/systemd/system/cube-sandbox-cube-api.service.d/90-clawbox-nofile.conf`
and preserves higher configured or live limits. It uses `prlimit` from
`util-linux`, touches only CubeAPI, and does not start or restart services.
The setting survives reboot; images, trained models, and experiment YAML need
no changes. A failed experiment must retain its run ID; reruns use a new one.
