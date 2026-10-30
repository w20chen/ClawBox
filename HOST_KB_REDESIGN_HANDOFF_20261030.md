# Host-resident ClawTune KB redesign handoff

Date: 2026-10-30 (Asia/Shanghai)

Repositories:

- `C:\Users\29068\Desktop\ClawBox`
- `C:\Users\29068\Desktop\ClawTune`

This document is the authoritative implementation handoff for moving the live
ClawTune knowledge base out of all Runtime and Tool VMs and onto the experiment
host. It is intentionally self-contained so that a new code agent can begin
from this file without relying on prior conversation.

## 1. Status at handoff

No code for this redesign has been written yet. The work completed so far was
code inspection and architectural planning only.

Repository state when this handoff was written:

- ClawBox: `main` at `8f04fd9` (`Compact successful model gateway records`).
- ClawTune: `main` at `8233120` (`Eliminate redundant sidecar startup work`).
- ClawTune worktree was clean.
- ClawBox had one pre-existing untracked user file:
  `FULL_1280_FAILURE_DEBUG_HANDOFF_20261028.md`.
- Do not modify, delete, stage, or overwrite that untracked file.

Before starting implementation, recheck both repositories and the remote
Kunpeng state. Preserve unrelated changes and all data related to `anns`.

## 2. Confirmed user decisions

These are requirements, not suggestions.

1. The complete live ClawTune KB must live on the host, outside all Runtime and
   Tool VMs.
2. For now, every Runtime-Tool VM pair has one logically independent KB.
3. Every logical KB starts from the same canonical ClawTune cold-start seed.
4. A pair's KB grows from observations produced by that pair only.
5. No learned state may leak between independent pairs.
6. A resumed session in the same `run_id` and `attempt_id` reopens the same
   host KB.
7. Every new attempt starts cold, even if it belongs to the same run.
8. If a required prediction fails before Tool execution, fail only that
   affected trace before executing the Tool command. Other traces and the arm
   continue.
9. The architecture must permit future host-defined privacy/sharing policies
   without refreshing Runtime images or the 503 Tool templates again.
10. Normal operation must not write large clause-telemetry artifacts. Learning
    should occur from compact live observations.
11. Avoid excessive robustness and repeated rebuilds. This is a research
    project; implement the smallest correct supported path and rebuild only
    after the protocol and implementation are final.

## 3. Project constraints

Follow the repository `AGENTS.md` instructions supplied by the user.

- The supported path is `clawbox experiment`, CubeSandbox, and ClawTune
  prediction/measurement integration.
- Do not extend deprecated control-plane modules merely because they already
  contain an older KB service.
- Preserve experiment correctness, snapshot policy, result metadata, existing
  configuration, result directories, templates, and unrelated workloads.
- Do not touch anything related to `anns` on Kunpeng.
- Public behavior needs a public configuration field and updates to the
  scenario walkthrough and option reference.
- Validate supported commands on `ssh kunpeng` after local tests.
- Keep verification evidence separate from user documentation.
- Small tests are correctness gates. Consequential performance conclusions
  require Full-1280 and, where relevant, Full-2560 evidence.
- Do not repeatedly rebuild 503 templates during development. Freeze the
  protocol and implementation first, validate one real pair, then refresh once.

ClawTune-specific constraints discovered during inspection:

- JSON Schema is the public protocol source of truth.
- Do not modify trace datasets or OpenClaw core.
- Avoid changing shared `tool_resource` code unless the supported workflow
  requires it. This redesign does require a small no-file finalization path,
  but keep it scoped and retain the current artifact-producing behavior as an
  explicit diagnostic option.

## 4. Why the current implementation is wrong for the new design

### 4.1 The writable KB is currently inside each Runtime VM

`clawbox/experiments/openclaw_driver.py` currently:

- creates `/state/clawtune/<session>/traces/tool-resource`;
- ships and runs `scripts/initialize-clawtune-state.py` in the Runtime;
- sets `CLAWTUNE_TOOL_RESOURCE_ARTIFACT_DIR` to that Runtime path;
- sets `CLAWTUNE_KB_SEED=/opt/clawtune/cold-start/tool-resource`;
- launches a full local ClawTune sidecar which loads and mutates that state.

Relevant current locations are around lines 635-653 and 788-805 of
`clawbox/experiments/openclaw_driver.py`.

### 4.2 Admission uses a different host-side frozen KB

`clawbox/experiments/prediction.py` has:

- `RuntimePredictionProvider`, which loads an immutable training KB on the
  host;
- `ColdStartPredictionProvider`, which loads and freezes the cold-start seed on
  the host;
- immutable host prediction caches keyed without a pair-local learned
  generation.

The Runtime currently sends a compact `call_prediction`, but the host live
provider still re-queries its own frozen KB. Thus Runtime learning and host
admission are not one coherent model.

### 4.3 Tool telemetry is produced in a different VM

The Tool bridge starts ClawTune's guest collector. The collector currently
finalizes a full `clause-telemetry-<execution_id>.json`, reads it back, and
returns only a summary to the Go bridge. The Runtime-local KB therefore cannot
directly learn the Tool VM's qualified clause and cgroup measurements without
an additional transfer path.

Relevant files:

- ClawTune `tools/guest_collector_server.py`
- ClawTune `services/sidecar/src/tool_resource/telemetry.py`
- ClawBox `toolbridge/guest_collector.go`
- ClawBox `toolbridge/main.go`
- ClawBox `toolbridge/collector.go`

### 4.4 The older KB endpoint is not the solution

ClawBox already contains `CLAWBOX_KB_ENDPOINT`, snapshot pull, batch flush, and
`clawbox.tuning.server`. Do not reuse that design as the live supported path.

It is unsuitable because it:

- downloads KB snapshots into Runtime VMs;
- ingests after a trace instead of updating live before later predictions;
- is primarily keyed by tenant/repository rather than the required pair scope;
- rebuilds or deserializes full snapshots on its request path;
- belongs to older batch/control machinery rather than the standalone
  experiment control lifetime.

It can be used as a source of validation or projection code, but do not route
the supported experiment through it.

### 4.5 Full KB copies do not scale

The checked-in ClawTune `bootstrap-v2` seed is 9,479,647 bytes serialized:

- `edge-kappa-kb.json`: 5,815,394 bytes
- `runtime-tool-resource-kb.json`: 1,854,362 bytes
- `clause-resource-kb.json`: 1,250,114 bytes
- `clause-lattice-time-kb.json`: 551,696 bytes
- `manifest.json`: 8,081 bytes

At 1,280 pairs, full serialized copies alone would occupy 12,133,948,160 bytes
(about 11.3 GiB). Python object graphs would require substantially more RAM.
Do not construct one complete seed-derived predictor object per pair.

## 5. Target architecture

### 5.1 Physical and logical model

Load one immutable cold-start base on the host. Give each logical KB a small
writable overlay:

```text
logical KB(pair P) = immutable cold-start base + writable overlay(P)
```

Physically:

```text
Experiment host
  cold-start base (one read-only instance)
  overlay A (pair A only)
  overlay B (pair B only)
  overlay C (pair C only)
  capability -> KnowledgeView mapping
```

The base must never be mutated by online observations.

### 5.2 KnowledgeView indirection

VM clients must not submit a client-chosen KB or privacy-scope identifier.
Each authenticated pair capability resolves on the host to a `KnowledgeView`:

```text
KnowledgeView
  readable_scope_ids: ordered tuple
  writable_scope_id: one scope
  seed_digest: immutable base identity
  mapping_policy_digest: frozen experiment policy identity
```

Current mapping:

```text
readable_scope_ids = [private_pair_scope]
writable_scope_id  = private_pair_scope
```

Future mappings may point multiple pair capabilities at the same overlay or
give a task a private overlay plus readable group/tenant overlays. This future
change must be host-only. The version-1 VM protocol should already operate in
terms of a capability, not a hard-coded pair KB ID.

Freeze the mapping for the lifetime of a run. A mapping-policy change requires
a new run ID, but not new VM images.

### 5.3 Pair identity and lifetime

For current pair isolation, derive the private logical scope from:

```text
(run_id, attempt_id, arm_id, session_id)
```

Do not use only `session_id`, because arm IDs can repeat across attempts.

Lifecycle rules:

- VM checkpoint/restore: same scope.
- VM recreation while resuming the same run/attempt/session: same scope.
- Worker interruption and same-attempt recovery: reopen the persisted scope.
- New `attempt_id`: create a new scope from the cold seed.
- New run: create a new scope from the cold seed.
- Finished scope: retain compact state as run evidence, but never implicitly
  reuse it in another attempt.

## 6. Runtime request flow

```text
Runtime OpenClaw plugin
    before_tool_call
        -> Runtime-local ClawTune sidecar
            -> host /v1/kb/predict
                -> cold base + permitted overlays
                <- prediction, generation, signed/opaque receipt
        <- normal ToolDecision

Runtime SSH wrapper
    -> host /v1/tool/admit with command digest + prediction receipt
        host resolves canonical prediction from receipt
    -> Tool VM executes command

Tool bridge
    -> guest collector finalizes compact observation in memory
    -> host /v1/kb/observe
        host commits only to the pair's writable overlay
        host returns committed generation
    -> SSH command returns original result

Runtime OpenClaw plugin
    after_tool_call
        -> Runtime-local sidecar for trace/event completion
```

The Tool observation acknowledgement must occur before the SSH command returns.
Therefore the next Tool call in the same pair observes every successfully
acknowledged earlier update.

Use a persistent HTTP client in the Tool bridge. Do not add polling or a new
SSH round trip.

## 7. Host service boundary

### 7.1 Keep one supported experiment control path

Add a `HostKnowledgeManager` to the standalone experiment worker. Compose it
with the existing host policy-control listener rather than deploying a second
product or using the deprecated control plane.

Recommended ownership:

- `ExperimentWorker` owns the manager for the run/attempt.
- `PolicyControlServer` (or a narrowly renamed/composed experiment-control
  server) exposes KB routes on the existing control address/port.
- Each session owns a pair binding, capability, prediction receipts, and
  policy callbacks.

Keeping the existing listener avoids another public port, another network
allowlist entry, and another service lifecycle.

### 7.2 Two-stage session registration

Current `PolicyControlServer.register()` occurs after VM creation, too late to
put its token in the Tool VM environment. Split registration into two stages:

1. Reserve the pair session and KB binding before creating either VM.
2. Generate the capability and place endpoint/capability in both VM
   environments.
3. Create both VMs.
4. Attach `admit`, `complete`, `abandon`, and progress callbacks before
   OpenClaw starts.

Requests requiring policy callbacks must fail clearly until callbacks are
attached. KB prediction/observation routing may be registered earlier.

Cleanup must close policy state and checkpoint/close the KB binding even when
VM creation or Agent execution fails.

### 7.3 Network path

`clawbox/experiments/worker.py` already gives both Runtime and Tool VMs narrow
egress to `CLAWBOX_CONTROL_HOST` (around lines 3304-3347). Reuse it. Do not grant
additional general network access for KB traffic.

## 8. Version-1 KB protocol

Define public request/response JSON Schemas in ClawTune first. Generate or
validate Python/TypeScript/Go payloads against those schemas. Use a protocol
version field from the first implementation so future host sharing changes do
not require image refreshes.

### 8.1 `POST /v1/kb/predict`

Authenticated by the pair capability. The client must not provide a writable
scope ID.

Minimum request fields:

```json
{
  "schema_version": "clawtune.host-kb.predict.v1",
  "session_id": "...",
  "event_id": "...",
  "tool_call_id": "... or null",
  "tool_name": "exec",
  "command": "logical command",
  "command_sha256": "64 lowercase hex characters",
  "repository": "owner/repo",
  "memory_measurement": "cgroup_v2_environment_union_v1"
}
```

The host must validate the repository and session against the capability's
binding. Do not trust them as routing inputs.

Minimum response fields:

```json
{
  "schema_version": "clawtune.host-kb.prediction.v1",
  "seed_sha256": "...",
  "mapping_policy_sha256": "...",
  "scope_generation": 7,
  "prediction_receipt": "opaque authenticated value",
  "prediction": {},
  "model_predictions": {},
  "fallback": null,
  "service_time_ns": 0
}
```

The response should contain the normal ClawTune `ToolPrediction` information
needed by the plugin and the canonical selected admission signal.

### 8.2 Prediction receipt

The plugin calls `decide()` before `instrumentExecParams()` generates the
ClawBox `execution_id`. Therefore the prediction receipt cannot initially bind
an execution ID unless the plugin ordering is substantially changed.

The simplest design is:

- bind the receipt to pair capability, event/tool-call identity, command
  digest, scope generation, selected prediction, and session lifetime;
- put the receipt in the SSH execution envelope;
- when `/v1/tool/admit` receives the new execution ID, atomically claim that
  receipt for the execution ID;
- reject receipt reuse for a second execution side effect;
- retain normal idempotency for retries of the same admission request.

Do not trust a prediction body supplied by the VM when a valid receipt exists.
The host should resolve the canonical prediction associated with the receipt.

### 8.3 `POST /v1/kb/observe`

Sent directly by the Tool bridge using the same pair binding.

Minimum identity fields:

- schema version;
- session ID witness;
- execution ID;
- command SHA-256;
- tool name;
- logical command identity;
- exit code/outcome;
- measurement contract.

Learning fields:

- qualified call duration;
- cgroup CPU and memory measurements;
- memory baseline, total peak, and extra peak;
- memory environment ID and eligibility;
- compact qualified clause observations;
- telemetry quality, loss, cleanup, and collection-validity summary;
- qualified PMU summary when available.

Explicitly exclude:

- raw per-process image diagnostics;
- full BPF maps or debug structures;
- command output unrelated to learning;
- the previous full clause-telemetry artifact.

Response fields:

- accepted/duplicate status;
- committed scope generation;
- observation and clause counts;
- compact payload digest;
- service time;
- explicit rejection/error reason.

### 8.4 Idempotency

Deduplicate observations by:

```text
(pair binding, execution_id, command_sha256)
```

An identical duplicate returns the already committed generation. Reuse of an
execution ID with different content is a validation failure.

## 9. Causal ordering

Do not use guest wall clocks as the authority across Runtime and Tool VMs.
Their clocks may differ.

Assign causal order on the host:

- prediction receives a host sequence/generation witness;
- a Tool observation becomes visible only after the host acknowledges it;
- the next prediction for that KnowledgeView sees all earlier acknowledged
  observations;
- uncompleted or unacknowledged executions remain invisible.

Retain guest timestamps as measurement evidence, but normalize KB visibility
using host receive/commit order. Existing ClawTune classes currently use
`ts_end < query.ts_start`; the host adapter may assign monotonic host timestamps
or introduce an explicit sequence abstraction, but the result must preserve
the same strictly-prior rule.

## 10. Layered KB implementation

The production implementation must share immutable cold evidence and keep only
small mutable overlays. A complete predictor per pair is an unacceptable
Full-1280 design.

### 10.1 ToolKB (`RuntimeToolResourceKB`)

Current useful property:

- `_public` is immutable after fitting/loading;
- `_repo` and `_pending` hold mutable online state.

Required refactor:

- load one immutable base public layer and seed repository layer;
- keep pair-private repository observations in an overlay;
- for a node that exists in both seed repository evidence and the private
  overlay, combine evidence exactly as a complete independent seed copy would;
- fall back from private/seed repository evidence to immutable public evidence
  using existing selection semantics;
- keep pending/causal state per KnowledgeView, not globally.

Do not implement isolation by replacing the real repository with a synthetic
pair repository. `bootstrap-v2` contains seed repository state for 12 real
repositories; a synthetic name would incorrectly discard that prior.

### 10.2 TrieKB (`ClauseResourceKB`)

Apply the same base-plus-overlay structure:

- immutable public and seed-repository evidence shared once;
- private prefix/clause observations per writable scope;
- per-view pending and monotonic causal state;
- layered node values equivalent to a full independent KB.

### 10.3 LatticeKB (`LatticeTimeKB`)

This is the most important isolation hazard.

Current LatticeKB intentionally builds common and repository-specific nodes in
one mapping. An online observation contributes to common nodes as well as repo
nodes. One shared mutable LatticeKB would therefore leak pair A's evidence into
pair B.

Implement:

- one frozen prepared seed lattice;
- one small delta observation log/prepared delta per writable scope;
- query-time combination of matching base and permitted delta node samples;
- background preparation only for the changed scope;
- no global rebuild when one pair learns;
- copy-on-write publication of a scope's prepared delta.

The combination must reproduce a conventional full KB initialized from the
seed and fed the same observations. Add differential tests comparing all
returned target distributions and selected contexts.

### 10.4 EdgeKappa

The seed's EdgeKappa snapshot is the largest component. Do not clone its full
graph per pair.

Implement a shared frozen base plus private overlay for:

- call tokens;
- completed outcomes;
- feedback/updated edge weights;
- observation IDs;
- completed call IDs.

Only the pair's permitted overlay may affect its query. Validate the layered
result against a full-copy reference for a short training sequence.

If EdgeKappa cannot be layered without a large implementation, first determine
whether it affects the supported ClawBox admission signal. It may be explicitly
disabled for the host-live paper workflow only if evidence shows it does not
affect the experiment's claimed idea/results. Do not silently share mutable
EdgeKappa state.

### 10.5 Execution-context KB

Store duration-context samples per writable scope. On prediction, read only
the overlays permitted by the KnowledgeView. The current seed does not include
an execution-context snapshot, but the protocol and persistence should still
cover it.

### 10.6 Command-rule cache

Bundled parser rules are code/read-only model assets and may be shared.
Dynamically learned rules are knowledge and must be scoped. For the current
replay path, keep dynamic generation disabled unless it is demonstrated to
affect predictions used by the experiment.

## 11. Host persistence

Do not use one existing full `StateStore` per pair. It is a one-writer full
snapshot store which retains current and previous complete generations.

Add a compact host overlay store under the run output. Suggested layout:

```text
<run-output>/kb/
  manifest.json
  scope-map.json
  scopes/
    <opaque-scope-id>/
      state.json
      observations.jsonl
  summary.json
```

`manifest.json` must include:

- schema version;
- run and attempt IDs;
- seed path and digest;
- ClawTune revision;
- mapping-policy digest;
- memory measurement semantics;
- protocol version.

Each scope stores only compact accepted learning records and small overlay
state. It must not duplicate the cold seed or raw telemetry.

Persistence rules:

- append/commit an accepted observation before acknowledging it;
- increment generation once per accepted Tool execution, not once per clause;
- recover a same-attempt scope by replaying/checkpointing its compact overlay;
- never open an old attempt's scope as the current attempt;
- retain a summary with counts, generation, digests, failures, and sizes;
- do not put secret capabilities in result artifacts or logs.

For this research system, a compact append journal plus periodic overlay-only
checkpoint is sufficient. Do not build a general distributed database.

## 12. Runtime VM behavior

Keep the Runtime-local ClawTune sidecar because it also owns:

- OpenClaw event handling;
- Runtime trace production;
- the local LLM proxy;
- health/lifecycle integration.

In host-live mode it must not initialize, load, or persist a local KB.

Add a `RemoteKnowledgeClient`/remote predictor implementation which:

- calls the authenticated host prediction route;
- converts the host response to the existing `ToolDecision` contract;
- reports seed digest, mapping digest, and generation in health/provenance;
- keeps no mutable KB snapshot in the VM;
- does not silently create a fallback local KB.

The OpenClaw plugin should continue calling `http://127.0.0.1:8765`. The local
sidecar, not the plugin, communicates with the host KB endpoint. This retains
the current local trace and LLM behavior.

## 13. Tool VM behavior

Refactor ClawTune `ClauseTelemetryCollector.finalize()` so it can return the
final artifact object without writing it. Preserve explicit artifact writing
for diagnostics/tests.

In normal ClawBox mode:

1. Finish the eBPF call.
2. Build the finalized artifact in memory.
3. Derive compact qualified clause observations.
4. Combine them with Tool bridge cgroup/PMU measurements.
5. POST the compact observation to the host.
6. Record only update count, generation, digest, duration, and error summary.
7. Return the original Tool result.

Default behavior must create no `clause-telemetry-*.json` file.

Retain a public diagnostic setting such as
`execution.retain_clause_telemetry_artifacts: false`. Enabling it requires a
new run ID but no image rebuild.

## 14. Admission integration

Add an explicit `host-live` prediction mode. Do not silently change the meaning
of the current frozen/cold-start modes during migration.

In host-live mode:

- the Runtime receives a prediction from the host KnowledgeView;
- the plugin carries the prediction receipt in the execution envelope;
- `/v1/tool/admit` claims and validates the receipt;
- admission retrieves the canonical host prediction associated with it;
- host-memory scaling/calibration remains admission policy;
- any cache key includes pair binding and scope generation;
- no separate `ColdStartPredictionProvider` query is used for that command.

Frozen mode may remain as an explicit research baseline. It is not live
learning.

## 15. Failure semantics

These rules are important for correctness and run isolation.

### 15.1 Before Tool execution

- Host KB preflight failure: do not start the arm.
- Prediction request/validation failure for one trace: fail that affected
  trace before executing the Tool command.
- Continue other traces and the arm.
- Do not use another pair's KB.
- Do not silently weaken admission or invent a prediction.

The SSH wrapper should return a clear nonzero failure (the current policy path
uses 125) and retain the exact error in validation/result evidence.

### 15.2 After Tool execution

An observation update failure happens after the command side effect and cannot
be repaired by re-executing it.

- Never retry the Tool command.
- Preserve its real stdout/stderr/exit code.
- Mark the trace as KB-update failed/degraded and fail its online-learning
  validity.
- Continue other traces.
- Retain the compact error in Tool bridge and host result summaries.

Do not turn an observation transport error into a different command output.

### 15.3 Optional cold-seed fallback

The user selected failure before execution for a required prediction. Do not
add automatic cold-seed fallback unless the user later explicitly requests it.

## 16. Public configuration

Keep the supported configuration minimal and explicit.

Recommended changes:

- Add `host-live` to `resources.prediction_mode`.
- Continue using `resources.prediction_artifact` as the explicit seed path for
  host-live mode.
- Add `execution.kb_scope_policy`, initially accepting only `pair`.
- Add `execution.retain_clause_telemetry_artifacts`, default `false`.
- Add a host-profile KB state root, preferably under a dedicated configured
  host path rather than an implicit temporary directory.
- Reuse `CLAWBOX_CONTROL_HOST` and the current experiment control port.

The frozen experiment specification must record:

- seed path/digest;
- scope policy and its digest;
- host-live protocol version;
- measurement semantics;
- retention setting.

Future sharing policies may add host-side resolver configuration. They must not
require new fields in VM messages.

Update:

- `docs/guide.md`
- `docs/reference.md`
- `docs/installation.md` if the host state root or recovery command changes
- `docs/results.md` for KB overlay artifacts and degraded-trace interpretation

Do not put debugging history or Kunpeng-specific validation narratives in user
documentation.

## 17. Likely code locations

### ClawTune

- `contracts/`: add host-KB protocol schemas.
- `services/sidecar/src/clawtune_kb/`: shared base and overlay store/view.
- `services/sidecar/src/tool_resource/runtime_kb.py`: layered ToolKB/TrieKB
  primitives or narrow reusable base/overlay APIs.
- `services/sidecar/src/tool_time/lattice_kb.py`: layered lattice view.
- `services/sidecar/src/clawtune_sidecar/predictors/edge_kappa.py`: scoped
  overlay or explicit host-live exclusion backed by evidence.
- `services/sidecar/src/execution_context/duration.py`: scoped context samples.
- `services/sidecar/src/clawtune_sidecar/api/dependencies.py`: select remote
  predictor without creating local state.
- `services/sidecar/src/clawtune_sidecar/api/app.py`: remote predictor lifecycle
  and health/provenance.
- `services/sidecar/src/clawtune_sidecar/config.py`: remote endpoint/auth mode.
- `services/sidecar/src/tool_resource/telemetry.py`: no-write finalization.
- `tools/guest_collector_server.py`: return compact observations in memory.
- `packages/clawtune-plugin/src/contracts.ts`: prediction receipt field.
- `packages/clawtune-plugin/src/exec-instrumentation.ts`: put receipt in the
  ClawBox envelope.
- corresponding Python/Node tests.

### ClawBox

- `clawbox/experiments/policy_control.py`: two-stage session binding and KB
  routes, or composition with a new host knowledge manager.
- new focused module such as `clawbox/experiments/host_kb.py`: do not bury all
  KB logic in the HTTP handler.
- `clawbox/experiments/worker.py`: early pair registration, VM environment,
  persistence root, cleanup, result/provenance.
- `clawbox/experiments/openclaw_driver.py`: start Runtime sidecar in remote-KB
  mode and stop initializing Runtime-local KB state.
- `clawbox/experiments/prediction.py`: host-live receipt resolution; no second
  frozen query for live calls.
- `clawbox/experiments/spec.py` and CLI/configuration helpers.
- `scripts/clawbox-policy-ssh.py`: carry receipt and preserve pre-execution
  failure behavior.
- `toolbridge/guest_collector.go`: receive compact observations.
- `toolbridge/main.go`: authenticated host observation POST and compact audit.
- `toolbridge/progress.go`: reuse its pooled HTTP conventions where useful;
  do not overload progress semantics.
- Go and Python tests listed below.

## 18. Implementation phases

Do not rebuild images during phases 1-7.

### Phase 1: contracts and reference semantics

1. Add JSON Schemas for prediction, receipt metadata, and compact observation.
2. Write reference tests using a conventional full independent KB.
3. Define exact generation and causal visibility semantics.
4. Keep these tests as the oracle for layered behavior.

### Phase 2: shared base and overlays

1. Load/validate the seed once.
2. Implement ToolKB and TrieKB overlays.
3. Implement LatticeKB delta/query combination.
4. Implement or explicitly scope EdgeKappa/context behavior.
5. Add differential tests against full copies.
6. Measure memory for 1,280 empty overlays locally or with a synthetic host
   benchmark; do not claim Kunpeng performance yet.

### Phase 3: host manager and persistence

1. Add `HostKnowledgeManager`.
2. Add pair binding and KnowledgeView mapping.
3. Add append/checkpoint recovery.
4. Add prediction receipts and observation idempotency.
5. Add per-scope locks; avoid one global prediction/update lock.
6. Add bounded compact metrics, not heavy per-node logging.

### Phase 4: experiment control integration

1. Split pair reservation from policy callback attachment.
2. Inject endpoint/capability into both VM environments before creation.
3. Add KB routes to the existing control listener.
4. Integrate cleanup and same-attempt recovery.
5. Make admission consume the receipt.

### Phase 5: Runtime remote predictor

1. Add remote mode to the local sidecar.
2. Do not initialize local writable state in that mode.
3. Preserve local trace and LLM proxy behavior.
4. Put the receipt into the plugin/SSH envelope.
5. Verify prediction failure blocks before SSH execution.

### Phase 6: Tool compact observation

1. Add no-file collector finalization.
2. Return compact qualified observations to the Go bridge.
3. POST synchronously through a persistent client.
4. Preserve the original command result.
5. Add explicit debug artifact retention, default off.

### Phase 7: configuration, results, and documentation

1. Add public fields and validation.
2. Record seed/mapping/protocol provenance.
3. Add KB summary artifacts and degraded-trace classification.
4. Update guide/reference/results/installation as required.
5. Validate fresh-shell CLI help and configuration errors locally.

### Phase 8: real validation and one rebuild

1. Run one real Kunpeng pair using overlay code shipped from the checkout where
   possible.
2. Run two pairs and prove isolation.
3. Validate checkpoint/restore and same-attempt recovery.
4. Freeze the protocol and code.
5. Rebuild the Runtime image/template once.
6. Refresh the Tool image and all 503 Tool templates once.
7. Verify image/kernel/revision identity and sample several templates.
8. Run one final Full-1280 with default logging.

Do not perform repeated 503 refreshes while fixing local test failures.

## 19. Required tests

### 19.1 ClawTune unit tests

- Empty overlays produce the same predictions as the seed.
- Pair A update changes A's prediction.
- The same query for pair B remains unchanged.
- Layered ToolKB output matches a full-copy reference.
- Layered TrieKB output matches a full-copy reference.
- Layered LatticeKB distributions and contexts match a full-copy reference.
- EdgeKappa/context behavior is isolated or explicitly disabled with evidence.
- Same-scope update becomes visible only after acknowledgement/strictly later
  prediction.
- No-write collector finalization returns a valid compact payload and creates
  no clause telemetry file.
- Diagnostic retention still creates and validates the artifact.
- JSON Schema validation covers valid, malformed, oversized, and mismatched
  identity payloads.

### 19.2 ClawBox Python tests

- Reserve pair before VM creation; attach callbacks later.
- Capability cannot route to another session/scope.
- Prediction receipt binds command digest and generation.
- Receipt is claim-once for execution side effects and idempotent for exact
  admission retries.
- Host-live admission never calls the frozen host provider.
- Same run/attempt/session reopens the same persisted generation.
- New attempt starts at generation zero over the seed.
- Prediction failure fails one trace before SSH execution and does not cancel
  peer traces.
- Observation failure after execution preserves the actual command result and
  marks KB update failure.
- Result provenance contains seed/mapping/protocol identities but no secrets.
- Recovery rebuilds overlay state from committed compact records.

### 19.3 Tool bridge Go tests

- Compact observation excludes raw diagnostics.
- Host POST includes exact execution ID and command digest.
- Persistent client reuses the connection.
- Identical retry is safe.
- Update failure is logged without re-executing or rewriting the command
  result.
- Default mode produces no clause telemetry path in the execution audit.
- Explicit diagnostic mode retains the artifact.

### 19.4 Plugin/Runtime tests

- Remote prediction becomes a normal `ToolDecision`.
- Receipt is present in the execution envelope.
- The command digest in the receipt matches the logical profile command, not
  the session-specific shell wrapper.
- Prediction failure with fail-closed host-live mode returns before executing
  SSH.
- Local trace and LLM proxy still work without a local KB.

## 20. Kunpeng correctness gates

Do not start with Full-1280.

### Gate 1: one pair

Run several sequential commands with qualified Tool measurements.

Prove:

- host generation starts at zero over the seed;
- each accepted execution increments it once;
- a later prediction uses the updated overlay;
- no writable KB exists in either VM;
- no `clause-telemetry-*.json` exists in default mode;
- Tool output/exit code is unchanged;
- host receipt is the admission source.

### Gate 2: two isolated pairs

Use identical starting commands, then deliberately feed different measured
outcomes.

Prove:

- initial predictions are equal;
- A's later prediction changes according to A;
- B remains unchanged until B learns;
- neither scope's persisted records contain the other's execution IDs;
- tokens cannot cross-route.

### Gate 3: lifecycle

- Checkpoint/restore both VMs; host KB remains available and generation is
  unchanged.
- Recreate a VM in the same run/attempt/session; it reconnects to the same KB.
- Start a new attempt; it starts cold.
- Interrupt/restart the experiment worker in a controlled small test and
  verify recovery from committed overlay state.

## 21. Full-1280 final validation

Use default logging, not diagnostic/heavy logging.

Collect at least:

- host KB prediction request count;
- observation request count;
- accepted/duplicate/rejected count;
- per-request p50/p95/p99 service and end-to-end latency;
- receipt/admission lookup latency;
- overlay preparation/update time;
- total host KB RSS and CPU timeline;
- shared base bytes and total overlay bytes;
- number of active scopes and generations;
- host control queue depth/thread count;
- Tool-side KB POST duration;
- prediction and observation error counts;
- number of failed/degraded traces;
- confirmation that no raw clause telemetry files were generated;
- experiment output size by category.

Acceptance conditions:

- no cross-pair prediction change;
- no local writable VM KB;
- no raw clause telemetry in default mode;
- prediction failures isolated to affected traces;
- no global lock or serialized host request path causing material Full-1280
  slowdown;
- host KB memory is dominated by one seed plus measured overlays, not 1,280
  seed copies;
- default logging overhead remains small relative to actual Tool and model
  execution.

Do not run Full-2560 merely because Full-1280 passes. Use it only when needed
for the experiment's consequential scale claim or when Full-1280 shows a
contention path that needs validation at 2560.

## 22. One-time image/template refresh rule

One final refresh is unavoidable because current images do not implement the
stable host-KB protocol:

- Runtime image/template: current sidecar assumes a local KB.
- Tool image and 503 Tool templates: current bridge/collector writes artifacts
  and does not send compact host observations.

After the version-1 protocol is frozen and these images are refreshed, future
privacy/sharing mapping changes must be host-only. Do not require another image
or 503-template refresh merely to map pairs to different readable/writable KB
scopes.

Before rebuilding on Kunpeng:

- preview disk usage and obsolete artifacts;
- protect all `anns` paths/workloads;
- remove only explicitly safe obsolete templates or crash dumps;
- leave enough space for all 503 refreshed templates and Cube's 85% snapshot
  rejection threshold;
- start with a few templates and watch them before continuing;
- use `nohup` for the long refresh only after the initial templates are clean.

## 23. Immediate next action for the implementing agent

1. Recheck both git worktrees and read applicable `AGENTS.md` files.
2. Do not touch the existing untracked failure-debug handoff.
3. Write the protocol schemas and reference full-copy differential tests first.
4. Implement the shared seed/pair-overlay layer before changing VM behavior.
5. Do not rebuild or run Full-1280 until local tests and the single-pair
   Kunpeng gate pass.
6. Commit and push each important verified milestone in the appropriate
   repository, keeping ClawBox and ClawTune revisions recorded together in the
   validation evidence.

The redesign is complete only when the host owns all mutable KB state, each
pair is independently isolated by host mapping, same-attempt recovery works,
new attempts start cold, admission uses the pair's live host prediction, and
future sharing policies can be changed without modifying VM clients.
