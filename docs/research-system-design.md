# ClawBox research system design

## Experimental unit

One logical coding agent is a pair of CubeSandbox MicroVMs:

```text
                         model HTTP
               +----------------------------+
               |                            v
+-----------------------------+      +------------------+
| Runtime VM                  |      | ModelGateway     |
| OpenClaw                    |      | replay or API    |
| ClawTune plugin + sidecar   |      +------------------+
| agent state and memory      |
| web search/fetch            | metadata  +------------------+
|                             +---------->| PolicyControl    |
| native SSH client/hook      |<----------+ FIFO admission   |
+---------------+-------------+  ADMIT +  +------------------+
                | command/stdout/stderr     |
                | native SSH                | Cube lifecycle
                v                           v
       +---------------------+       +------------------+
       | Tool VM             |       | CubeSandbox     |
       | mutable workspace   |       | create/pause/   |
       | process/file tools  |       | restore/destroy |
       | cgroup + eBPF       |       +------------------+
       +---------------------+
```

Runtime and Tool have different experimental roles. Runtime state persists for
the Agent's lifetime. Tool residency is the manipulated resource: resident
baselines retain it, while reclamation baselines checkpoint and pause it during
predicted idle windows and restore it before the next Tool operation.

## Tool placement

| Class | Operations | Location | Tool admission |
|---|---|---|---|
| Workspace/process | `exec`, `process`, `read`, `write`, `edit`, `apply_patch` | Tool VM over native SSH | Yes, once per execution ID |
| Retrieval | `web_search`, `web_fetch` | OpenClaw process in Runtime VM | No |
| Agent memory | `memory_search`, `memory_get` | OpenClaw process in Runtime VM | No |
| Model request | OpenAI-compatible chat completion | Runtime to ModelGateway | Drives idle/residency policy, not Tool admission |

ClawTune instruments the six Tool-VM operations and creates the execution
envelope. Runtime-local operations may have ordinary OpenClaw logs but do not
create Tool reservations or wake a paused Tool.

## Admission protocol

For every Tool-VM operation, ClawTune assigns a stable `execution_id` before
OpenClaw invokes SSH. The Runtime SSH hook sends `(session_id, execution_id,
operation, command_sha256, prediction)` to PolicyControl and blocks.

PolicyControl restores Tool if necessary and enters the requested memory amount
into a process-wide FIFO reservation queue. It returns only after the request
is at the head of the queue and the physical/committed memory constraints are
satisfied. Independent HTTP requests are serviced concurrently; the queue lock
is not held during checkpoint or restore.

The response contains `ADMIT` and Tool's current semantic SSH target. This is
necessary because restoring a CubeSandbox may change its mapped port after
OpenClaw constructed the pending SSH command. The hook rewrites that invocation
and launches real OpenSSH exactly once. Completion releases the reservation and
is idempotent. A missing completion fails the experiment rather than retrying a
possibly completed command.

```text
OpenClaw       SSH hook       PolicyControl       CubeSandbox       Tool
   | command      |                |                   |              |
   |------------->| admit(id)      |                   |              |
   |              |--------------->| restore if paused |              |
   |              |                |------------------>|              |
   |              |                | endpoint(2222)    |              |
   |              |                |<------------------|              |
   |              | ADMIT + route  |                   |              |
   |              |<---------------|                   |              |
   |              | rewrite current argv; SSH once ----------------->|
   |              |<---------------- stdout/stderr -------------------|
   |              | complete(id)   |                   |              |
   |              |--------------->| release           |              |
```

## Checkpoint and network invariants

Tool lifecycle is `NEW -> CREATING -> RUNNING -> CHECKPOINTING -> SWAPPED ->
RESTORING -> RUNNING -> DESTROYING -> CLOSED`. Calls are serialized per Tool.
Restore reconnects by stable sandbox ID and then resolves port 2222 again.

Runtime network policy is created after Tool's first endpoint is known. The
semantic endpoint host and the host running PolicyControl/ModelGateway are
allowed. The mapped Tool port may change after restore; the Runtime hook handles
that. A changed endpoint host is rejected because CubeSandbox cannot mutate an
existing Runtime VM's allowlist. Deployment validation therefore treats stable
endpoint host identity as part of the semantic endpoint contract.

## Model modes

Both formal modes run the same OpenClaw binary, plugin, sidecar, prompt, native
SSH tools, admission path, and validation inside Runtime:

- `api`: ModelGateway forwards the request to a configured upstream and records
  the response plus pure model latency.
- `replay`: ModelGateway checks the canonical request, waits for recorded model
  latency, and returns the recorded OpenAI-compatible response from a
  session-local cursor.

An API result exports a replay trace. Replaying it must consume every entry once,
match every canonical input, deliver every response, and produce the same
validated workspace result. The lightweight `replay_engine` driver bypasses
OpenClaw and is retained only for systems-capacity exploration.

## Measurement model

ClawBox emits ordered JSONL with wall-clock and monotonic nanosecond timestamps.
The result bundle retains:

- Runtime and Tool create/destroy service time;
- Tool checkpoint, restore, and endpoint/known-host refresh time;
- model request start, generation, policy release, and Runtime delivery;
- FIFO queue depth and wait distribution;
- Runtime-to-PolicyControl admission round trip;
- estimated control overhead after queue and restore time are subtracted;
- Tool execution latency and command result;
- validation, output hashing, cleanup, and stabilization boundaries;
- node physical memory samples and memory-time integral;
- ClawTune, Tool bridge, cgroup-v2, and eBPF records joined by exact execution ID.

Formal acceptance requires exact-ID join rate 1.0, no duplicate Tool execution,
no wrong-session routing, no telemetry loss, no replay divergence, successful
validation, and zero owned sandbox leaks.

## Evaluation progression

Run route and pair gates at c1/c4, managed API and exported-trace replay at c1,
correctness/HOL/race gates at c4/c8, policy pilots at c20, and formal replay at
c40/c60. Report lightweight replay-engine results separately from formal
OpenClaw results.
