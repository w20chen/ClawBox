# ClawBox research-system contract

ClawBox studies whether many concurrent LLM Agents can safely overcommit
Kunpeng physical memory by combining Agent execution context, command-specific
Tool demand prediction, memory admission, and reclamation during long model
waits. Running OpenClaw in a VM is necessary infrastructure, not the research
result.

## Managed architecture

Each logical Agent owns one long-lived Runtime CubeSandbox VM and one Tool
CubeSandbox VM. Runtime runs OpenClaw and model-side execution. Tool owns the
mutable workspace and executes every sandboxed operation through native SSH.
CubeSandbox is the only VM substrate; ClawBox is the policy and orchestration
layer above it. PolicyControl admits and records operations but never proxies
SSH commands or output.

The required loop is:

```text
Agent/OpenClaw
-> model wait or Tool request
-> observe Agent context
-> ClawTune command/resource prediction
-> synchronous physical-memory admission and reservation
-> CubeSandbox residency/restore decision
-> native SSH Tool execution
-> execution-scoped cgroup-v2 and eBPF observation
-> exact session_id + execution_id join
-> ClawTune KB feedback where training is permitted
-> next prediction and decision
```

Admission precedes both VM materialization and SSH execution. One accounting
mechanism must cover restore/materialization memory, predicted incremental Tool
memory, and safety headroom. The reservation remains held until the actual
remote operation has ended, the SSH process has been reaped, and completion
telemetry is ordered correctly.

The execution identity is attached at the lowest boundary that actually owns
the SSH subprocess. ClawTune attaches it directly to command-bearing `exec`.
For OpenClaw filesystem/backend SSH created below the tool-hook boundary, the
ClawBox SSH hook attaches a new ID before synchronous admission and inserts that
same ID into the Tool-bridge envelope. Backend-maintenance SSH is measured but
does not count as an Agent Tool call. Runtime-span coverage is a separate metric
from the mandatory Policy/SSH/guest-telemetry exact-ID join.

## ClawTune ownership

Reuse the sibling `ClawTune` repository rather than recreating its research
logic. In particular, ClawTune owns command normalization and fallback keys,
Tool duration and CPU/memory estimation, P50/P90 statistics, cgroup-v2
collection, native eBPF/kprobe telemetry, and the Runtime Tool-resource KB.
ClawBox may validate, join, freeze, and report these records and adds the
physical-memory admission and CubeSandbox residency policy around them.

Any compatibility adapter must be tested against the pinned native ClawTune
reader. A locally copied tokenizer, percentile estimator, or fallback lattice
is not an acceptable substitute when the pinned ClawTune implementation can be
called directly.

Formal evaluation trains or calibrates the KB from separate recording data,
freezes it before policy comparison, and uses the same immutable artifact in
all arms. Held-out replay does not update its own predictor unless the arm is
explicitly an online-learning experiment. Prediction error and fallback rate
are first-class results.

## VM reclamation

For `resident`, Runtime and Tool remain resident for the Agent lifetime. For
native `snapshot_pause`, a sufficiently long model wait may reclaim both VMs
only after active Tool SSH operations have ended. ModelGateway owns the
pending model result while Runtime is absent. Runtime is restored and checked
before the response is released; Tool may remain swapped until its next
admitted operation.

On the pinned CubeSandbox source revision `64102d9`, the pause path performs a
memory snapshot and then destroys the live microVM/shim while retaining a
paused tombstone. A live Kunpeng probe on 2026-09-06 touched 1.5 GiB inside a
Tool VM, observed approximately 1.69 GiB sandbox-process RSS, paused in about
0.93 seconds, observed the sandbox process disappear and roughly 1.64 GiB
return to `MemAvailable`, then restored the same guest PID and allocation.
Cleanup returned the sandbox inventory to empty. These numbers are diagnostic
evidence, not a formal policy result; formal runs must retain raw samples and
must not infer reclamation merely from a successful pause response.

Guest execution memory and host physical VM memory are different quantities.
Guest cgroup/eBPF measurements train Tool predictions. Host process/cgroup and
`MemAvailable` measurements establish density, memory-time, restore cost, and
actual snapshot reclamation.

## Evaluation contract

Deterministic managed replay is the primary c20/c40/c60 method. Only model
generation and its recorded timing are replayed. Runtime VM, OpenClaw, native
SSH, Tool VM, workspace, Tool commands, admission, pause/restore, host pressure,
cgroup telemetry, and eBPF telemetry remain real. Replay divergence fails
closed, and primary results preserve recorded wait durations without using a
future actual wait as a non-oracle prediction.

Use at least two or three heterogeneous, representative coding trajectories,
clean equivalent workspaces, deterministic trace-to-session assignment, and an
identical offered-arrival schedule across arms. Validate c1 lifecycle and
identity, then c4/c8 isolation and leaks, c20 policy behavior, and finally
c40/c60. Real-LLM c1/c2/c4 confirms that the same architecture works without
replay.

The final report separates evidence classes and includes throughput, JCT,
Tool latency, blocked time, mean/peak host memory, memory-time, prediction
error, fallback rate, reservation accuracy, pause/restore cost, reclaimed
memory, response hold, exact telemetry joins, validation, replay divergence,
wrong/duplicate Tool execution, OOM interventions, and sandbox leaks. Unit
tests, SSH reachability, a successful pause API call, or simulated scale alone
do not satisfy this contract.
