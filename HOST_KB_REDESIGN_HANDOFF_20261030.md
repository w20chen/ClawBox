# Host-resident ClawTune KB redesign handoff

Last reviewed: 2026-11-01 (Asia/Shanghai)

Repositories:

- `C:\Users\29068\Desktop\ClawBox`
- `C:\Users\29068\Desktop\ClawTune`

This document is the authoritative implementation handoff for moving the live
ClawTune knowledge base out of all Runtime and Tool VMs and onto the experiment
host. It is intentionally self-contained so that a new code agent can begin
from this file without relying on prior conversation.

## 1. Status at handoff

This is the implementation target, not a description of deployed behavior.
Host-live implementation is in progress in ClawBox; it is not yet deployment-qualified. No images/templates have been refreshed and no
new full experiment has run. Verification evidence is separate from this handoff.

Implementation baseline (2026-11-01; the handoff already had user edits):

- ClawBox: `73364d78ac55e0fb0dbdb824935fa8a0f4be5864` (`update to tune`).
- ClawTune: `4a6d0af66e76b377bfc957baf84a5f414c6d8bf5`.
- ClawBox now pins the unchanged upstream revision above. `bootstrap-v2` bytes remain unchanged.
- Preserve current parser/query/composition reuse and completion repair behavior.
- Execution-context KB and obsolete seed bundles are removed upstream.
- New live training uses current ClawTune marked imputation, by user decision.
- This handoff and `FULL_1280_FAILURE_DEBUG_HANDOFF_20261028.md` are now
  tracked. Preserve the latter as unrelated work.
- Kunpeng is reachable. Implementation and validation are in progress; no final
  corpus refresh or Full-1280 acceptance run has completed.
- User scope correction: do not modify ClawTune. All adaptations belong to
  ClawBox; use unchanged upstream at the revision above. Any later sections
  naming ClawTune edit locations identify source to borrow into ClawBox instead.


Before starting implementation, recheck both repositories and the remote
Kunpeng state. Preserve unrelated changes and all data related to `anns`.

Changes from the original handoff: remove the deleted EdgeKappa work; correct
seed sizes; reuse existing incremental Lattice maintenance; account for command
and command context; specify attempt-scoped storage and actual resume work;
make the VM protocol independent of future sharing topology. Sections 2, 5.3,
and 15.2 include the user's decisions confirmed during this update.

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
12. Plain `clawbox experiment resume RUN_ID` reopens the previous attempt and
    its learned KB. Starting a new attempt must be explicit and starts cold.
13. If an observation update fails after bounded retries, preserve the command
    result and continue that trace using its last committed KB. Mark its
    online-learning validity degraded; continue other traces normally.

Requirements above are settled. Concrete class names and proposed fields below
are implementation guidance. Preserve current prediction/learning semantics
unless a change is explicitly agreed; do not use this redesign to restore
removed models, change the feature bound, or introduce new sharing policies.

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

Find the initialization and plugin configuration by
`CLAWTUNE_TOOL_RESOURCE_ARTIFACT_DIR`, `CLAWTUNE_KB_SEED`, and `failOpen` in
`clawbox/experiments/openclaw_driver.py`; line numbers will move.
The current plugin configuration uses `mode: observe` and `failOpen: false`.
ClawTune's plugin catch path still permits execution in observe mode. Setting
`failOpen` alone therefore does not establish the required host-live failure
behavior; validate both the before-hook and SSH admission guard.

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

The checked-in ClawTune `bootstrap-v2` seed is 3,664,164 bytes serialized at
`e14b21a` (file bytes including the manifest):

- `runtime-tool-resource-kb.json`: 1,854,362 bytes
- `clause-resource-kb.json`: 1,250,114 bytes
- `clause-lattice-time-kb.json`: 551,696 bytes
- `manifest.json`: 7,992 bytes

At 1,280 pairs, full serialized copies alone would occupy 4,690,129,920 bytes
(about 4.37 GiB). This is a storage calculation, not a measured RSS estimate.
Prepared Python structures require a separate memory measurement.
Do not construct one complete seed-derived predictor object per pair.

`clawtune_kb.default_seed_directory()` selects `bootstrap-v2`; image builds can
override it with `CLAWTUNE_KB_SEED`. Do not infer deployed seed identity from
that default. The reviewed manifest SHA-256 is
`c8dbfda638a992301295bd2f06284e82bd8bdc68d8fa09f453a3b06631b8d0fb`.
Validate every declared snapshot hash as well as the manifest before loading.
Its recorded provenance includes historical missing-label imputation and use of
the original train/test tasks as a deployment prior. Preserve that provenance;
do not describe it as a held-out evaluation or silently regenerate it. New
live observations follow current ClawTune marked-imputation semantics, as
confirmed by the user on 2026-11-01. Preserve observed, approximate, missing, and
imputed provenance; never describe imputed training values as measurements.

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

VMs treat the view revision as an opaque value. Internally it identifies the
seed, policy, and ordered `(scope_id, generation)` read set captured for one
prediction. A single scope counter cannot identify a future multi-scope view.
Keep scope IDs and merge rules host-side; the first implementation must support
only `pair`. Future sharing work must explicitly define overlap/deduplication,
evidence weighting, and visibility semantics before enabling another policy.
This handoff provides the boundary for that work, not its statistical policy.

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

The current CLI does not satisfy this target: `clawbox/cli.py` generates a new
attempt ID for `resume` unless one is supplied. In addition,
`ExperimentSupervisor._run_arm()` creates the attempt/arm directory with
`exist_ok=False`, and recovery cleans interrupted VMs before restarting arms.
Passing an old ID alone is not same-attempt recovery.

Implement plain resume by resolving the saved attempt ID. An explicit different
`--attempt-id` starts cold; document that choice for both direct resume and
`launch` of an existing run. Preserve completed results and prior invocation
logs when reopening an interrupted attempt. Keep scope storage stable across
worker invocations, append recovery evidence, and reconcile execution IDs before
accepting observations. Never overwrite the earlier attempt to make resume work.

KB recovery does not by itself restore OpenClaw progress or the Tool filesystem.
The recovery gate must show which calls completed, which are unresolved, and
where execution resumes. Do not replay already completed calls into a retained
KB and report that as uninterrupted learning. An unresolved execution must not
be re-executed automatically; mark the affected trace incomplete if its progress
cannot be recovered. Other recoverable traces may continue.

## 6. Runtime request flow

```text
Runtime OpenClaw plugin
    before_tool_call
        -> Runtime-local ClawTune sidecar
            -> host /v1/kb/predict
                -> cold base + permitted overlays
                <- prediction, opaque view revision, prediction receipt
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

Before prediction, forward the existing command/task context required by the
predictor. At completion, include compact outcome evidence required by the
resource eligibility and censoring rules. Runtime completion must not learn the
same resource sample a second time.

The Tool observation acknowledgement must occur before the SSH command returns.
Therefore the next Tool call in the same pair observes every successfully
acknowledged earlier update.

This is the success path. Exhausted observation retries instead return the
original command result with a degraded-learning marker (section 15.2).

Use a persistent HTTP client in the Tool bridge. Do not add polling or a new
SSH round trip.

## 7. Host service boundary

### 7.1 Keep one supported experiment control path

Add a `HostKnowledgeManager` to the standalone experiment worker. Compose it
with the existing host policy-control listener rather than deploying a second
product or using the deprecated control plane.

Recommended ownership:

- Each arm's `ExperimentWorker` owns its manager for that run/attempt/arm.
- `PolicyControlServer` (or a narrowly renamed/composed experiment-control
  server) exposes KB routes on the existing control address/port.
- Each session owns a pair binding, capability, prediction receipts, and
  policy callbacks.

Keeping the existing listener avoids another public port, another network
allowlist entry, and another service lifecycle.

The supervisor currently launches an isolated worker per arm, so "one base"
means one prepared base per worker process, shared by all its pairs. Do not
claim one Python object across processes. Future cross-arm/run sharing needs an
explicit host ownership/lifetime design; pair clients still need no scope IDs.

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
egress to `CLAWBOX_CONTROL_HOST`. Reuse it. Do not grant
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
  "command_context": {},
  "context_inputs": {},
  "memory_measurement": "cgroup_v2_environment_union_v1"
}
```

The host must validate the repository and session against the capability's
binding. Do not trust them as routing inputs.

These are illustrative envelopes, not final schemas. Before freezing v1, define
the typed `context_inputs` projection from the existing ToolBeforeRequest:
logical command arguments, declared cwd/relevant environment, task-instance
and call correlation witnesses, and command-parser context. Bind task identity
on the host so VM recreation does not manufacture a new independent training
task. Carry clause identity/features through completion so learning uses the
same interpretation as prediction. Do not resolve guest paths on the host or
silently drop current context inputs. Keep unknown context explicitly unknown.

Minimum response fields:

```json
{
  "schema_version": "clawtune.host-kb.prediction.v1",
  "seed_sha256": "...",
  "mapping_policy_sha256": "...",
  "scope_generation": 7,
  "view_revision": "opaque-host-read-version",
  "prediction_receipt": "opaque authenticated value",
  "prediction": {},
  "model_predictions": {},
  "fallback": null,
  "service_time_ns": 0
}
```

The response should contain the normal ClawTune `ToolPrediction` information
needed by the plugin and the canonical selected admission signal.
`scope_generation` reports the writable scope's committed counter;
`view_revision` identifies the complete prediction read set. Clients echo the
receipt and must not interpret either value as a client-selected scope.

### 8.2 Prediction receipt

The plugin calls `decide()` before `instrumentExecParams()` generates the
ClawBox `execution_id`. Therefore the prediction receipt cannot initially bind
an execution ID unless the plugin ordering is substantially changed.

The simplest design is:

- bind the receipt to pair binding, event/tool-call identity, command/context
  digest, view revision, selected prediction, and session lifetime;
- put the receipt in the SSH execution envelope;
- when `/v1/tool/admit` receives the new execution ID, atomically claim that
  receipt for the execution ID;
- reject receipt reuse for a second execution side effect;
- retain normal idempotency for retries of the same admission request.

Do not trust a prediction body supplied by the VM when a valid receipt exists.
The host should resolve the canonical prediction associated with the receipt.
Ordinary overlay advancement does not invalidate an already issued receipt:
it records the prediction actually used for that call. A changed binding,
command/context, or expired session does invalidate it. Retrying prediction for
the same call must not create duplicate pending call-feature bindings.

### 8.3 `POST /v1/kb/observe`

Sent directly by the Tool bridge using the same pair binding.

Minimum identity fields:

- schema version;
- session ID witness;
- execution ID;
- command SHA-256;
- tool name;
- claimed prediction/admission correlation;
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
- qualified PMU summary when available;
- resource outcome/censoring evidence and marked-imputation provenance.

Every metric needs units, source/measurement identity, and eligibility. Retain
clause index, argv/features, structural flags, and censoring/timeout evidence
needed by current ingestion; "compact" must not discard learning semantics.
Validate the observation against the admitted execution and canonical command,
not just a session token. Ineligible targets remain absent with a reason;
valid measured zero is distinct from missing data. A valid execution with no
eligible targets may commit with zero learning rows and an explicit reason.

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

Use this observation identity:

```text
(pair binding, execution_id, command_sha256)
```

An identical duplicate returns the already committed generation. Enforce
uniqueness of `(pair binding, execution_id)` and compare a canonical learning
payload digest, including the command digest. Reuse with a different command
or measurement content is a validation failure, not a new composite-key row.
Exclude transport timing/retry counters from that digest. Persist the decision
with the observation so recovery retains deduplication.

### 8.5 Future client compatibility

Expose supported protocol versions in health/preflight. Freeze request bounds,
required fields, error codes, timeout units, and exact digest canonicalization
before the image refresh. VM clients must accept additive optional response
fields and treat receipts/view revisions as opaque. Reject unsupported major
versions before execution. Sharing-policy changes within this contract require
host changes only; new required measurements or an incompatible wire protocol
can still require a later client/image update. Do not promise compatibility
with arbitrary future features or add legacy protocol fallbacks now.

## 9. Causal ordering

Do not use guest wall clocks as the authority across Runtime and Tool VMs.
Their clocks may differ.

Assign causal order on the host:

- prediction receives a host sequence/generation witness;
- a Tool observation becomes visible at its durable host commit/publication;
- the next prediction for that KnowledgeView sees all earlier acknowledged
  observations;
- uncompleted/uncommitted observations remain invisible;
- a lost acknowledgement may follow a successful commit. An identical retry
  resolves that outcome without learning twice; lack of a response does not
  prove lack of a commit.

Retain guest timestamps as measurement evidence, but normalize KB visibility
using host receive/commit order. Existing ClawTune classes currently use
`ts_end < query.ts_start`; the host adapter may assign monotonic host timestamps
or introduce an explicit sequence abstraction, but the result must preserve
the same strictly-prior rule.

Prepare the updated view before publishing/acknowledging it, or make the next
query wait for that committed generation's preparation. Background work must
not return a stale prediction after a successful acknowledgement. Keep real
durations and timeout bounds separate from causal sequence values. On restart,
restore the persisted ordering rather than comparing old guest/host monotonic
clock epochs. Capture a coherent read version for each prediction.

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

Current code already has `LatticeTimeKB.fork_for_update()`, `prepare()`, and
`tool_time/incremental.py::IncrementalLatticeState`. Ordinary append updates can
reuse an existing prepared prefix and extend it; full construction remains a
reference/initialization path. Do not describe every update as a full rebuild
or replace this maintenance machinery without evidence. It is not yet a
pair-overlay implementation, and its retained maps/caches need a memory audit.

Implement:

- one frozen prepared seed lattice;
- one small delta observation log/prepared delta per writable scope;
- an exact view over base evidence plus permitted delta evidence;
- background preparation only for the changed scope;
- no global rebuild when one pair learns;
- copy-on-write publication of a scope's prepared delta.

The combination must reproduce a conventional full KB initialized from the
seed and fed the same observations. Add differential tests comparing all
returned target distributions and selected contexts.

Concatenating two independently prepared prediction distributions is not enough.
New observations can change signature groups, LOSO risk, global priors, parent
shrinkage, node creation/order, and context selection. Reuse unchanged immutable
evidence and update/derive affected statistics against the combined view.
Scope any memoization that depends on those statistics. Keep the seed's optional
feature bound (currently 6); inefficient maintenance is not evidence that the
algorithm requires a smaller bound.

### 10.4 Removed model: EdgeKappa

EdgeKappa was removed in ClawTune `e14b21a`, including runtime code, schemas,
seeds, and tooling. It is outside this redesign. Do not implement its overlay,
restore its fields, or add compatibility for old EdgeKappa snapshots. Use the
current validated seed format and current prediction backends.

### 10.5 Command context and call identity

Execution-context KB, duration history, and its snapshot contract were removed
upstream in `0d7fccf`. Do not restore them. Preserve the remaining command-parser
context, prediction-time feature bindings, outcome/censoring metadata, and
execution correlation. Host code must not resolve guest paths on its filesystem.
No execution-context snapshot or history projection belongs in the host protocol.

### 10.6 Command-rule cache

Bundled parser rules are code/read-only model assets and may be shared.
Dynamically learned rules are knowledge and must be scoped. For the current
replay path, keep dynamic generation disabled unless it is demonstrated to
affect predictions used by the experiment.

## 11. Host persistence

Do not use one existing full `StateStore` per pair. It is a one-writer full
snapshot store which retains current and previous complete generations.

Use the existing experiment artifact root, not a second independent KB state
root. The supervisor already places each worker under
`<run-output>/attempts/<attempt_id>/<arm_id>/`. Suggested layout:

```text
<run-output>/attempts/<attempt_id>/<arm_id>/kb/
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
- arm ID and stable session-to-scope bindings;
- seed path and digest;
- ClawTune revision;
- mapping-policy digest;
- memory measurement semantics;
- protocol version;
- parser/model configuration and compact observation-projection version.

Each scope stores only compact accepted learning records and small overlay
state. It must not duplicate the cold seed or raw telemetry.

Persistence rules:

- append/commit an accepted observation before acknowledging it;
- publish one recoverable transaction for all of that execution's eligible
  ToolKB/TrieKB/Lattice/context updates and its deduplication decision;
- increment generation once per accepted Tool execution, not once per clause;
- recover a same-attempt scope by replaying/checkpointing its compact overlay;
- never adopt a different attempt's scope into the selected attempt;
- retain a summary with counts, generation, digests, failures, and sizes;
- do not put secret capabilities in result artifacts or logs.

Use one durable journal commit as the recovery authority; overlay checkpoints
are derived acceleration with an applied journal sequence. Recover committed
rows once, tolerate only an incomplete final record, and reject inconsistent
seed/policy/model identities with an actionable remedy. A successful response
must meet the promised reboot durability (flush/fsync before acknowledgement).
Retain execution/admission reconciliation needed to avoid duplicate side
effects; receipt idempotency alone is not exactly-once command execution.
Reissue capabilities on worker recreation and reject old bindings; reconnect
surviving/restored clients before allowing calls. Do not bake capabilities into
reusable images/templates or public run evidence. A private session checkpoint
may contain its old capability and must follow the same rebinding rule.

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
- prediction cache keys also include the complete view revision, command and
  parser/context inputs, measurement contract, and model configuration; keep
  admission calibration identity separate. Start without a prediction cache if
  its complete dependency set has not been established;
- no separate `ColdStartPredictionProvider` query is used for that command.

Frozen mode may remain as an explicit research baseline. It is not live
learning.

Distinguish normal model/evidence selection within a live view from fallback
after a failed prediction request. Preserve the existing supported selection
and calibration rules; a transport/model failure must not silently switch to
a separate frozen provider.

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

After bounded retries of the observation POST (never of the command), continue
that trace from the latest committed host view. Its online-learning validity
stays degraded for the attempt even if later updates succeed. A lost response
may hide a successful commit: reconcile by execution ID/payload digest, then
use the actual committed state, rather than rolling back or assuming no update.
Keep command success separate from learning validity in the result schema and
reporting denominators. A later required prediction failure still follows
section 15.1; degradation does not authorize prediction bypass.

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
- Reuse the configured experiment artifact root for attempt/arm KB state.
- Expose bounded KB HTTP deadline/retry settings with units and defaults chosen
  in phase 1, and record them in the frozen specification. Retries apply only
  to idempotent requests, never Tool execution.
- Reuse `CLAWBOX_CONTROL_HOST` and the current experiment control port.

The frozen experiment specification must record:

- seed path/digest;
- scope policy and its digest;
- host-live protocol version;
- measurement semantics;
- retention setting;
- model/parser configuration, observation-projection version, and failure/retry policy.

Future sharing policies may add host-side resolver configuration. They must not
require new fields in VM messages.

Document plain same-attempt resume and explicit new-attempt behavior. For each
public field state its default, units, prerequisites, and whether a change needs
host apply, a new run ID, seed retraining, or image rebuild. Seed/model/policy/
measurement changes require a new run; a different seed does not inherently
require retraining or a VM rebuild once host-live clients are installed.

Update:

- `docs/guide.md`
- `docs/reference.md`
- `docs/installation.md` for host prerequisites and changed recovery commands
- `docs/results.md` for KB overlay artifacts and degraded-trace interpretation

Do not put debugging history or Kunpeng-specific validation narratives in user
documentation.

## 17. Likely code locations

### ClawTune

- `contracts/`: add host-KB protocol schemas.
- `services/sidecar/src/clawtune_kb/`: shared base and overlay store/view.
- `services/sidecar/src/tool_resource/runtime_kb.py`: layered ToolKB/TrieKB
  primitives or narrow reusable base/overlay APIs.
- `services/sidecar/src/tool_time/lattice_kb.py` and `tool_time/incremental.py`:
  layered lattice view using existing incremental maintenance where applicable.
- `services/sidecar/src/clawtune_sidecar/identity.py`: stable call identity.
- `services/sidecar/src/clawtune_kb/`: compact resource projection and marked-imputation provenance.
- `services/sidecar/src/command_parser/service.py`: scoped call-feature bindings.
- `services/sidecar/src/clawtune_sidecar/api/dependencies.py`: select remote
  predictor without creating local state.
- `services/sidecar/src/clawtune_sidecar/api/app.py`: remote predictor lifecycle
  and health/provenance.
- `services/sidecar/src/clawtune_sidecar/config.py`: remote endpoint/auth mode.
- `services/sidecar/src/tool_resource/telemetry.py`: no-write finalization.
- `tools/guest_collector_server.py`: return compact observations in memory.
- `packages/clawtune-plugin/src/contracts.ts`: prediction receipt field.
- `packages/clawtune-plugin/src/index.ts`: context forwarding and fail-closed
  host-live behavior even when ordinary plugin operation uses observe mode.
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
- `clawbox/cli.py` and `clawbox/experiments/supervisor.py`: default same-attempt
  resume, reopenable artifact paths, and progress/execution reconciliation.
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
5. Cover existing command context, task identity, and compact outcome evidence;
   settle request limits/timeouts and additive response compatibility before
   freezing the VM contract. Preserve existing admission selection/calibration
   semantics while changing the source of learned evidence.

### Phase 2: shared base and overlays

1. Load/validate the seed once.
2. Implement ToolKB and TrieKB overlays.
3. Implement LatticeKB delta/query combination.
4. Implement scoped command-feature bindings and current label policy.
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
6. Update CLI/supervisor resume semantics and preserve prior invocation evidence;
   verify that recovery does not replay completed side effects into retained KBs.

### Phase 5: Runtime remote predictor

1. Add remote mode to the local sidecar.
2. Do not initialize local writable state in that mode.
3. Preserve local trace and LLM proxy behavior.
4. Put the receipt into the plugin/SSH envelope.
5. Verify prediction failure blocks before SSH execution, including the current
   observe-mode exception path.

### Phase 6: Tool compact observation

1. Add no-file collector finalization.
2. Return compact qualified observations to the Go bridge.
3. POST synchronously through a persistent client.
4. Preserve the original command result.
5. Add explicit debug artifact retention, default off.
6. Bound observation retries and propagate degraded-learning status while the
   same trace continues from committed state.

### Phase 7: configuration, results, and documentation

1. Add public fields and validation.
2. Record seed/mapping/protocol provenance.
3. Add KB summary artifacts and degraded-trace classification.
4. Update guide/reference/results/installation as required.
5. Validate fresh-shell CLI help and configuration errors locally.
6. Document install, reuse, reboot/interruption recovery, repeat execution,
   and cleanup through the existing public CLI. Exercise installation in an
   isolated environment; record blocked checks in verification evidence.

### Phase 8: real validation and one planned refresh

1. Run one real Kunpeng pair using overlay code shipped from the checkout where
   possible.
2. Run two pairs and prove isolation.
3. Validate checkpoint/restore and same-attempt recovery.
4. Freeze the protocol and code.
5. Rebuild the Runtime image/template once.
6. Refresh the Tool image and selected Tool template inventory once
   (previously recorded as 503 templates).
7. Verify image/kernel/revision identity and sample several templates.
8. Run one final Full-1280 with default logging.

Treat 503 as the previously recorded inventory, not a live count verified by
this update. Discover the selected template inventory and saved identities
before refreshing it. Reuse existing isolated installation/CLI evidence when
the changed path is covered; run only checks needed for the changed behavior.
The one-refresh rule avoids repeated speculative builds; it does not justify
keeping a protocol or image defect that changes experiment results.

Do not perform repeated 503 refreshes while fixing local test failures.

## 19. Required tests

### 19.1 ClawTune unit tests

- Empty overlays produce the same predictions as the seed.
- Pair A update changes A's prediction.
- The same query for pair B remains unchanged.
- Layered ToolKB output matches a full-copy reference.
- Layered TrieKB output matches a full-copy reference.
- Layered LatticeKB distributions and contexts match a full-copy reference.
- Command-feature bindings are isolated; compact resource projection and
  marked-imputation behavior match current ClawTune semantics.
- Same-scope updates become visible at commit; every prediction after a
  successful acknowledgement sees that generation or a later committed one.
- No-write collector finalization returns a valid compact payload and creates
  no clause telemetry file.
- Diagnostic retention still creates and validates the artifact.
- JSON Schema validation covers valid, malformed, oversized, and mismatched
  identity payloads.
- Layered Lattice parity covers changed signature groups, global priors, parent
  shrinkage, newly introduced nodes, and query context selection, not just
  concatenated samples.

### 19.2 ClawBox Python tests

- Reserve pair before VM creation; attach callbacks later.
- Capability cannot route to another session/scope.
- Prediction receipt binds command digest and generation.
- Receipt is claim-once for execution side effects and idempotent for exact
  admission retries.
- Host-live admission never calls the frozen host provider.
- Same run/attempt/session reopens the same persisted generation.
- Plain resume selects the saved attempt; an explicitly different attempt
  starts cold. Reopening an existing arm directory preserves earlier evidence.
- New attempt starts at generation zero over the seed.
- Prediction failure fails one trace before SSH execution and does not cancel
  peer traces.
- Observation failure after execution preserves the actual command result and
  marks KB update failure; a subsequent call in that trace uses committed state.
- Result provenance contains seed/mapping/protocol identities but no secrets.
- Recovery rebuilds overlay state from committed compact records.
- Commit followed by a lost acknowledgement remains exactly one update across
  retry/restart; conflicting payload reuse is rejected.
- View revisions cover every readable overlay without exposing scope choices
  to VM clients; cache tests include command-context and feature-binding changes.

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
  SSH, including observe-mode catches and missing/invalid receipt admission.
- Local trace and LLM proxy still work without a local KB.
- Prediction/observation context has equivalent model inputs after transport;
  Runtime after-tool handling does not duplicate resource learning.

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
- Exercise plain resume and explicit new-attempt resume through the CLI;
  verify invocation logs/results are preserved and completed calls are not
  replayed as new training observations.
- Inject a lost observation acknowledgement and an exhausted update retry;
  verify deduplication, continued execution, and persistent degraded validity.
- Verify reboot recovery with persisted causal ordering and fresh credentials.

Record real VM tests, isolated installation, unit tests, and syntax/help checks
separately. Existing-host success is not proof of fresh installation. If a
documented command cannot run, record the command and prerequisite/blocker in
verification evidence (and ClawTune's `docs/CURRENT_PLAN.md` when working there).

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
- preview any removal separately and apply only explicitly selected obsolete
  artifacts; protect recent/growing crash dumps and unrelated storage;
- leave enough space for the verified template inventory and Cube's 85% snapshot
  rejection threshold;
- start with a few templates and watch them before continuing;
- use the maintained image/template commands for the long refresh after the
  initial templates are clean; keep background execution and logs reproducible
  through the documented workflow.

## 23. Immediate next action for the implementing agent

1. Recheck both git worktrees and read applicable `AGENTS.md` files.
2. Preserve the tracked failure-debug handoff and all unrelated work. Reconnect
   to Kunpeng and inspect the actual checkout/deployment before any mutation.
3. Write the protocol schemas and reference full-copy differential tests first,
   including command/outcome context and receipt/view-version semantics.
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

## 24. Keeping this handoff usable for later work

At each implementation milestone, update section 1 with the exact ClawBox and
ClawTune revision pair, phases actually completed, remaining blockers, and paths
to separate verification artifacts. Keep requirements distinct from implemented
behavior and measured results. Replace stale instructions rather than appending
contradictory alternatives; retain only unresolved design choices here.

No user decision remains pending from this review. Same-attempt default resume
and continued execution after observation failure are confirmed in section 2.
Future sharing/weighting policies are deliberately deferred; only their host-side
extension boundary is required now. If implementation would change a model's
learning semantics or lose an input, explain that consequence and ask before
making the change.

Validation of this handoff update: local source/revision/seed inspection and
document consistency checks only. No implementation tests, VM runs, installation,
rebuilds, or performance measurements were performed. Remote verification was
blocked by the SSH timeout recorded in section 1.
