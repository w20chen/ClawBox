# Tiered oracle experiment review and implementation plan

Status: planning only, 2026-09-07. No implementation changes, validation runs, or formal experiments were performed for this review.

The attached brief clearly specifies the two policies, preserved architecture, direct checkpoint requirement, validation sequence, experiment families, and result package. The additions below resolve experimental and lifecycle ambiguities without adding another baseline family.

## Confirmed scope

- Retain CubeSandbox, the managed OpenClaw Runtime/Tool VM pair, native SSH execution, endpoint refresh, workspace ownership, response holding, and frozen ClawTune command-specific P90 admission.
- Add exactly `tool-p90-tiered-lru-oracle-reactive` (A) and `tool-p90-tiered-time-oracle-reactive` (B).
- Use LOCAL on NUMA0, a bounded WARM snapshot pool on NUMA1, and persistent COLD snapshots on SSD. Implement direct LOCAL-to-WARM checkpointing and direct restoration from the selected tier.
- Canonical experiment: 60 submitted agents, Runtime 2 GiB, Tool 4 GiB, 360 GiB offered VM capacity, LOCAL 64 GiB, WARM 64 GiB, replay time scale 1.0.
- User selected the existing `rec-a` SWE-ReBench recording described below. Preserve its recorded responses, action order, and timing. Describe this as a systems case study on that trace; repetitions do not establish generality across repositories.
- All implementation validation and formal experiments run through `ssh kunpeng`.
- User-confirmed revision to RQ2: **How does pressure-triggered placement compare with placement at model-wait start when both policies have perfect knowledge of the current model wait?** No non-oracle tiered control is added.
- Describe these as oracle-informed heuristics. Fixed thresholds, LRU, finite storage, and reactive restore do not establish an optimal-performance upper bound.

## Findings from this review

| Evidence inspected | Finding and consequence |
| --- | --- |
| Local checkout | HEAD `6d5baddd37efd034034b6b0533ce509485869b0e`; the working tree was clean before this planning file was added. Freeze a versioned deployment before implementation validation. |
| `clawbox/experiments/baselines.py` | Eleven canonical baseline recipes exist, plus compatibility aliases. Preserve the canonical definitions; do not count aliases as additional experiments. |
| `clawbox/experiments/memory.py`, `worker.py` | Admission currently receives whole-host growth relative to the arm's starting memory sample. This is not an enforced NUMA0-only cap. WARM consumption must not be charged again as LOCAL. |
| `clawbox/cube/client.py`, `lifecycle.py` | The ClawBox pause wrapper currently exposes no per-operation tier destination. Direct WARM checkpointing needs investigation at the CubeSandbox storage boundary, not only a new policy name. |
| Managed model-wait path in `worker.py` | Safe Tool-then-Runtime checkpointing, response holding, Runtime restore, and lazy Tool restore already exist. The new path must also work when Tool is already nonresident but Runtime is LOCAL. |
| Checked-in c60 YAML | `examples/traces/openclaw-cube-replay.jsonl` is a simple file-creation smoke workload. It is not the formal SWE-ReBench workload requested here. |
| Read-only `ssh kunpeng` probe | Kernel `6.6.0-72.0.0.76.oe2403sp1.aarch64`; four NUMA nodes. Nodes 0 and 1 each reported about 503 GiB total and more than 470 GiB free. The proposed NUMA capacities are plausible, subject to exclusive experiment allocations and a fresh preflight. |
| Storage probe | `/data` currently shares an XFS root filesystem on NVMe-backed LVM; about 783 GiB was available. Full-study storage sufficiency is unverified and must be estimated from real allocated snapshot sizes. |
| Remote source inventory | `/home/weitianc/ClawBox` is at `d42da58919411a4ad6830d80a5c6a7759f69cce0` with local changes. `/home/weitianc/ClawBox-cube` has application files but is not a Git checkout. Neither observation identifies the active deployed build conclusively. |
| Related remote checkouts | ClawTune HEAD is `76eab6fa5c6333f4e80901c030f10cab0e4ce605`. CubeSandbox HEAD is `64102d91725402090b18fdc14845e7782775d034` with local changes, including the pause implementation. These are checkout observations, not yet final binary provenance. |

## Selected replay trace (user confirmed)

The workload is `15five/scim2-filter-parser`, SWE-ReBench instance `15five__scim2-filter-parser-13`, base commit `08c32462831d3849a70241ac9fea946b6b1884a6`. The task requests returning a NamedTuple instead of a tuple in `src/scim2_filter_parser/transpilers/sql.py`.

Use this existing replay artifact on kunpeng:

```text
/home/weitianc/ClawBox/results/paper_replay_20260901_128g_v4/selected-traces/rec-a-enriched.jsonl
SHA-256: 12541145678c9f65c5b1388410f82a2d9954a26b100a3a781db5fa837eb83ae5
```

Its original model recording is:

```text
/data/recording-output/model-trace-session-0000.jsonl
SHA-256: 8be4a7a6affe1f316e585cf7bdef170cd6e48ba43bf844a923045a1793d28b47
```

A read-only comparison on 2026-09-07 verified that all 27 action IDs, start/end timestamps, recorded model latencies, and raw model responses match between the original and enriched files. The enriched artifact also contains raw request metadata for all 27 model calls; audit that metadata's provenance and compatibility before relying on strict request matching.

| Recorded property | Value |
| --- | ---: |
| Model | `deepseek-v4-flash` |
| Model waits | 27 |
| Wait mean / p50 / p90 | 5.060 / 3.692 / 9.000 seconds |
| Maximum wait | 21.487 seconds |
| Waits < 2 seconds | 3 (11.1%) |
| Waits 2 to < 20 seconds | 23 (85.2%) |
| Waits >= 20 seconds | 1 (3.7%) |
| Tool calls issued in recorded model responses | 31: 28 `exec`, 1 `read`, 1 `edit`, 1 `apply_patch` |

These Tool counts describe issued calls, not verified completed native executions. Complete Tool-duration, memory-demand, and execution-integrity characterization from the associated resource/bridge evidence under `/data/recording-evidence/session-0000/` before formal use. All three wait bins are represented, but the long-wait bin is sparse; report that limitation.

Trace existence is confirmed; successful replay with the final managed CubeSandbox build is not yet confirmed. The original recording summary reports a validation-command exit of 129. Saved `rec-a-c1-current*` attempts under `/home/weitianc/clawbox-results-current/` also report failures, including canonical request mismatch and invalid/missing Tool telemetry. Preserve those outcomes and diagnose the causes; do not assume that an old recording or enriched file proves current correctness.

The diagnostic copy `/home/weitianc/clawbox-results-current/frozen-traces/rec-a-current-openclaw.jsonl` has SHA-256 `8ea22236666382a270624c5216b66df6da415c546f65486ce92e45db77c11bf4` and matching recorded responses/timing, but different request metadata. Do not silently substitute it for the selected artifact. Restore compatible runtime/template/workspace behavior and inspect existing request canonicalization without weakening replay checks or altering the recorded workload to force a pass. Escalate a concrete incompatibility if it cannot be resolved within those constraints.

## Definitions to freeze before formal experiments

### Memory accounting and placement

Treat LOCAL and WARM as separate budgets. Record configured capacity, admission reservations, and measured physical consumption separately. Define whether each memory quantity includes VM overhead, snapshot buffers, and operation headroom; include LOCAL checkpoint/restore headroom within the stated LOCAL capacity. Apply the same definition to every new formal arm, including old policy recipes, and rerun them under that definition.

The proposed target is physically constrained experiment VM memory on NUMA0, with separate NUMA1 storage allocation. Keep the existing P90 reservation algorithm and whole-host emergency guard, but provide an experiment-scoped LOCAL measurement for the LOCAL budget. Verify whether the installed CubeSandbox process/cgroup layout can enforce this without mixing the NUMA1 snapshot writer into the LOCAL limit. Do not claim a hard physical cap from the existing admission number alone. If enforcement is unavailable, resolve that limitation before formal runs and label any admission-budget-only results accurately.

Use tmpfs with an explicit byte limit, `mpol=bind:1`, and `noswap`, subject to kernel and writer-cpuset validation. A NUMA mount option alone is insufficient evidence: Linux documents that the writer's allowed memory nodes can alter the effective policy. Verify actual file-page placement and fail validation on unintended fallback. See [Linux 6.6 tmpfs documentation](https://www.kernel.org/doc/html/v6.6/filesystems/tmpfs.html).

Control and measure the COLD snapshot page cache as well. Otherwise SSD snapshots can consume unaccounted DRAM or restore from cache, weakening both tier and equal-capacity comparisons. Freeze a common cache protocol, preferably scoped to experiment snapshot files, and distinguish snapshot-file reads/writes from physical device I/O. Account for transient buffers and retained backing files in total DRAM usage; avoid double-counting mapped shared pages.

### Snapshot scope and capacity

Proposed interpretation: tier the VM memory payload and required resume metadata; retain CubeSandbox's existing immutable-image and workspace/disk semantics. Enumerate all restore dependencies during the storage investigation. A WARM memory-snapshot hit does not imply zero SSD I/O for rootfs or workspace data. If the backend requires moving a larger snapshot package, include its real allocated bytes in WARM reservations and document that scope before freezing the study.

Use a per-sandbox snapshot generation and authoritative manifest. Keep lifecycle status separate from stable tier: LOCAL/WARM/COLD does not replace CHECKPOINTING/SPILLING/RESTORING or describe every retained backing artifact.

- Define `warm_reserved_bytes` as outstanding capacity reserved for writes and `warm_committed_bytes` as allocated pool bytes already written, including incomplete writes and retained dependencies until released. Never count the same bytes in both values.
- Before starting concurrent checkpoints, reserve a backend-supported conservative upper bound. Transfer reservation to committed usage as bytes are allocated; reconcile unused reservation on completion or failure.
- Enforce `warm_reserved_bytes + warm_committed_bytes <= warm_capacity_bytes` atomically. Track logical size, allocated size, and transferred bytes separately, with precise definitions for `actual_snapshot_bytes`.
- Select WARM-LRU victims per Runtime/Tool snapshot, using a documented timestamp and deterministic tie-breaker. Pin active restore/demotion dependencies against eviction.
- A snapshot larger than the whole WARM pool cannot be solved by spilling other entries. The default is an explicit unsupported-capacity/configuration outcome; never silently route a WARM decision to SSD. WARM-disabled control arms are an explicit exception defined below.
- Persist a COLD generation to a temporary destination, verify completeness and dependencies, complete the defined persistence barrier, atomically publish the authoritative manifest, then release the WARM source. Cross-filesystem copying itself is not atomic.
- Serialize restore and demotion of the same sandbox. If restore maps or lazily reads snapshot backing pages, retain and charge that backing until the backend no longer needs it. Verify that restored running memory satisfies LOCAL placement before claiming a completed WARM-to-LOCAL transition.

### Exact policy behavior

| Decision | A: pressure-triggered oracle LRU | B: wait-start oracle placement |
| --- | --- | --- |
| At model-wait start | Save current wait identity and oracle duration/deadline; no automatic checkpoint. | Decide immediately from full duration D. D < 2 s stays LOCAL; 2 <= D < 20 s checkpoints to WARM; D >= 20 s checkpoints directly to COLD. |
| When LOCAL reclaim is needed | Filter safely reclaimable agents in a current model wait. Prefer remaining wait >= 2 s, then select oldest using existing agent LRU. Place using remaining time at action time. | Do not silently apply A's pressure policy. Preserve the chosen placement rule; queue admission until capacity is available, with explicit timeout/failure behavior. Any emergency override must be separately defined and reported before use. |
| Short-wait pressure fallback | If all eligible >= 2 s victims are exhausted, allow eligible < 2 s victims and prefer WARM. If no safe victim exists, wait; safe admission is not a guarantee of bounded progress. | Short waits remain LOCAL under the pure B policy. |
| Checkpoint order | For a selected agent, checkpoint eligible LOCAL Tool first, then eligible LOCAL Runtime. Already nonresident roles are skipped. | Same safe role order. |
| WARM full | Spill eligible WARM-LRU snapshots to COLD until reservation fits; wait if candidates are temporarily pinned. | Same storage rule. |
| Model response becomes ready | Restore Runtime reactively and release the response only after readiness checks. | Same. |
| Next Tool request | Restore Tool reactively, refresh endpoint and verify identity, then execute through the existing P90/SSH path. | Same. |

Use oracle time only for the current replay wait. Store a request/wait ID and monotonic start/deadline; compute remaining time as `max(0, deadline - now)`. Record wall-clock timestamps separately for correlation. The deadline represents the recorded model wait, excluding response hold, admission delay, and checkpoint/restore overhead. Reject these oracle policies with live inference.

For B, the placement decision uses D at wait start even if actual I/O queues later; log decision-to-service delay. For A, recheck current wait eligibility and remaining time before committing a delayed action. Do not start a queued checkpoint after that response is ready. If a response becomes ready during an already committed checkpoint, complete the safe transition and restore Runtime before delivery. Neither policy automatically demotes WARM entries merely because a threshold is crossed.

### Measurement and validity

- Define c60 as 60 submitted replay sessions at the frozen arrival pattern. Report actual concurrent resident/running agents; admission serialization is a result.
- Measure JCT from scheduled arrival to correctness-validated completion, including admission and creation delays. Define the throughput window from first scheduled arrival to the last completion; separately report setup and teardown time.
- Define a correct agent as a replay-consistent session passing the selected case validation and execution-integrity checks. This does not imply newly solving a SWE-ReBench task beyond the recorded solution.
- Define a step as the existing replay/agent step unit and freeze it. Do not interchange Tool commands, model calls, and steps.
- Warm hit rate: successful WARM memory-snapshot restores divided by successful WARM plus COLD memory-snapshot restores. Report Runtime, Tool, and combined values.
- Cold spill rate: distinct snapshot generations successfully spilled WARM-to-COLD divided by distinct generations successfully admitted to WARM. Separately report direct-to-COLD rate and fraction of generations reaching COLD. Use N/A for zero denominators.
- Calculate residency from physical-byte occupancy integrated over time. Keep logical snapshot residency separate. Use time-weighted means and report sampling interval and coverage.
- Report transferred bytes/service time as operation bandwidth and total bytes/total service time as aggregate service bandwidth. Report end-to-end transition latency separately, including queueing, spill dependencies, and readiness checks.
- Concurrent before/after node deltas are contextual observations, not isolated measurements of bytes reclaimed by one operation.
- Preserve infrastructure-invalid attempts and reasons; fix and rerun affected arms. Policy-induced OOM, admission timeout, or inability to complete c60 remains a reported policy outcome. Do not discard those outcomes and retain only successful repetitions.
- Report each repetition, means, and variability. Compute per-run JCT percentiles before aggregating across runs. Do not treat 60 sessions from one run as 60 independent experiment repetitions. Report completion counts and timeouts alongside any successful-session latency statistics.
- Replace “maximum supported concurrency” claims with “completion/correctness at c60” unless an additional concurrency search is actually performed.
- Describe NUMA1 as an emulated pooled-memory snapshot tier. The experiment does not measure real CXL/UB hardware latency, bandwidth, or sharing behavior.

## Experiment matrix

Freeze the manifest before examining policy performance. The following target gives every required formal configuration three repetitions and reuses the primary 64/64 results in later tables.

| Family | Configurations | Distinct arms | Repetitions | Planned runs |
| --- | --- | ---: | ---: | ---: |
| Main c60 | Eleven canonical existing policies at LOCAL 64 GiB, WARM disabled; A and B at LOCAL 64 GiB, WARM 64 GiB | 13 | 3 | 39 |
| Additional WARM sensitivity | A and B at LOCAL 64 GiB, WARM 32 and 128 GiB | 4 | 3 | 12 |
| Capacity-matched controls | A and B at LOCAL 64 and 128 GiB, WARM disabled | 4 | 3 | 12 |
| Required target total | Reuse A/B 64/64 from the main comparison | 21 | 3 | 63 |
| Optional LOCAL sensitivity | A and B at LOCAL 48 and 96 GiB, WARM 64 GiB | 4 | 3 | 12 additional |

The capacity-matched controls are configurations of the same two policies, not additional baseline families. With WARM explicitly disabled, a checkpoint decision that would target WARM writes directly to COLD; the oracle information, trigger, eligibility, P90 admission, and reactive restore stay fixed. For each A/B policy, compare 64/0, 64/64, and 128/0. These controls also strengthen RQ1 by avoiding admission or threshold changes as confounders.

Retain the eleven current canonical names:

1. `lifetime-full-resident`
2. `tool-full-resident`
3. `tool-static-resident`
4. `tool-p90-resident`
5. `tool-oracle-resident`
6. `tool-static-eager-reactive`
7. `tool-p90-eager-reactive`
8. `tool-p90-fixed-reactive`
9. `tool-p90-wait-reactive`
10. `tool-p90-wait-proactive`
11. `tool-static-time-oracle-reactive`

The existing time-oracle baseline uses static Tool admission and a 4-second threshold. Preserve it as defined; it is not by itself a matched control for the new P90 policies with a 2-second checkpoint threshold.

Inventory required frozen input artifacts before scheduling: P90 predictions, held-out Tool-oracle measurements, and independent model-wait estimates for existing wait-aware policies. Do not inject held-out oracle waits into non-oracle arms, silently substitute a different admission policy, or omit a poor-performing baseline. Missing prerequisites must be reported and resolved or explicitly recorded as unavailable.

Use one frozen trace/template/workspace setup, arrival schedule, seed, and prediction artifact per paired comparison. Run arms sequentially on the host, in randomized complete repetition blocks, with verified isolation and a common cache/reset protocol. The 63-run count is a planned target, not a runtime guarantee. Estimate wall time and artifact growth from measured pilot durations; optional sensitivities come after required coverage. If coverage is incomplete, report it as incomplete.

## Implementation sequence and gates

### 1. Freeze the deployment and workload

Locate the active binaries/services and deployment paths. Preserve remote changes and existing experiments. Create an isolated, versioned implementation checkout rather than overwriting the dirty home checkout or assuming an unversioned deployment directory equals a Git commit. Record source commits, patch hashes, deployed binary hashes, SDK version, templates, guest kernel, CPU placement, NUMA topology, storage mounts, swap settings, and host background load.

Validate the selected rec-a artifact against the original recording and the managed OpenClaw replay path. Diagnose the recorded validation/request/telemetry failures and pass a complete c1 replay with the intended templates and workspace before formal use. Complete the requested Tool-duration and memory distributions and preserve the recorded wait statistics above. Freeze trace and prediction hashes; do not reselect or modify the trace based on policy performance.

Gate: reproducible baseline inventory, valid trace/prediction provenance, resource-accounting contract, and adequate measured storage/host capacity.

### 2. Prove the storage path with one sandbox

Trace the installed CubeSandbox memory-volume, snapshot-catalog, rootfs-dependency, and restore paths. Find the smallest supported per-snapshot placement hook. If absent, add a small reviewable CubeSandbox API/storage extension carried with the existing deployment patches and matching SDK; preserve ordinary SSD pause behavior.

Prove that the first memory-snapshot write targets NUMA1-backed storage, that an SSD-staged memory snapshot is never used for a WARM transition, and that restore can consume the selected memory snapshot directly. Determine whether restore is eager or retains lazy/mapped backing. This is the highest-uncertainty implementation step.

Gate: actual direct LOCAL-to-WARM-to-LOCAL and LOCAL-to-WARM-to-COLD-to-LOCAL cycles on kunpeng, with valid state and measured placement.

### 3. Add tier state, policy wiring, and telemetry

Expected ClawBox changes are concentrated in `experiments/spec_types.py`, `spec.py`, `baselines.py`, `policy.py`, `worker.py`, `memory.py`, `cube/client.py`, and `cube/lifecycle.py`, plus a small snapshot-pool module and the existing CLI/reporting paths. Use the existing replay wait source and managed gateway integration; do not introduce an LLM-time predictor.

Add reservation and snapshot-manifest state, per-role lifecycle serialization, both decision rules, and replay-only schema checks. Keep policy decisions separate from storage capacity/spill mechanics. Preserve legacy defaults while making all new formal arms use the common NUMA accounting protocol.

Add the complete transition event schema requested in the brief. Also include run/repetition/policy identifiers, wait/request ID, snapshot generation, queue/decision timestamps, status/error, and metric-availability indicators. Preserve existing CubeSandbox phase timing and collect scoped SSD I/O where supported; unavailable physical I/O remains null with a reason, not an estimated zero.

Gate: deterministic tests on kunpeng for 2/20-second boundaries, remaining-time selection, independent Runtime/Tool state, concurrent reservations, response/checkpoint races, live-mode rejection, spill failure recovery, and restore/demotion serialization; existing regression checks pass.

### 4. Complete integration gates and a c60 pilot

Run both required c1 transition chains with workspace/content and identity checks. Test the case where Runtime is LOCAL while Tool remains WARM or COLD. Force WARM overflow at c4/c8 and verify WARM-LRU ordering, reservation conservation, physical placement, correct spill recovery, current SSH endpoint, and no duplicate commands or task-owned sandbox leaks. Exercise model completion during queued and in-progress checkpoint/spill work.

Use validation-only controlled waits or reduced pool capacities where needed to force boundary cases; keep them separate from the unmodified formal trace. Verify restored pages and any retained backing charges, not just mount names or successful APIs.

Run a c60 pilot to measure runtime, snapshot sizes, storage growth, telemetry coverage, and host interference. Keep pilot data distinct unless it already meets the frozen formal protocol without implementation changes.

Gate: all correctness/placement tests pass, required telemetry reconciles, and the formal manifest plus time/storage estimate is fixed.

### 5. Run the formal matrix

Execute the 63-run target in randomized repetition blocks with a persistent run manifest and resumable bookkeeping. Record every attempt. Verify template/trace/prediction hashes, idle baseline, task ownership, and cleanup between arms. Fix implementation/infrastructure faults and rerun affected configurations under the same frozen build; retain genuine policy failure outcomes. Complete required families before optional 48/96-GiB LOCAL arms.

Gate: every planned arm has its documented repetitions/outcomes, no hidden exclusions, and complete raw evidence or explicit missing-data labels.

### 6. Produce and verify the result package

Create one study directory with `provenance/`, `workload/`, `configs/`, `raw/`, `per-run/`, `aggregate/`, `plots/`, `report.md`, and a checksummed manifest. Keep source inputs and reproduction commands with the analysis scripts.

Generate the full machine-readable main table specified in the brief. Use a compact paper main table for correctness, throughput, JCT, waits, and memory; put detailed per-tier operation and I/O columns in companion tables. Produce capacity curves, migration-cost distributions, LOCAL/WARM occupancy and residency integrals, SSD traffic, failure accounting, and a paired representative timeline chosen by a declared rule rather than best performance.

Cross-check transition counts/bytes against manifests, reservations against physical pool usage, latency records against event timestamps, and aggregates against individual runs. Rebuild figures and tables from preserved raw data without hand-entered results.

Answer RQ1, revised RQ2, RQ3, RQ4, and RQ5 only to the extent supported by measured results. If 128 GiB WARM does not show diminishing returns, report that the tested range did not locate the point. Missing measurements, unsuccessful configurations, and trace-specific scope remain visible.

Gate: a reproducible measured paper-oriented package containing all requested artifacts and the final report's ten requested sections.
