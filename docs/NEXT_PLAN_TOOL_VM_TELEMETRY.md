# Next Implementation Plan: Tool-VM Telemetry Before Managed Sandbox Completion

**Status:** proposed implementation plan
**Date:** 2026-08-20
**Primary repositories:** `ClawBox` and sibling `ClawTune`
**Priority decision:** repair and prove the telemetry-to-prediction loop before
continuing the managed sandbox control-plane milestone.

## 1. Decision

The next program of work is to make real per-execution cgroup-v2 and eBPF
telemetry available inside the Firecracker Tool VM, preserve ClawTune's native
telemetry and knowledge-base semantics, and prove that measurements from one
run affect a later prediction.

Managed API lifecycle convergence remains necessary, but it is the next
milestone after telemetry. Completing run status, cancellation, retry, and CRD
version convergence first would produce a better manager around a sandbox whose
most important measurement feature is still unavailable.

## 2. Architectural Facts Established from the Code

ClawBox has two different Firecracker microVMs in one Cell:

```text
physical Kubernetes node
|-- Runtime microVM kernel
|   `-- OpenClaw + ClawTune plugin/sidecar
|       `-- SSH client for tool execution
`-- Tool microVM kernel
    `-- tool-bridge
        `-- shell and actual tool process tree
```

The physical host sees a Firecracker/Kata VM process and virtual I/O. The
Runtime VM sees the SSH client. Only the Tool VM guest kernel sees the real
`execve`, process, cgroup, CPU, memory, I/O, and network activity of tool
commands. Therefore:

1. Per-command eBPF programs must execute in the **Tool VM guest kernel**.
2. The loader may run inside the privileged Tool container because that
   container uses the guest kernel; it does not have to be a guest-OS system
   daemon.
3. Host-node or Runtime-VM eBPF may provide VM-level diagnostics, but it cannot
   be the source of per-command attribution.

The current ClawBox collector is not eBPF. `toolbridge/collector.go` creates a
best-effort execution cgroup and reads cgroup-v2 files, with a `/proc` sampler
as fallback. It always emits `null` network counters. The Runtime sidecar also
sets eBPF-required false and disables its local cgroup path, correctly avoiding
false attribution to the SSH client, but leaving Tool-VM eBPF absent.

## 3. Current Reuse of ClawTune

Reuse is partial:

- `docker/Dockerfile.runtime` copies and installs the sibling ClawTune sidecar
  and plugin into the Runtime image.
- The Tool image receives only the ClawBox Go tool bridge. It does not contain
  ClawTune's BCC collector or its KB implementation.
- `clawbox/tuning/clawtune.py` reproduces part of ClawTune's
  `runtime_tool_resource_kb_v1` serialization instead of importing the native
  implementation.
- The control-plane KB publishes `runtime-tool-resource-kb.json`, but not the
  native eBPF-derived `clause-resource-kb.json` used by ClawTune's
  `ClauseResourceKB`.

ClawTune already exposes the important adaptation seam. Its historically named
`DockerExecutionContext` accepts either a Docker container ID or an explicit
cgroup-v2 path plus a trusted root PID. `ClauseTelemetryCollector` accepts the
same explicit scope. This should be generalized additively and reused in the
Tool VM rather than reimplementing its mature event parsing, loss accounting,
artifact validation, KB eligibility, and causal prediction logic in ClawBox.

## 4. Goals and Non-Goals

### Goals

- Produce genuine ClawTune eBPF clause telemetry from the Tool VM guest kernel.
- Produce independent whole-command cgroup-v2 accounting for CPU, memory, and
  disk I/O.
- Make every fallback, loss, scope, and collector failure explicit.
- Preserve native ClawTune artifact and KB formats as protocol sources of
  truth.
- Keep observations isolated by tenant and repository in the ClawBox control
  plane.
- Prove an inter-run feedback loop: run A observes, the KB advances, and run B
  loads and reports a prediction from that generation.
- Keep standalone ClawTune's Docker/host behavior working without regression.

### Non-goals for this milestone

- Do not make learned predictions control Cell admission or VM sizing yet.
- Do not claim that per-command prediction can directly select the initial VM
  size. The Cell is created before the agent has generated its commands.
- Do not require within-run online learning in the first integration. ClawBox's
  current artifact transfer happens at task finalization; inter-run learning is
  the first acceptance target.
- Do not finish the Managed API lifecycle projector, cancellation, retry, or
  CRD migration until the telemetry gates below pass.
- Do not replace ClawTune's BCC implementation preemptively with a new Go BPF
  implementation.

## 5. Required Invariants

1. **Kernel locality:** an observation is eligible only if its collector ran
   against the Tool VM guest kernel.
2. **Exclusive scope:** the execution uses a non-root, per-execution cgroup.
   The Tool Pod or guest root cgroup is never accepted as command attribution.
3. **Observe-before-exec:** telemetry is armed and the cgroup scope is
   registered before the requested shell command is allowed to `execve`.
4. **Two independent artifacts:** ClawTune eBPF clause telemetry and
   `cgroup_resource_v1` remain separate measurement sources. Neither artifact
   is described as containing the other.
5. **Fail closed for training:** unavailable, lossy, incomplete, mismatched, or
   degraded telemetry cannot train the active KB.
6. **Fail open for tool execution during the initial shadow phase:** telemetry
   failure is recorded but does not destroy the agent task. A dedicated strict
   validation mode may fail the telemetry acceptance gate.
7. **Native contracts:** ClawTune JSON Schemas and native artifact validators
   remain authoritative. ClawBox adds identity, signing, persistence, and
   tenancy around those contracts.
8. **No standalone regression:** every ClawTune change is additive or
   compatibility-preserving and passes its original Docker/host test suite.
9. **Pinned compatibility:** every ClawBox image records the exact ClawTune
   revision used to build the collector, predictor, and KB projector.

## 6. Target Data Flow

```text
ClawTune prediction snapshot in Runtime VM
                 │
                 ▼
OpenClaw produces an exec command and execution_id
                 │ SSH envelope
                 ▼
Tool bridge in Tool VM
  1. create per-execution cgroup
  2. start a blocked child/helper
  3. move child into the cgroup
  4. register cgroup + trusted PID with guest ClawTune collector
  5. begin the native command observation
  6. release child to exec the requested shell
  7. wait, finalize, validate, and persist artifacts
                 │
                 ▼
Runtime finalization retrieves immutable Tool-VM artifacts
                 │ signed batch / digest
                 ▼
ClawBox tenant × repo telemetry store
  ├── raw native eBPF artifacts
  ├── independent cgroup resource artifacts
  └── native ClawTune KB snapshots
                 │
                 ▼
next Runtime VM pulls generation N and records prediction provenance
```

## 7. Cross-Repository Ownership

### ClawTune owns

- eBPF program source, BCC or future CO-RE backend, attach lifecycle, maps, and
  event loss accounting;
- command/clause parsing and native telemetry artifact validation;
- `ClauseResourceKB`, `RuntimeToolResourceKB`, prediction semantics, snapshot
  serialization, and schema compatibility;
- a generic explicit-cgroup execution adapter usable outside Docker;
- a real semantic telemetry self-test.

The public name `DockerExecutionContext` may remain as a compatibility alias,
but new code should use a generic name such as `ExecutionScope` or
`CgroupExecutionContext`. Existing Docker callers must not change behavior.

### ClawBox owns

- Tool-VM image assembly and guest capability/security configuration;
- SSH execution envelope, execution IDs, command lifecycle, and the
  observe-before-exec gate;
- creation and cleanup of exclusive per-execution cgroups;
- local RPC between the Go bridge and the guest ClawTune collector;
- artifact extraction from the Tool VM and durable upload;
- tenant/run/attempt identity, HMAC signing, deduplication, persistence, and
  snapshot distribution;
- Cell-level capacity and eventual task-level profile selection.

### Shared contract rule

ClawBox must consume native ClawTune schemas and validators from a pinned
ClawTune revision. It should not add another hand-maintained copy of a KB or
telemetry schema. Cross-repository compatibility tests must load artifacts and
snapshots produced by the exact ClawTune package embedded in the images.

## 8. Phased Work Plan

### T0 — Make the existing contract honest

**Status (2026-08-20): completed.** The Tool Bridge now reports component
sources, fallback/error provenance, and sampling coverage; procfs fallback is
degraded (or invalid with no observed process) and its quality overrides the
span during the exact execution join. Runtime flush logs source/quality
counters. The additive fields are accepted by ClawTune's native
`CgroupResourceResult`. Validation: 167 ClawBox Python tests, collector-only Go
tests on Linux/arm64, and 6 ClawTune compatibility tests.

**Purpose:** stop treating cgroup-only or `/proc` data as eBPF evidence while
the real collector is developed.

Work:

- Correct ClawBox comments, field descriptions, tests, and capability reports
  that currently call `cgroup_resource_v1` an eBPF artifact.
- Extend the independent resource artifact with additive provenance fields, if
  accepted by the ClawTune schema:
  - CPU, memory, disk, and network source independently;
  - fallback used;
  - cgroup setup and read errors;
  - sample count and coverage window.
- Mark `/proc` fallback `degraded` when it can miss short-lived work or has
  insufficient samples. Never mark a zero-evidence fallback `valid`.
- Add collector-source counters to live diagnostics and evidence scripts.

Acceptance:

- No test can claim eBPF by constructing arbitrary JSON with fake network
  counters.
- A cgroup-only run is reported as cgroup-only.
- A fallback run is observable and cannot enter the trusted KB unless its
  explicitly defined quality gate passes.

### T1 — Prove the native ClawTune collector inside a Kata Tool VM

**Purpose:** answer the feasibility question before committing to packaging or
a new backend.

Work in ClawTune:

- Add an additive explicit-cgroup semantic smoke entry point around the native
  `ClauseTelemetryCollector`/SDK.
- Accept an explicit cgroup path, trusted root PID, command, artifact path, and
  repository identity without requiring Docker discovery.
- Keep the existing Docker path and all public schemas unchanged.
- Emit detailed preflight results for BCC import, kernel compilation, kprobe
  attachment, perf-event attachment, cgroup identity, ring-buffer polling,
  event loss, and cleanup.

Work in ClawBox:

- Build a dedicated research Tool image containing the exact sibling ClawTune
  revision and the BCC/compiler dependencies required by the Kata guest
  kernel.
- Extend `probe-kata-guest.sh` so it loads and executes a real BPF program. BTF,
  sysctls, capabilities, and file existence alone are not sufficient.
- Run the semantic smoke under the same RuntimeClass, seccomp profile,
  capabilities, architecture, and guest kernel as production Tool cells.

Real-machine acceptance gate:

- BCC imports from the collector's actual interpreter.
- BPF compilation and every required attach operation succeed.
- A known gated child produces at least one correctly scoped exec event.
- CPU/RSS clause measurements are nonzero for a controlled workload.
- The artifact reports active collector state, zero loss, valid integrity,
  successful cleanup, and at least one `eligible_for_kb` call.
- A concurrent unrelated guest process is absent from the artifact.

Decision after T1:

- If BCC is reliable and its dependencies can be reproduced in the supported
  Tool images, retain the native backend.
- If BCC depends on unavailable guest headers or is operationally fragile, add
  a **new backend inside ClawTune** behind the same collector/artifact interface,
  preferably CO-RE. Keep BCC as the standalone default until parity tests prove
  the new backend. Do not create a ClawBox-only telemetry fork.

### T2 — Integrate the collector with the Tool bridge

**Purpose:** make telemetry part of every real SSH tool execution without an
exec-boundary race.

Work:

- Package a long-lived ClawTune guest collector helper in the Tool image.
- Start it under the existing single-container Tool Pod model.
- Add a versioned, authenticated local Unix-socket protocol between the Go
  bridge and collector helper. At minimum it needs health, begin, finish, abort,
  and shutdown operations.
- Replace start-then-move with a gated execution sequence:
  1. create the cgroup;
  2. start a helper child blocked on an inherited pipe/event;
  3. move and verify the child in the cgroup;
  4. register the scope and begin observation;
  5. release the child to `execve` `/bin/sh -lc ...`;
  6. finalize telemetry before cgroup cleanup.
- Continue producing the independent cgroup counter artifact from the same
  execution cgroup.
- Bound collector memory, event buffers, artifact size, and command count.
- Ensure timeout, cancellation, bridge crash, and collector crash all clean up
  leases and cgroups idempotently.

Acceptance:

- The initial shell exec and descendant commands are observed.
- Two concurrent executions have distinct cgroups and no cross-attributed
  clauses.
- Quoted commands, pipelines, subshells, timeouts, and rapid commands retain
  the same SSH behavior and exit codes as before integration.
- Collector failure cannot hang or corrupt the SSH channel.
- Native ClawTune artifact validation, not a ClawBox approximation, determines
  KB eligibility.

### T3 — Preserve and ingest native telemetry

**Purpose:** replace the simplified imitation with a native, auditable
multi-tenant data path.

Work:

- Retrieve both native clause eBPF artifacts and independent cgroup resource
  artifacts from the Tool VM during Runtime finalization.
- Sign an immutable manifest containing artifact digests plus tenant, run,
  attempt, cell, repository, execution, collector version, and ClawTune commit.
- Extend the tuning API/store to persist raw native artifacts before
  projection. Reject identity mismatch, invalid signature, incompatible schema,
  incomplete cleanup, loss, or ineligible calls.
- Install/import the pinned ClawTune KB package in the control-plane build.
- Build native `ClauseResourceKB` and `RuntimeToolResourceKB` snapshots using
  ClawTune code. Retire `clawbox/tuning/clawtune.py`'s duplicated builder only
  after byte/semantic parity and migration tests pass.
- Publish both `clause-resource-kb.json` and
  `runtime-tool-resource-kb.json` with generation and source digest metadata.
- Keep all queries and generations scoped by tenant and repository.

Acceptance:

- Replaying an artifact is idempotent.
- Tampered or cross-tenant artifacts are rejected.
- A snapshot round-trip through the control plane loads successfully using the
  pinned ClawTune `from_json_obj` implementation.
- The generation input digest is reproducible from the immutable artifact set.
- Rollback restores the previous pair of native snapshots atomically.

### T4 — Prove prediction feedback in shadow mode

**Purpose:** demonstrate usefulness before prediction controls resources.

Work:

- Pull both native snapshots before starting the Runtime ClawTune sidecar.
- Record, for every prediction, tenant, repository, KB generation, snapshot
  digest, match level, evidence count, predicted values, and actual values.
- Run controlled repeated commands and command-disjoint workloads.
- Evaluate cold-start, known-command, and fallback behavior against the fixed
  profile baseline.
- Keep `FixedProfileSizer` authoritative during this phase.

Real-machine acceptance gate:

1. Run A begins at generation 0 or N.
2. Run A produces valid Tool-VM eBPF and cgroup artifacts.
3. The KB accepts the observations and advances to generation N+1.
4. Run B pulls exactly generation N+1.
5. Run B records at least one native prediction whose evidence includes run A.
6. The prediction/actual report is archived with the exact ClawBox and
   ClawTune revisions.

Promotion beyond shadow mode requires predefined safety metrics: coverage,
p90 calibration, underprediction rate, over-allocation, loss rate, fallback
rate, and task success rate. A small successful demo is not sufficient.

### T5 — Design controlled resource action

Per-command and Cell-level decisions must remain distinct:

- ClawTune knows a command only after the Cell already exists. Its native
  prediction can advise per-command cgroup controls or command scheduling
  inside the Tool VM.
- Initial Firecracker VM sizing needs a separate task/repository-level model
  based on historical aggregates available at submission time.

First action should be advisory or bounded per-command control with hard floors,
ceilings, headroom, and immediate fixed-profile fallback. Learned Cell sizing
comes only after enough task-level evidence exists and must retain the atomic
two-VM reservation invariant.

### M1 — Resume managed sandbox convergence

After T4 passes, resume the earlier managed platform work:

- move the Cell controller and dispatcher to one native SandboxTask version;
- implement cancellation inside the Cell reconciler;
- add a status projector from SandboxTask status to managed Attempt and Run;
- populate durable artifact/result references;
- constrain retry to terminal retryable attempts;
- pass the full problem statement through the managed benchmark client;
- add an end-to-end managed lifecycle test and real-machine smoke.

This work remains important, but it must consume the proven telemetry and
artifact contracts produced by T0-T4 rather than inventing another path.

## 9. ClawTune Change Safety Policy

Changes in the sibling repository are allowed when necessary, subject to these
rules:

1. Prefer additive generic interfaces over modifying Docker semantics.
2. Keep existing JSON Schemas and artifact readers backward compatible.
3. Preserve `DockerExecutionContext` as an alias or supported API if a generic
   context replaces it.
4. Keep BCC behavior as the reference backend until a second backend passes
   artifact and semantic parity tests.
5. Run ClawTune contract validation, sidecar tests, plugin tests/typecheck, and
   existing Linux semantic checks in addition to new guest-mode tests.
6. Record every Linux-only command that cannot run locally in
   `ClawTune/docs/CURRENT_PLAN.md`, as required by that repository.
7. Never merge a ClawBox change that depends on an uncommitted sibling-tree
   state. Pin and record the ClawTune revision in image metadata and evidence.

## 10. Test Matrix

### Local/CI tests

- ClawTune native artifact and JSON Schema validation.
- Explicit-cgroup adapter unit tests with fake BPF only for failure logic.
- ClawTune Docker-path regression suite.
- Bridge protocol framing, authentication, timeout, cancellation, and crash
  recovery.
- Gated-child race tests on Linux.
- Native artifact ingestion, signing, identity, deduplication, and rollback.
- Snapshot compatibility tests that use ClawTune's actual readers.
- Existing ClawBox Python and Go tests.

Mocks may verify error handling, but cannot satisfy an eBPF availability gate.

### Real Kata/Firecracker tests

- actual BPF compile/load/attach and semantic exec capture;
- actual perf-event sampling and ring-buffer polling;
- exact cgroup ID match and unrelated-process exclusion;
- serial and concurrent execution isolation;
- short command, pipeline, fork tree, timeout, and forced cancellation;
- artifact retrieval and durable control-plane ingest;
- two-run KB generation and prediction proof;
- resource and Firecracker process leak checks after cleanup.

## 11. Operational Signals

At minimum expose and archive:

- collector backend and version;
- BPF attach state and failure reason;
- executions by cgroup, process fallback, and unavailable source;
- ring-buffer/event loss;
- cgroup creation/move/read/cleanup failures;
- native artifact accepted/rejected counts and reasons;
- KB generation, input digest, observation count, rollback count;
- snapshot pull generation/digest and pull/parse failures;
- predictions by match level and evidence count;
- prediction error, calibration, underprediction, and fallback rates.

The task result and managed API should eventually link to this evidence, but
telemetry availability must be independently inspectable before managed status
projection is complete.

## 12. First Concrete Deliverable

The first implementation deliverable is deliberately narrow:

> Run ClawTune's native clause eBPF collector inside a real Kata/Firecracker
> Tool VM against one gated command in an explicit per-execution cgroup, and
> archive a native version-2 artifact that passes ClawTune validation with zero
> loss and no unrelated process attribution.

This deliverable should modify ClawTune only to add a generic explicit-cgroup
adapter/self-test and should modify ClawBox only to assemble and invoke the
research Tool image and archive evidence. Production bridge integration starts
only after this gate passes.

## 13. Completion Definition for the Telemetry Program

T0-T4 are complete only when all of the following are true on the target
Kunpeng Kata/Firecracker machine:

- genuine guest-kernel eBPF telemetry is captured for real tool commands;
- independent cgroup-v2 counters are present and provenance is honest;
- concurrent commands are isolated without cross-attribution;
- invalid or degraded data cannot train the KB;
- the control plane stores native artifacts and builds native ClawTune
  snapshots using pinned ClawTune code;
- a later run demonstrably loads the new generation and reports a prediction
  based on earlier Tool-VM evidence;
- standalone ClawTune Docker/host behavior and tests remain intact;
- every result is tied to exact ClawBox, ClawTune, image, guest-kernel, and
  RuntimeClass revisions.

Only after this definition is met should managed sandbox convergence become the
primary implementation milestone again.
