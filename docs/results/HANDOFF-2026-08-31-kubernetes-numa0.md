# ClawBox paper-evaluation handoff: Kubernetes, replay, and NUMA 0

Date: 2026-08-31 (Asia/Shanghai)  
Local workspace: `C:\Users\29068\Desktop\ClawBox`  
Remote host: `ssh kunpeng` (`hostname-txyuq.foreman.pxe`)  
Remote repository: `/home/weitianc/ClawBox`

## 1. User's required outcome

The final deliverable must be a non-toy, reproducible paper evaluation of
ClawBox baselines on the real Kunpeng machine. It must:

- use the production Kubernetes `SandboxTask`/Cell path for the primary study;
- compare conservative fixed sizing, naive static sizing, and predictive
  sizing (`p90-static`, and `p90-elastic` where its semantics add value);
- use replay for controlled comparisons, as requested by the user;
- use all resources from NUMA node 0 only: CPUs `0-79` and NUMA-0-local
  memory;
- use at least three independent, representative SWE-ReBench tasks, not three
  trajectories of one task;
- measure correct tasks/min, completed tasks/min, model steps/min, queue and
  execution latency, resource reservations, actual memory, failures/OOMs, and
  prediction provenance;
- prevent train-on-test leakage and preserve machine-readable artifacts;
- include enough repetitions and randomized/balanced arm order for a paper;
- update the README with an easy, exact reproduction interface.

Do **not** report a one-task smoke test or the direct-Firecracker pilots below
as fulfillment of this outcome.

## 2. Corrected scope decision

The project has two distinct execution paths:

1. The production path is Kubernetes -> `SandboxTask` -> two Kata/Firecracker
   Pods (Runtime and Tool).
2. The replay/checkpoint harness directly owns Firecracker processes and does
   not pass through Kubernetes or Kata's lifecycle manager.

The directly managed checkpoint code is not an interchangeable Kubernetes
backend. Porting its snapshot/restore lifecycle to running Kata Pods would
require substantial containerd/Kata/Kubernetes coordination. The user
correctly rejected presenting that implementation as a Kubernetes baseline.

Therefore:

- Kubernetes-native fixed and P90 sizing must be the primary study.
- The valid direct-Firecracker checkpoint runs may be reported only as a
  separately labeled mechanism-feasibility appendix, if useful.
- Do not claim Kubernetes checkpointing was implemented or evaluated.
- Do not restart `/data/clawbox-paper-suite-001`; the previously prepared
  216-arm direct-Firecracker matrix is no longer the primary experiment.

The automatic guard that would have launched that matrix was stopped before
the main output directory was created.

## 3. Current repository and machine state

### Source

- Local, `origin/main`, and remote HEAD were all:
  `2e82306b429f75a480a58b169239269ebff34334`.
- Remote Git worktree was clean at handoff.
- The last full local and remote test runs passed. The local run had six
  environment-dependent skips.
- This handoff document is the only new tracked file created after that
  commit; commit it if desired after reviewing it.

Important recent commits:

```text
2e82306 Separate snapshot I/O timeout and fail fast
6e1120f Bind paper reports to suite provenance
a0f8676 Document comprehensive replay report telemetry
abe9fa1 Report confidence intervals for paired effects
14d5e48 Include full telemetry in paper reports
697d059 Avoid vacuous replay progress validity
82fbe1e Show partial arm correctness in replay progress
56964fc Test paper report arm integrity gates
ce19255 Validate complete unique paper arm sets
ee4364a Add fail-closed replay paper report generator
ac9cc3a Gate main replay throughput on task correctness
ad11a2c Fix dependency-safe VM checkpoint ordering
```

### Remote host

- Kubernetes node was `Ready`.
- There were no active non-`Cleaned` `SandboxTask` objects at handoff.
- There were no Firecracker processes after the concurrency-20 pilot.
- `/data/clawbox-paper-suite-001` did not exist.
- `/data` had approximately 535 GiB free after compacting the pilot.
- Direct replay bridges `cbr0000` through `cbr0075` existed (76 bridges).
- Do not infer Kubernetes NUMA placement from those direct-replay bridges.

### Deployed Kubernetes components

The production control plane is running, including:

- `clawbox-cell-controller`
- `clawbox-ingester`
- `clawbox-tune-kb`
- managed API/dispatcher
- Kata RuntimeClass `kata-fc-arm64`

The deployed controller image observed at handoff was an immutable registry
digest, but it is not proven to correspond to Git commit `2e82306`. Before a
new registered run, rebuild/publish from a clean pinned source and record all
image digests.

## 4. Experimental results already obtained

These are real measurements, but none is the requested full Kubernetes paper
matrix. Preserve the distinction.

### 4.1 Valid direct-Firecracker concurrency-20 checkpoint gate

Purpose: verify the corrected dependency-safe checkpoint order and separate
300-second Firecracker snapshot API timeout under high concurrent snapshot
I/O. This is a mechanism gate, not a Kubernetes result.

Configuration:

- one SWE-ReBench task: `15five/scim2-filter-parser#13`;
- replay trace `rec-a`, 27 model steps/session;
- 20 simultaneous sessions;
- Runtime 2 GiB + Tool 4 GiB;
- LLM-wait checkpointing;
- NUMA node 0 direct binding;
- source commit `2e82306b429f75a480a58b169239269ebff34334`;
- source-tree SHA-256
  `853f32e8af86800c865f30ce8bd84dc433f964dca7ec2939417abb5de2a826cd`;
- ordinary Firecracker API timeout 15 s, snapshot create/load timeout 300 s.

Validity gate:

- 20/20 sessions completed;
- 20/20 correctness runs passed (`156 passed` per session);
- 540/540 model steps completed;
- 540 paired checkpoint cycles;
- 1,080 VM snapshot operations and 1,080 VM restore operations;
- zero recorded failures;
- all 20 final-state hashes were exactly
  `e0fbe70ead115074cde7e414be6cd5fef9cd1d36b748f5a107f93eb6aec2284f`;
- `final_state_equal=true`.

Measured values:

| Metric | Result |
| --- | ---: |
| Arm wall time | 1,435.003 s |
| Correct tasks/min | 0.8362 |
| Completed tasks/min | 0.8362 |
| Model steps/min | 22.5784 |
| Mean session latency | 1,390.910 s |
| P50 session latency | 1,410.807 s |
| P95/P99 session latency | 1,433.153 s |
| Mean Firecracker RSS | 43.654 GiB |
| Peak Firecracker RSS | 74.213 GiB |
| Firecracker RSS-time | 1,032.279 GiB-min |
| Allocated snapshot blocks | 120.000 GiB |
| Sum of VM snapshot service time | 18,921.912 s |
| Mean per-VM snapshot operation | 17.520 s |
| Sum of VM restore service time | 56.698 s |
| Mean per-VM restore operation | 0.0525 s |

Authoritative remote artifacts:

```text
/data/checkpoint-timeout-c20-pilot-001/study-summary.json
/data/checkpoint-timeout-c20-pilot-001/r00-replay-fixed-snapshot/results/summary.json
/data/checkpoint-timeout-c20-pilot-001/checkpoint-timeout-gate.json
/data/checkpoint-timeout-c20-pilot-evidence.tar.gz
/data/checkpoint-timeout-c20-pilot-evidence.tar.gz.sha256
```

Evidence archive SHA-256:

```text
47717d6d7ea7eea031597f566df7025b8aeaa82e1a313f84d81e70c70da43f1b
```

After hashing the evidence archive, only disposable `.ext4`, `.mem`, and
`.vmstate` payloads under the exact pilot directory were deleted. The compacted
pilot directory is about 61 MiB and the evidence archive about 16 MiB. The
deleted VM payloads are not recoverable, but all scientific summaries,
manifests, logs, validation output, and correctness output are in the archive.

### 4.2 Valid direct-Firecracker concurrency-8 checkpoint gate

Purpose: validate Runtime-first checkpoint and Tool-first restore order before
testing the snapshot timeout fix. Again, this is not a Kubernetes result.

| Metric | Result |
| --- | ---: |
| Sessions correct/completed/requested | 8/8/8 |
| Model steps | 216 |
| Arm wall time | 626.141 s |
| Correct tasks/min | 0.7666 |
| Model steps/min | 20.6982 |
| Mean/P50/P95 session latency | 605.674 / 600.014 / 623.365 s |
| Mean/peak Firecracker RSS | 15.168 / 32.010 GiB |
| Firecracker RSS-time | 157.675 GiB-min |
| Checkpoint cycles | 216 |
| VM snapshot/restore operations | 432 / 432 |
| Allocated snapshot blocks | 48.000 GiB |
| Mean snapshot/restore operation | 6.926 / 0.0490 s |

Remote root:

```text
/data/checkpoint-order-c08-pilot-001
```

All eight final-state hashes match the same expected hash used by the c20 gate.

### 4.3 One successful production Kubernetes fixed-sizing run

A real production-path Kubernetes task completed successfully on 2026-08-30:

```text
SandboxTask:
swe-kb-seed-test-20260830162046-15fiv-9e1dc11e30
Run label: kb-seed-test-20260830162046
Task: 15five__scim2-filter-parser-13-seed-01
Baseline: fixed-resident
Outcome: Succeeded / ArtifactsDurable
```

Persisted fixed `small` sizing decision:

- Runtime: 1,250 millicores, 2.5 GiB;
- Tool: 2,000 millicores, 4 GiB;
- recorded total reservation including overhead/safety:
  4,125 millicores and 8,267,812,045 bytes;
- two Pods and 20,078,972,109 storage bytes.

Timing derived from authoritative CR timestamps:

| Interval | Result |
| --- | ---: |
| Creation to `Cleaned` | 498.428 s |
| `queuedAt` to `admittedAt` | 2.689 s |
| Admission to Tool ready | 7.987 s |
| Tool ready to Runtime started | 2.664 s |
| Runtime started to `Cleaned` | 480.684 s |

Local copies made during this session:

```text
.artifacts/kb-seed-task.json
.artifacts/kb-seed-envelopes.json
.artifacts/kb-seed.log
```

This proves the production Kubernetes fixed path works. It is not a benchmark
score: `Succeeded/ArtifactsDurable` means the agent exited and artifacts were
uploaded; it does not prove the SWE-ReBench solution passed its evaluator. No
P90 Kubernetes arm has yet completed.

### 4.4 Early two-step direct replay comparison

`/data/openclaw-fixed-replay-eval-20260830` contains three repetitions of a
two-model-step direct replay for fixed 4 GiB resident versus checkpoint:

- resident: 43.235 s mean wall, 1.3878 sessions/min;
- checkpoint: 46.548 s mean wall, 1.2891 sessions/min;
- all final-state hashes matched.

This was a functional smoke test with only two model steps. It is a toy result
and must not appear as primary paper evidence.

## 5. Invalid/excluded direct-replay launches

Never pool timing from these directories:

1. `/data/clawbox-paper-suite-001-excluded-unbound-20260831`
   - suite parent/helpers were not bound to NUMA 0.
2. `/data/clawbox-paper-suite-001-excluded-sunlen-20260831`
   - long arm label exceeded the Unix-domain socket path limit.
3. `/data/clawbox-paper-suite-001-excluded-checkpoint-order-20260831`
   - unsafe Tool-first checkpoint ordering caused divergent final state.
4. `/data/clawbox-paper-suite-001-excluded-api-timeout-20260831`
   - 15-second ordinary API timeout was incorrectly used for synchronous
     multi-GiB snapshot I/O, causing 87 socket timeouts in a 51-arm partial run.

The fourth launch's scientific evidence was archived before disposable VM
payload deletion:

```text
/data/clawbox-paper-suite-001-excluded-api-timeout-evidence.tar.gz
SHA-256: 697cf7b141300d6829f233b7353499a700598349e0a68eb4612d142ddb44cb49
```

No measurements from any excluded launch are valid paper data.

## 6. Implemented functionality

### Kubernetes-native sizing

The production Kubernetes launcher supports:

```text
--baseline fixed-resident
--baseline p90-static --kb-generation N
--baseline p90-elastic
```

Relevant code:

- `clawbox/benchmark/kubernetes.py`
- `clawbox/cell/controller.py`
- `clawbox/cell/capacity.py`
- `clawbox/cell/p90.py`
- `clawbox/cell/manifests.py`

`p90-static` resolves an exact native KB generation. `p90-elastic` resolves the
latest generation once at admission. Both freeze the full decision in
`status.sizingDecision`. The predictor changes Tool CPU/memory only, with 25%
headroom, a 250m CPU floor, a 2 GiB memory floor, and caps at the selected fixed
profile. Runtime and storage stay fixed.

The launcher persists:

```text
.status.sizingDecision.baseline
.status.sizingDecision.prediction
.status.sizingDecision.cellSize
.status.reservation
.status.queuedAt/admittedAt/toolReadyAt/runtimeStartedAt/cleanedAt
```

### Replay/checkpoint infrastructure

The direct harness has:

- fixed 4 GiB, fixed 2 GiB, and P90-static sizing arms;
- resident and LLM-wait checkpoint arms;
- Runtime-first checkpoint and Tool-first restore;
- separate 15 s ordinary and 300 s snapshot API timeouts;
- atomic CPU-pair/resident-memory leases;
- correct tasks/min and steps/min reporting;
- RSS, RSS-time, NUMA, cgroup, session-tail, admission-tail, and snapshot
  telemetry;
- fail-closed complete-suite report generation;
- fail-fast suite execution.

This remains useful mechanism code, but it is not the Kubernetes study.

### Frozen offline P90 evidence

Recovered recording-set-2 telemetry produced 58 trusted completed calls. The
frozen repo-level `exec` prediction was:

- latency P90: 9.054 s;
- CPU P90: 0.8998 cores;
- command-cgroup memory P90: 1,531,904 bytes;
- 51 matching `exec` observations.

The 2 GiB floor dominates the selected Tool memory. Consequently P90-static
and naive fixed-2-GiB are operationally identical on this task. Any benefit
against fixed 4 GiB is evidence-gated selection of a smaller static profile,
not proof that prediction beats an equal-size naive control.

Files:

```text
/data/workloads/p90-recording2-train-recording1-eval.json
docs/results/artifacts/kunpeng-2026-08-31/p90-recording2-train-recording1-eval.json
docs/results/artifacts/kunpeng-2026-08-31/p90-loo-calibration.json
```

Training and evaluation recordings are disjoint recording sets but originate
from the same SWE task. This is not task-level holdout.

## 7. Critical blockers before the requested full study

### 7.1 Kubernetes does not currently guarantee NUMA 0

The `SandboxTask` CR, Kubernetes launcher, and Cell manifest path have no
NUMA-node or CPU-set experiment field. Recording `nodeName` proves only the
physical node, not NUMA node 0. Current fractional CPU requests also do not
provide exclusive CPU-manager allocation.

Do not claim NUMA-0 isolation from the current Kubernetes path.

The next session must design and validate a Kubernetes-native NUMA mechanism.
Potential host-level approaches include kubelet CPU Manager + Topology Manager
and appropriately reserved CPUs/memory, or a CRI-aware resource manager. This
is host-sensitive: inspect the existing kubelet/containerd configuration and
create a reversible migration/rollback plan before changing it. Merely adding
a Kubernetes node label is insufficient because all four NUMA nodes belong to
one Kubernetes node.

Acceptance evidence must show, for every Runtime and Tool VM:

- host Firecracker PID;
- `Cpus_allowed_list` restricted to `0-79`;
- `Mems_allowed_list` restricted to NUMA node 0;
- parent controller/replay gateway measurement helpers also restricted as
  appropriate;
- no workload CPUs or memory allocated from NUMA nodes 1-3;
- NUMA-0 memory and CPU capacity/reserve recorded before and after.

### 7.2 Kubernetes production path is API-only, not replay

`clawbox.benchmark.kubernetes` currently resolves only:

```text
swe_rebench + openclaw + api + kubernetes + ssh + resident
```

The direct runner owns the replay gateway. The Kubernetes launcher has no
`--inference-backend replay`, trace field, or request-validation plumbing.

To meet the user's replay requirement, implement a Kubernetes-compatible
OpenAI replay endpoint without bypassing the production Runtime/Tool Cells.
The clean design should:

- keep real OpenClaw in the Runtime VM and real SSH Tool commands;
- point the Runtime's OpenAI-compatible endpoint at a controlled replay
  gateway reachable under explicit NetworkPolicy;
- route sessions deterministically and validate request payloads;
- preserve recorded response bodies and measured latency at `time_scale=1`;
- fail closed on request count/order/body divergence;
- record replay trace SHA-256 and gateway source/image digest;
- avoid sharing mutable replay position between concurrent sessions;
- include replay service/gateway CPU and memory in the experimental boundary
  or explicitly exclude and justify it.

Legacy recording-set-1 traces do not contain request payloads. Prefer new
request-validating recordings or clearly mark request bodies unvalidated.

### 7.3 No native Kubernetes P90 generation for the target identity

At handoff, `/var/lib/clawbox/tune-kb/tune-kb.db` contained only this native
snapshot:

```text
tenant=kb-acceptance
repo=clawbox/toolbridge-integration
generation=1
```

There was no native admission snapshot for tenant `benchmark` and repository
`15five/scim2-filter-parser`. The successful `kb-seed-test` task did not create
one in Tune-KB. Old native artifacts use per-task tenant-like identities and
repository `testbed`, indicating that tenant/repository propagation or
projection must be audited before P90 runs.

Useful read-only diagnostic script created locally:

```text
.artifacts/query-kb.sh
```

Do not launch P90-static until an exported exact generation exists and its
tenant, repository, evidence count, source/pair digests, and ClawTune revision
are archived. P90-elastic must persist the same expected generation if it is
used in a paired comparison.

### 7.4 Only one ARM64 SWE-ReBench image is mapped

`/data/swe-rebench-arm64-map.json` has exactly one supported image:

```text
15five/scim2-filter-parser#13
127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:
bdf4637498a4b765f0e91333ff292c226bd011a31c220d76609daff43e2c39fd
```

The expected `/data/swe-rebench.parquet` was absent. Task selection JSON exists
at `~/ClawTune/swe_rebench/tasks.json` and `tasks_128.json`.

Build at least three independent task images and archive the mapping recipe,
dataset digest, SWE-bench harness commit, Tool Bridge binary digest, and final
OCI digests. Repeated copies or trajectories of the same task do not increase
the independent sample size.

### 7.5 Kubernetes success is not SWE correctness

`Succeeded/ArtifactsDurable` means the agent exited zero and uploaded its
artifacts. It is not an official SWE-ReBench evaluation result. Retrieve each
archived `result.json`/patch and evaluate it with the pinned SWE-ReBench harness
against the correct base commit. Report both infrastructure completion and
correct task completion.

The current `scripts/clawbox traces TASK` exports trace artifacts but not the
central archived `result.json`; extend the evidence/retrieval interface or use
the authenticated archive endpoint. Never expose secret values in logs.

## 8. Required full experiment design

After the blockers above are resolved, use this minimum design.

### Workloads and split

- At least three independent SWE-ReBench tasks/repositories with native ARM64
  images.
- Prefer 5-10 tasks if build/runtime budget permits.
- Train P90 only on earlier tasks/runs; evaluation task IDs must not occur in
  training evidence.
- Freeze one immutable KB generation for `p90-static`.
- Include a naive static arm set to the same CPU/memory selected by P90, so the
  incremental predictive claim is identifiable.

### Arms

Primary Kubernetes arms:

1. conservative `fixed-resident` (`small`, current Tool 2 CPU/4 GiB);
2. naive static with the exact P90-selected Tool resources;
3. `p90-static` pinned to the frozen generation;
4. optionally `p90-elastic`, with every resolved generation recorded.

There is no Kubernetes checkpoint arm unless a genuine Kata/containerd
lifecycle implementation is added and independently validated.

### Load levels and repetitions

- Use NUMA-0 capacity only: 80 logical CPUs and 515,829 MiB local memory,
  minus an explicit host/control-plane reserve.
- Sweep concurrency that exposes both CPU and memory effects, for example
  `[1, 8, 20, 40]`, then add a memory-pressure/density sweep only after the
  Kubernetes capacity model is correctly bounded to NUMA 0.
- At least three repetitions per task/arm/concurrency cell.
- Deterministically randomize or Latin-square arm order within task and
  repetition.
- Run on a clean node; record background process, pressure, storage, and NUMA
  samples before, during, and after.

Do not run only one arm at a time if throughput under concurrency is the
research question. A one-task serial run may be used solely as a gate before
the registered matrix.

### Primary metrics

- correct completed tasks/min;
- completed sessions/min;
- replay model steps/min;
- wall time and session p50/p95/p99;
- queue/admission and VM startup p50/p95/max;
- requested/reserved Runtime and Tool CPU/memory;
- whole-VM Firecracker RSS, peak RSS, and RSS-time;
- NUMA-0 local memory usage and remote-node allocation violations;
- cgroup CPU/memory, OOMs, timeouts, and infrastructure failures;
- P90 evidence count, generation, source/pair digest, predicted values, chosen
  values, floors/caps, and observed coverage;
- official SWE-ReBench correctness for each final patch.

Use task ID as the independent statistical unit. Average repeated trajectories
within task. Do not compute cross-task confidence intervals with `n=1`.

## 9. Immediate next-session order

1. Read this file and verify remote state; do not assume it has not drifted.
2. Confirm no Firecracker/direct suite process and no active `SandboxTask`.
3. Audit kubelet CPU Manager, Topology Manager, Memory Manager, containerd/Kata,
   and current cgroup topology read-only.
4. Design the reversible NUMA-0 Kubernetes isolation mechanism and add an
   automated preflight/attestation gate.
5. Implement Kubernetes replay inference while preserving OpenClaw and the
   Runtime/Tool Cell path.
6. Fix tenant/repository propagation into native Tune-KB and prove an exact
   P90 export for a held-out identity.
7. Build/map at least three independent ARM64 SWE-ReBench tasks.
8. Add a Kubernetes paper-suite runner that randomizes arms, resumes safely,
   fails closed, retrieves archived results, runs the correctness evaluator,
   and writes a single provenance-bound measurements table.
9. Run one non-counted gate for fixed and P90/replay on NUMA 0.
10. Register/freeze configs and then run the complete matrix.
11. Generate final JSON/CSV/Markdown, update README reproduction instructions,
    run all tests locally and remotely, commit, push, and audit completeness.

## 10. Useful commands and paths

Read-only state check:

```bash
ssh kunpeng
cd ~/ClawBox
git rev-parse HEAD
git status --short
pgrep -af 'firecracker|clawbox.replay.cli' || true
kubectl get nodes -o wide
kubectl -n clawbox-benchmarks get sandboxtasks
df -h /data
numactl --hardware
numastat -m
```

Production Kubernetes launcher interface:

```bash
bash scripts/run-swe-rebench.sh --help
```

Relevant inputs:

```text
/data/kb-seed-tasks.json
/data/swe-rebench-arm64-map.json
~/ClawTune/swe_rebench/tasks.json
~/ClawTune/swe_rebench/tasks_128.json
```

Existing direct-study documentation/configs (reference only, do not launch as
the Kubernetes paper result):

```text
docs/results/p90-baselines-kunpeng-2026-08-31.md
docs/results/artifacts/kunpeng-2026-08-31/main-suite.json
docs/results/artifacts/kunpeng-2026-08-31/checkpoint-density-suite.json
```

Local diagnostic/launch artifacts created during prior work are under
`.artifacts/`. They are intentionally ignored by Git. Some contain stale PIDs
and old direct-suite commands; do not execute them without reading and updating
them. In particular, the v5 continuation guard was stopped and must not be
restarted for the revised Kubernetes scope.

## 11. Honest final status

- Baseline and telemetry implementation work is substantial and tested.
- Direct checkpoint mechanism correctness is validated at concurrency 8 and
  20 on NUMA 0.
- One real Kubernetes fixed Cell run succeeded.
- No Kubernetes P90 result exists yet.
- No Kubernetes replay result exists yet.
- No Kubernetes NUMA-0 placement guarantee exists yet.
- Only one independent ARM64 SWE-ReBench task is currently available.
- Therefore the requested full paper evaluation is **not finished**.

