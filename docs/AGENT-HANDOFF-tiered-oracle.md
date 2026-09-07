# ClawBox tiered-oracle study: agent handoff

Prepared on 2026-09-07 at the user's explicit request to stop the current work
and transfer it to another code agent. This document is the entry point for the
successor. It contains the necessary conversation decisions; no conversation
transcript is required. It does not certify that the implementation is correct
or that the machine is currently healthy.

**Read this entire document before acting. Do not resume the old Kubernetes
recovery workflow. There are no formal c40 performance results yet.**

## 1. Final objective and instruction precedence

Finish the two tiered oracle baselines, establish a correct standalone
CubeSandbox deployment on kunpeng, pass the required replay and tier gates,
run all 13 baselines at c40, and deliver measured, reproducible paper-oriented
results plus a concrete setup guide a human can follow without a code agent.

The latest user instructions override earlier experiment sizes and deployment
assumptions:

| Subject | Final instruction |
| --- | --- |
| Architecture | ClawBox uses standalone/bare-metal CubeSandbox, not Kubernetes. The user explicitly rejected the recent Kubernetes changes. |
| Remote access | Use `ssh kunpeng`. All implementation validation and formal experiments run on kunpeng. Local source inspection/editing and document checks are fine. |
| Scale | c1 and c4 correctness/integration gates, then c40. No c8 or c60. |
| Formal repetitions | One run per baseline; no repeated formal runs. Infrastructure-invalid attempts may need repair and rerun, retaining every attempt. |
| Baselines | Preserve all 11 canonical policies; add exactly the two named below. Total: 13. |
| Workload | Keep the selected rec-a trace, recorded responses, action order, and timing fixed. |
| Resources | Choose defensible memory settings using preflight/pilot evidence, explain them, and freeze before comparing policy performance. |
| Evidence | Download all experiment traces/logs; retain genuine failures and source/provenance evidence. |
| Git | Commit and push each coherent milestone. Preserve existing changes. |
| Recovery | The user explicitly authorized `sudo reboot` if needed. Authorization exists, but passwordless sudo did not work. |
| Working style | Work autonomously within scope; concise progress updates; favor reuse and a simple supported deployment. Do not promise an unsupported completion time. |
| Current agent | Stop experiments/implementation and produce this handoff. Resume task work only as the successor assigned by the user. |

The original `docs/tiered-oracle-experiment-plan.md` remains the detailed
scientific/invariant specification and must be read completely, but its c60,
c8, three-repetition, and 63-run scheduling instructions are superseded.
The final agreed operational matrix is the 13-policy c40 comparison once each,
after c1/c4 gates. Do not silently restore sensitivities, repetitions, or c60.
The original capacity sensitivities and matched-capacity controls remain
unmeasured unless the user separately expands the final scope. Report the
resulting limits on conclusions explicitly.

`docs/cubesandbox-setup.md` specifies standalone deployment and says the old
Kubernetes deployment is historical diagnosis only. That is the correct
architecture. In contrast, `docs/tiered-oracle-recovery.md` was recently written
around the wrong Kubernetes deployment: preserve its useful fault evidence,
but do not execute its Pod/DaemonSet continuation instructions.

## 2. Honest completion status

| Requirement | Evidence/status at handoff |
| --- | --- |
| Two new policy definitions and initial storage wiring | Implemented in source, not integration-certified. |
| Existing 11 canonical definitions | Present; c40 config contains all 13 names. Preserve semantics, not just names. |
| Selected rec-a complete c1 | NOT passed in this effort. Several failed attempts are retained. |
| Direct WARM transition/placement gates | NOT passed with the final deployment. |
| c4 overflow, race, identity and correctness gates | NOT completed. |
| Formal c40 comparison | NOT run. No throughput/JCT/memory comparison exists. |
| Targeted Python tests on kunpeng | Passed at source `753de20`; later capacity regression verified separately. These are not replay or physical-placement evidence. |
| Latest c40 schema/config check | Passed on kunpeng: c40, one repetition, 13 policies, legacy time oracle present. |
| Archives | Study evidence, recording/resource evidence, and CubeSandbox tracked source downloaded; hashes below. |
| Final paper result package | NOT produced. Status/recovery documents are not a final report. |
| Human setup guide | Existing guide plus an incorrect Kubernetes recovery guide; final verified standalone instructions still required. |

Do not call this task nearly finished. Source exists, but major correctness and
deployment obligations remain. A host reboot alone will not establish readiness.

## 3. Policy and workload contract

### Canonical baselines

Retain these eleven definitions from `clawbox/experiments/baselines.py`:

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

Add exactly:

12. `tool-p90-tiered-lru-oracle-reactive` (A)
13. `tool-p90-tiered-time-oracle-reactive` (B)

Aliases do not count as additional baselines. The existing time-oracle baseline
uses static Tool admission and a 4-second threshold; do not turn it into a P90
or 2-second baseline. Preserve the frozen command-specific ClawTune P90 path,
managed OpenClaw Runtime/Tool pair, native SSH, endpoint refresh, workspace
ownership, response holding, and reactive restore behavior.

### Exact new-policy behavior

| Event | A: pressure-triggered oracle LRU | B: wait-start oracle placement |
| --- | --- | --- |
| Wait starts | Record current wait ID, duration, monotonic deadline; no automatic checkpoint. | Decide from full recorded duration D: D < 2 s stays LOCAL; 2 <= D < 20 s goes WARM; D >= 20 s goes directly COLD. |
| LOCAL pressure | Among safely reclaimable agents in a current model wait, prefer remaining wait >= 2 s, then use existing agent LRU. Choose tier from remaining duration at action time using the 2/20 s thresholds. | Preserve the wait-start choice. Queue admission; do not silently invoke A as a pressure fallback. |
| Short-wait fallback | Only after eligible >= 2 s victims are exhausted, allow eligible < 2 s victims, preferring WARM. If no safe victim exists, wait. | Short waits remain LOCAL. |
| Checkpoint | Eligible LOCAL Tool first, then eligible LOCAL Runtime; skip nonresident roles. | Same. |
| WARM full | Spill eligible per-snapshot WARM-LRU generations to COLD until reservation fits; wait for temporarily pinned dependencies. | Same. |
| Model response ready | Restore Runtime and verify readiness before releasing the response. | Same. |
| Next Tool request | Restore Tool reactively, refresh endpoint, verify identity, use existing P90/native-SSH path. | Same. |

Use only the current wait's oracle information. Remaining time is
`max(0, monotonic_deadline - now)`; it excludes response holding and migration
overhead. B keeps the decision made from D and logs queue delay. A rechecks
eligibility before committing delayed work. Never begin a queued checkpoint
after its response is ready. If readiness occurs during a committed checkpoint,
finish the safe transition and restore before delivery. Do not automatically
demote a WARM entry when time crosses a threshold. Reject tiered oracle policies
with live inference. These are oracle-informed heuristics, not proven optimal
upper bounds.

Preserve revised RQ2 verbatim:

> How does pressure-triggered placement compare with placement at model-wait
> start when both policies have perfect knowledge of the current model wait?

The plan references RQ1/RQ3/RQ4/RQ5 and an external brief's ten report sections,
but the exact wording of those questions/sections was not found in the inspected
repository documents. Do not invent their original wording or claim the brief
was available. Use the concrete deliverables here and the plan; locate any
additional brief if present. Missing wording does not prevent implementation or
the agreed c40 study, but must be disclosed when mapping the final report.

### Frozen selected trace

Repository: `15five/scim2-filter-parser`.
SWE-ReBench instance: `15five__scim2-filter-parser-13`.
Base commit: `08c32462831d3849a70241ac9fea946b6b1884a6`.
Task: return a NamedTuple instead of a tuple in the SQL transpiler.

Selected artifact on kunpeng:

```text
/home/weitianc/ClawBox/results/paper_replay_20260901_128g_v4/selected-traces/rec-a-enriched.jsonl
SHA256 12541145678c9f65c5b1388410f82a2d9954a26b100a3a781db5fa837eb83ae5
```

Original recording:

```text
/data/recording-output/model-trace-session-0000.jsonl
SHA256 8be4a7a6affe1f316e585cf7bdef170cd6e48ba43bf844a923045a1793d28b47
```

Earlier comparison verified all 27 action IDs, timestamps, recorded latencies,
and responses match. Request metadata provenance and full replay compatibility
still need validation. Recorded model: `deepseek-v4-flash`; time scale 1.0.
Wait mean/p50/p90: 5.060/3.692/9.000 seconds; max 21.487 seconds.
Bins: 3 waits < 2 s, 23 waits from 2 to < 20 s, 1 wait >= 20 s.
Recorded responses issue 31 Tool calls: 28 exec, 1 read, 1 edit, 1 apply_patch.
Issued calls are not proof of completed native executions.

Resource/bridge evidence is under `/data/recording-evidence/session-0000/`.
Complete the Tool-duration/memory characterization and execution joins from it.
The original recording summary reported validation exit 129; do not erase it.

Do NOT substitute the diagnostic trace:
`/home/weitianc/clawbox-results-current/frozen-traces/rec-a-current-openclaw.jsonl`
(SHA256 `8ea22236666382a270624c5216b66df6da415c546f65486ce92e45db77c11bf4`).
It has different request metadata. Also do not use
`examples/traces/openclaw-cube-replay.jsonl` as the formal rec-a workload.

The current configuration retains the exact prompt, including whitespace:
`examples/experiments/tiered-oracle-rec-a-c1.yaml` and
`examples/experiments/tiered-oracle-rec-a-c40.yaml`. Do not retype/normalize it.
Task validation command:

```bash
cd /testbed && PYTHONPATH=/testbed/src /opt/miniconda3/envs/testbed/bin/python -m pytest tests/ -q
```

Prediction artifacts (local hashes checked at handoff):

| File | SHA256 |
| --- | --- |
| `examples/predictions/rec-a-independent-p90.json` | `479f8a64b93a466b1b685ad759f21b34021ec34e80015b483c7f4170f259f121` |
| `examples/predictions/rec-a-heldout-oracle.json` | `0788a26688a2e4a028e4bd90613985d6387e7b29975896ba964f1cefbab2fc1a` |

Verify both hashes against the files before use. Independent model-wait prediction in the config is
9.054413080215454 s, source `independent-recording2-repo-tool-p90`.
Audit `scripts/freeze-rec-a-policy-inputs.py` and artifact provenance/fallbacks:
the host-increment mapping uses a conservative full-Tool bound. An exact-command
key alone does not prove an independently measured command-specific host peak.
Do not leak held-out model-wait oracle values into non-oracle policies, silently
replace P90 admission, or relabel missing oracle evidence as measured.

## 4. Resource and lifecycle invariants

Candidate configuration, not certified measurements: Runtime 2 GiB, Tool 4 GiB,
2 vCPUs each; LOCAL 64 GiB on NUMA0; WARM 64 GiB on NUMA1. c40 therefore offers
240 GiB of configured VM memory. LOCAL checkpoint/restore headroom is 8 GiB
inside the limit; emergency free-memory setting is 8 GiB. Static Tool reservation
is 512 MiB. Seed 20260907; arm timeout 1800 s; command timeout 300 s; sampling
interval 0.2 s. Review feasibility before freezing the formal manifest, never
tune based on which policy wins.

Mandatory conditions:

- Enforce actual experiment-scoped LOCAL memory and NUMA placement, including
  agreed overhead/headroom. Admission reservations and a node-wide memory delta
  are not hard enforcement. Keep a whole-host emergency guard separately.
- WARM is bounded NUMA1 tmpfs with `mpol=bind:1` and `noswap`. Verify writer
  cpusets and actual file-page placement; mount options alone are insufficient.
- First memory-snapshot writes for WARM must target WARM directly. Never write
  through SSD and copy back to claim direct checkpointing. Restore directly
  from the selected tier.
- Separate reserved, committed, logical, allocated, and transferred bytes.
  Atomically enforce reserved + committed <= capacity, including incomplete
  writes and retained backing, without double counting. Audit transfer from
  reservations during writes, not just at final manifest publication.
- Use authoritative generation manifests. Pin restore/spill dependencies;
  serialize operations on the same sandbox. Distinguish lifecycle status from
  stable tier. Reconcile failures with actual backend state, not assumed state.
- Oversized WARM generations are explicit unsupported-capacity outcomes;
  never silently route them COLD. Explicit WARM-disabled controls are a separate
  configuration, not an implicit fallback.
- WARM-to-COLD spill must verify a temporary COLD copy, complete persistence
  barriers, publish authority atomically, and only then release WARM. Audit
  directory/catalog persistence and crash consistency, not only file fsync.
- Determine eager versus mapped/lazy restore. Retain and charge backing files
  until safely released. Verify restored physical pages reside in LOCAL.
- Control COLD page cache with a common scoped protocol. Distinguish file I/O
  from physical SSD I/O; unknown metrics are null with reasons, never fake zeros.
- A WARM memory hit does not imply zero rootfs/workspace SSD I/O.
- Never relax replay matching, identity checks, telemetry joins, capacity, or
  NUMA invariants to get a passing run. Record a concrete blocker if infeasible.

### Known implementation gaps to address before formal runs

1. `NumaNodeMemorySampler` in `clawbox/experiments/memory.py` and its worker
   wiring measure a node-wide baseline-subtracted delta. They do not establish
   a hard experiment cgroup limit or placement of VM pages.
2. `CubeSandboxLifecycle.restore()` connects before removing the WARM manifest,
   with no restore pin around connect. A concurrent spill selection can pin the
   generation during restore. Add a deterministic race regression and fix
   ordering/serialization; inspect cross-lifecycle lock ordering too.
3. Restore removes WARM accounting after connect without sufficient evidence
   that backing is released. Establish backend semantics before fixing charges.
4. Checkpoint exception handling restores the previous local state and aborts
   the reservation; audit partial backend success and leftover files before
   assuming that rollback is accurate.
5. Full transition telemetry, COLD cache protocol, physical enforcement, and
   all required boundary/race tests remain unverified. The prior small test
   suites do not cover the full contract.

The last completed fix, `eab3be3`, makes an oversized request raise
`WarmSnapshotTooLarge` rather than generic `WarmCapacityError` during victim
selection. Contrary to an earlier mistaken explanation, the old code did NOT
actually spill entries in that oversized case; it already failed to find a
sufficient victim prefix. The fix is correct error classification.

## 5. Local and remote state

### Local repository

```text
C:\Users\29068\Desktop\ClawBox
Shell: PowerShell
Branch: main
Implementation HEAD before this handoff:
eab3be3265ee561896d029de880663c9d3a674a1
Upstream: github.com/w20chen/ClawBox.git
```

Tracked worktree was clean before writing this document. Implementation HEAD
was pushed and checked against origin. The handoff itself will be a subsequent
documentation commit; inspect `git log -1` rather than assuming its hash here.
No ClawBox `AGENTS.md` was found in prior applicable checks; check again in the
successor environment, including ancestor directories. CubeSandbox's AGENTS
requires `Assisted-by: AGENT_NAME:MODEL_VERSION` or
`Autonomously-by: AGENT_NAME:MODEL_VERSION` and prohibits AI-added Signed-off-by.

### Remote source inventory: last observed, recheck before use

| Path | Last known state / caution |
| --- | --- |
| `/home/weitianc/ClawBox` | Original dirty checkout; early observation `d42da589...`. Preserve it. Holds selected trace evidence. |
| `/home/weitianc/ClawBox-cube` | Unversioned application tree, not reliable source provenance. |
| `/home/weitianc/ClawBox-tiered-2906a91` | Isolated implementation checkout, last at `753de20`; later local commits are NOT automatically deployed there. An uploaded c40 YAML may be untracked. |
| `/home/weitianc/ClawBox-tiered-d4b1ce4` | Older environment also referenced by reused virtualenv launchers. Do not assume unused. |
| `/home/weitianc/CubeSandbox-tiered-20260907-v4` | Branch `codex/tiered-oracle-direct-memory-20260907`, last `094daaa`; tracked source clean then, untracked CubeMaster coverage. Origin was a bundle, no writable upstream. |
| `/home/weitianc/clawbox-capacity-validation-20260907-icc9id` | Isolated exported source with before/after regression logs and capacity fix. Not a deployed service. |
| ClawTune checkout | Earlier HEAD `76eab6fa5c6333f4e80901c030f10cab0e4ce605`; locate/recheck before using. |

CubeSandbox source lineage: `64102d9` semantic TCP endpoint, `fdc1a8c` tiered
direct snapshots, `b54ffaa` provenance compatibility, `094daaa` same-node
HostPort forwarding patch. The local `.worktrees/CubeSandbox-tiered` was at
64102d9 with edits, not equivalent to a clean 094daaa checkout. Never reset it.
Some historical Git parents are missing; `git bundle --all` failed. The
downloaded tracked-source archive is a fallback, not full repository history.

### Last-known machine state, not refreshed during handoff preparation

- `ssh kunpeng` resolves to user `weitianc`, address `193.124.7.2`, port 22.
- Node hostname: `hostname-txyuq.foreman.pxe`; ARM64, four NUMA nodes, about
  2 TiB RAM total. Early nodes 0/1 each had about 503 GiB. Re-measure free RAM,
  storage, background load, and placement before provisioning.
- Host kernel: `6.6.0-72.0.0.76.oe2403sp1.aarch64`.
- Last uptime observation: 2026-09-07 around 06:03 UTC, up about 4 h 28 min.
  No successful reboot was observed after that probe.
- Host journal contains repeated soft lockups in mount-namespace teardown:
  `native_queued_spin_lock_slowpath -> mntput_no_expire -> namespace_unlock ->
  put_mnt_ns -> do_exit`. At 05:35:33 UTC: runc init PID 774692, CPU 141.
  At 05:46:54 UTC: `(ostnamed)` PID 785715, CPU 250. This is HOST kernel evidence,
  not the guest DAX failure described later. Root trigger is not established.
- Containerd repeatedly failed to process already-exited containers' TaskExit
  events, with DeadlineExceeded errors. Its last observed PID remained 19275.
  A Docker administrative helper never ran its restart command: systemd timed
  out creating the helper scope. Do not claim containerd was restarted.
- SSH authentication succeeded but opening command sessions often took tens
  of seconds. Kubernetes queries and container operations often timed out.
- `ssh kunpeng "sudo -n reboot"` was explicitly attempted after user approval
  and failed with `sudo: a password is required`. Root SSH also failed auth.
  Do not request or expose passwords in chat. A human can use
  `ssh -t kunpeng "sudo reboot"` and enter the password in their terminal.
- No fresh SSH/deployment/reboot was initiated to prepare this handoff. A local
  process inventory at handoff showed no ssh.exe processes. This is NOT proof
  that all remote guest VMs or containers are gone; inventory remote ownership.
- Standalone CubeSandbox installation state has NOT been established. This
  is the first deployment question to answer after host recovery.

### Old Kubernetes deployment: historical evidence, not the target

The previous agent incorrectly reused namespace `cube-system`. Last known
replacement node was `cube-node-554rc`, Pod IP `192.168.3.179`, stuck Init:0/1.
API/master/proxy were also Kubernetes Pods. Earlier node pods `cbzcd`, `pqx8p`,
and `vhk7f` were force-deleted; container-runtime errors indicate orphaned
processes may have remained. Do not repeat force deletions or blindly kill
processes to clean this up. Inspect ownership and require an explicit, scoped
cleanup plan before retiring shared old infrastructure.

Historical endpoint settings `CUBE_API_URL=http://127.0.0.1:30030` and proxy
port `30080` belonged to that environment. Do NOT copy them into a standalone
machine configuration without inspecting actual listeners/services. Likewise,
do not adopt Pod IPs as Tool endpoints. ClawBox must call CubeSandbox's semantic
`get_tcp_endpoint(2222)` API and receive a Runtime-reachable deployment address.
No NodePort, Redis lookup, guest-IP fallback, or ad hoc SSH proxy is permitted.

Historical registry containers were `clawbox-registry` and
`clawbox-registry-podnet`, sharing a volume. Push address `127.0.0.1:5000`;
template read address `172.17.0.1:5001`. Inventory before reuse. A built image
can be useful without continuing its Kubernetes deployment.

### Reusable build artifacts: candidates, not standalone certification

| Artifact | Last known value |
| --- | --- |
| Cubelet image | `127.0.0.1:5000/clawbox/cubelet:tiered-094daaa` |
| Registry digest | `sha256:977399a2e5dcca6659a2a8d4f356343d2352542bbf316358c623f1039324baa0` |
| Docker image ID | `sha256:b75249195f83f3b68968192889dfa1065121c006719ba5cf9dbdd043968ef3be` |
| Existing builder | `cube-sandbox-builder:ubuntu2004` |
| Runtime template | `tpl-f4c0c6a3c80a48be95b271b4` |
| Runtime image digest | `sha256:54a2b7d5d488bb8f862d90d85e16d46fe92692af7c2801baf44dab3653245a0c` |
| Tool template | `tpl-3ed0a8051a404e20a6d9890a` |
| Tool image digest | `sha256:f0d65d4474aa97ca471062771bda8b0bdffa831e34b8536a522d56d4e0b7cc10` |
| Guest kernel tag | `6.18.28-cube-kprobes-daxfix1` |
| Guest kernel SHA256 | `5b59ed694175fb6eb26167d101ee15fd985c11f65ff698b10e586a2d23020820` |
| Component version directory | `/data/cubelet/root/component_versions/cube-kernel-scf/sha256-5b59ed694175` |
| Kernel build output | `/home/weitianc/CubeSandbox-tiered-20260907-v4/_output/kernel/aarch64-6.18.28-cube/vmlinux` |
| WARM root used previously | `/data/cubelet/clawbox-tiered-20260907/warm` |
| COLD root used previously | `/data/cubelet/clawbox-tiered-20260907/cold` |

Runtime workspace is `/workspace`; Tool workspace is `/testbed`. Source image
references and full template metadata are in the YAML and evidence archive.
Template IDs are environment/catalog-specific: preserve digests and prove any
reuse in standalone, rather than assuming IDs transfer. The Cubelet build
compiled ARM64 BPF and Go binaries but did not prove runtime forwarding.
The full build console was not saved as a dedicated remote build log; do not
invent that evidence. Reuse caches rather than rebuilding everything by default.

## 6. Mistakes already made and how not to repeat them

| Prior mistake | Required correction |
| --- | --- |
| Reused and repaired old Kubernetes deployment despite standalone documentation | Inventory and use standalone CubeSandbox. Kubernetes repair is not the experiment plan. |
| Applied broad deployment changes while chasing connectivity | Freeze one isolated source/build/service set and verify each gate; inspect effective binaries, not labels. |
| Missed existing HostPort hairpin patch | Audit the required patches and regenerated embedded BPF before building. Patch presence is not runtime proof. |
| Repeated force deletion of stuck Pods | Container exits may remain stuck in the host kernel. Preserve evidence; use host recovery instead of endless rollouts. |
| Claimed an old successful c1 was comparable | The known success used a different tiny smoke trace. It does not validate selected rec-a. |
| Suspected normal inner guest IP was stale network state | `169.254.68.6` is normal CubeSandbox inner addressing. Do not rebind guest IPs based on that alone. |
| Tested Tool SSH before bridge setup | Tool bridge starts through `native_tool_bridge_setup_command()`; snapshot entrypoints do not necessarily rerun with injected keys. |
| Confused template HTTP endpoint with native TCP routing | Use `get_tcp_endpoint(2222)` and test Runtime-to-Tool identity. Host connectivity or populated BPF maps is insufficient. |
| Changed guest kernel during host failure diagnosis | Distinguish host mount-namespace lockups from guest DAX faults. Avoid speculative kernel rebuilds. |
| Overstated implementation/tests as near completion | Report exact passed gates, missing gates, and real c40 coverage. |
| Used moved virtualenv pip launchers | Use the selected interpreter's `python -m pip`; check real interpreter/prefix and recreate an isolated venv when needed. |
| Repeated PowerShell escaping failures | Do not use Bash-style `\"` or `\$` to escape PowerShell. Put complex remote work into reviewed script files and SCP them, then `ssh kunpeng "bash /absolute/script.sh"`. |
| Downloaded long trace names directly on Windows | Archive on Linux, download the archive, compare SHA256. Preserve originals. |
| Treated source code assumptions as physical evidence | Validate bytes, placement, retained dependencies, and actual service state on the host. |

### Commit audit and selective cleanup

Do not blanket-revert today's work; commits mix valid policy changes and wrong
deployment assumptions. Use new corrective commits without rewriting history.

| Commit | Meaning / treatment |
| --- | --- |
| `6d5badd` | Starting point before tiered work; already documented standalone and four CubeSandbox patches. |
| `58c2620` | Tiered policy core and original plan; keep and audit. |
| `e50a3c0` | NUMA sampler/direct spill; mixed patch includes wrong Kubernetes chart changes. Remove those hunks while preserving required backend work. |
| `d4b1ce4` | Replay request compatibility; audit without weakening validation. |
| `2906a91` | Frozen inputs/matrix and updated mixed CubeSandbox patch; audit input provenance. |
| `6a8294a` | Wrong old CubeNode startup workaround; added fixed Pod gateway neighbor. Remove from active deployment path. |
| `0a33e96`, `dc55933` | Image entrypoint and template provenance fixes; not intrinsically Kubernetes-specific. |
| `e69d10a`, `ff2bed8`, `4220059` | Template/request environment/prompt compatibility changes; later template commits supersede IDs. |
| `114dadc`, `8a0079e`, `dcd7e62` | Guest kernel metadata evolution and DAX fix; don't conflate with the host kernel failure. |
| `39a2327`, `753de20` | Policy SSH auth fallback and authenticated Runtime template; keep valid behavior. |
| `de9b155` | Correct c40-only scheduling update plus historical deployment status. Preserve final scope. |
| `db52047` | Useful fault evidence but wrong Kubernetes-oriented recovery guide. Rewrite active guidance, retain history. |
| `eab3be3` | Tested oversized-WARM error classification fix; keep. |

Specific wrong active artifacts:

- `deploy/cubesandbox-tiered-direct-memory.patch`: hunks for
  `deploy/kubernetes/chart/files/cube-master/conf.yaml`,
  `deploy/kubernetes/chart/templates/_helpers.tpl`, and
  `deploy/kubernetes/chart/templates/node-daemonset.yaml`.
- `deploy/cubesandbox/cube-node-startup-timeout-patch.yaml`: old Pod startup
  patch, modified at 6a8294a. It originally existed before this task.
- `docs/tiered-oracle-recovery.md`: Pod readiness, kubectl/DaemonSet, and old
  API/proxy port instructions must not remain the active resumption path.

The general CubeSandbox semantic endpoint/forwarding patches are not identical
to these Kubernetes-specific changes. Inspect hunks; preserve standalone needs.
`prepare-semantic-source.sh` prepares the four base patches, not a fully proven
tiered release. The large tiered patch contains unrelated inherited changes;
do not apply it blindly to a fresh v0.7.0 checkout.

## 7. Evidence inventory and historical clutter

### Keep these archives: verified local copies

All paths below are relative to `C:\Users\29068\Desktop\ClawBox\.worktrees\`.

| File | SHA256 |
| --- | --- |
| `clawbox-tiered-evidence-20260907-recovery.tar.gz` | `0499554c56440146d89df2811376a75c4421969d544a7deb8def594b2a83da26` |
| `clawbox-tiered-recording-20260907.tar.gz` | `0d69f6cf77d71c7723704884f0fecd0134a91a398fa197ea3072fdbef70e47ee` |
| `cubesandbox-tiered-094daaa-source.tar.gz` | `270ec346a9a7d2d42304b11e70b3c653cf41c1333349a66da37b4c4d7df526e1` |
| `host-kernel.log` | `62fbbfafe38fe9c3a4f95a4eff9a8af026933b1d1c0d0edbdfdf6c5969964fa0` |
| `remote-unit-tests-753de20-r2.log` | `3a3ed778ecbb161232a9a064caf28fab082c2fe632ff7439eec160feb5317278` |

The first archive contains the study directory, including failed attempts and
deployment evidence (151 entries at creation). Later kernel/test logs are
separate. The second contains original recording-output, recording-evidence,
selected-traces, and prediction inputs. The third is tracked CubeSandbox source,
not binaries or complete Git history. These archives are not a complete backup
of every historical host service log, VM disk, or registry volume.

Remote study: `/home/weitianc/clawbox-tiered-study-20260907/`.
Failed gate attempts are under `raw/gate-c1-20260907-r1` through `r7`, with
associated console logs where available. Failures included template/provenance
issues, request mismatch, kernel/component startup, policy auth, and TCP
identity errors. r7 arm ID begins `4c25e1a821b69f0fdd1cc7f4`; cleanup failures
can obscure the first causal failure, so read event/console logs too.

Remote unit tests at source 753de20 ran:

```text
tests/test_snapshot_pool.py
tests/test_policy_v2.py
tests/test_experiments_v2.py
tests/test_policy_ssh.py
```

`remote-unit-tests-753de20.log` is the failed missing-pytest attempt; `-r2.log`
is passing. The newer capacity regression logs are in local
`.worktrees/warm-capacity-validation-20260907/` and remote
`/home/weitianc/clawbox-capacity-validation-20260907-icc9id/evidence/`.
The regression failed before the fix and pool/policy suites passed afterward.
Earlier Windows tests are not substitutes for the user's remote validation rule.

Historical c1 success:
`/home/weitianc/clawbox-results-current/smoke-v5-c1/arms/84972f9c1019368d387a0d7c.json`.
It reported one completed session, validation pass, join 1.0, telemetry loss 0,
about 68.96 s, but used a tiny two-model-step smoke trace. Old templates were
Runtime `tpl-ec97143fa76e409981055c2f` and Tool `tpl-72f8a42d8fe746279d0bb80a`,
with an older 6.6.119 guest component `sha256-f84e3fa28ae6`. A later strict TCP
gate with the old pair also failed. Reuse only after proving compatibility.

### Clutter is not blanket deletion permission

| Location/category | Handling |
| --- | --- |
| `.worktrees/CubeSandbox-tiered` | Dirty source with work to preserve; not disposable just because of the directory name. |
| `.worktrees/CubeSandbox-patch-check`, `kernel-patch-check` | Likely inspection scratch; inspect status and unique files before removal. |
| `.worktrees/clawbox-tiered-study-20260907` | Partial direct SCP extraction; some long paths failed. The checked archive is the complete transfer of that snapshot. |
| `.worktrees/*.sh`, `map_debug.py`, `guest_exec.py`, `route_test.sh`, `official-startup-patch.yaml` | Diagnostic helpers, some for the wrong K8s route. Do not execute as setup automation. Some can create sandboxes or contain ephemeral auth material. |
| `.pytest-*`, `.pytest_cache` | Numerous local test scratch directories, some permission-denied. Do not widen permissions or recursively delete the workspace to clean them. |
| `deploy/clawbox-tiered-*.bundle`, `deploy/cubesandbox-64102d9.bundle`, `deploy/cubesandbox-existing-20260907.patch` | Transfer/source backups; check uniqueness and replacement archives before cleanup. |
| `.tmp-clawbox-d379186.bundle` | Historical tracked bundle; not automatically safe to delete without considering repository cleanup scope. |
| `deploy/rec-a-enriched.inspect.jsonl` | Diagnostic local trace copy; never replace the authoritative frozen trace with it. |
| `results/`, `release-evidence/`, `.artifacts/`, `.clawbox-workspaces/`, `clawbox.db`, `.venv/` | Existing data/environments of mixed ownership; inventory, do not assume garbage. |
| Remote `/data/cubelet`, `/data/log`, `/usr/local/services/cubetoolbox`, registry volumes | Shared infrastructure/data; no broad deletion, unmounting, or overwrite. |
| Remote original ClawBox/CubeSandbox trees, old templates and kernels | Preserve until provenance/consumers are known. Versioned reuse is preferable to reconstruction. |

A historical unrelated Firecracker PID 392065 and parent 391856 were observed;
do not kill by stale PID. A user SSH tunnel previously existed but no ssh.exe
was present at handoff. Check fresh ownership instead of blindly killing or
restarting a remembered tunnel. Do not stop unrelated host services without
assessing scope and impact.

## 8. Successor execution plan with stop/go gates

### Milestone A: establish the correct starting point

1. Read applicable AGENTS instructions, this handoff, and the full original
   plan with the overrides in section 1. Inspect local/remote dirty state.
2. Correct the Kubernetes-only deployment changes selectively. Preserve fault
   evidence and valid policy/backend changes. Commit and push.
3. Through `ssh kunpeng`, check current boot, kernel journal, service health,
   mount/storage topology, and background workloads. If the prior host fault
   remains, obtain administrator recovery using the existing reboot approval.
   Do not keep deploying into a damaged kernel. Do not claim a reboot occurred
   from an SSH disconnect: verify a changed boot ID and healthy services.
4. Inventory standalone CubeSandbox services, release bundles, databases,
   registry, templates, and process paths. Reuse verified assets. The absence
   of a standalone inventory in this handoff is not proof that none exists.

Safe read-only starting commands (adapt output size; no secrets):

```powershell
Set-Location C:\Users\29068\Desktop\ClawBox
git status --short
git log -5 --oneline
ssh kunpeng "uptime; cat /proc/sys/kernel/random/boot_id; uname -r"
ssh kunpeng "systemctl list-units --type=service --all --no-pager"
ssh kunpeng "ps -eo pid,ppid,stat,comm; ss -lnt"
```

Inspect service definitions/listeners before picking API ports. Use bounded
commands and check individual exit codes; a compound shell command may return
success after an earlier timeout. Do not output entire credential-bearing env
files, Kubernetes Secrets, model requests, or private keys.

### Milestone B: isolated standalone deployment

Use a versioned checkout/release and matching SDK, with explicit provenance.
Do not overwrite dirty original trees. Follow the supported bare-metal/one-click
route in `docs/cubesandbox-setup.md` and the CubeSandbox source's deployment
instructions after auditing them for the selected revision. Reuse the known
builder and cached artifacts where valid; do not rebuild all templates/kernels
without a demonstrated incompatibility.

Port the required direct memory-snapshot capability into the standalone release
without the mixed Helm hunks. Verify actual API binary, Cubelet, embedded BPF,
SDK, kernel component and image digests. Freeze the machine configuration in a
protected file with explicit listeners and routable endpoint address, not Pod
IPs or the historical 30030/30080 assumptions.

Go gate: one sandbox creates, runs, stops, and cleans up; native Runtime-to-Tool
SSH and identity pass; checkpoint/restore retains the correct workspace and
returns a refreshed semantic endpoint. No recurrent host lockups.

### Milestone C: complete the implementation, not just test scaffolding

Fix the section 4 gaps with deterministic regressions on kunpeng. Audit per-role
state, reservation conservation, partial failures, response/checkpoint races,
restore/spill locking, oversized snapshots, and live-mode rejection. Preserve
existing policy semantics and do not add emergency policy overrides silently.
Prove storage dependencies and enforce actual LOCAL/WARM placement/capacity.
Capture backend and physical evidence for both transition chains.

Relevant source map:

| Area | Files |
| --- | --- |
| Specs/recipes | `clawbox/experiments/spec.py`, `spec_types.py`, `baselines.py` |
| Decisions/coordination | `clawbox/experiments/policy.py`, `worker.py` |
| WARM ledger | `clawbox/experiments/snapshot_pool.py` |
| Measurement | `clawbox/experiments/memory.py` |
| SDK/lifecycle | `clawbox/cube/client.py`, `lifecycle.py` |
| Replay | `clawbox/experiments/openclaw_driver.py`, model gateway/replay modules |
| Auth/Tool setup | `scripts/clawbox-policy-ssh.py`, Tool bridge setup code |
| CubeSandbox direct storage | `Cubelet/services/cubebox/pause_cow.go`, `relocate_snapshot.go`, `template_ops.go`, SDK/API changes in the source archive |
| Tests | `tests/test_cube.py`, `test_policy_v2.py`, `test_snapshot_pool.py`, `test_numa_memory.py`, `test_experiments_v2.py`, `test_policy_ssh.py` |

### Milestone D: c1/c4 gates and protocol freeze

Pass a COMPLETE selected rec-a c1, including strict request matching, preserved
responses/timing, native execution joins, final task validation, and cleanup.
Then pass both LOCAL->WARM->LOCAL and LOCAL->WARM->COLD->LOCAL chains with
content/identity checks and measured placement. Include Runtime LOCAL while
Tool is nonresident. At c4, force WARM overflow and verify LRU, reservations,
restore/spill serialization, response-ready races, no duplicate commands, and
no owned sandbox leaks. Validation-only controlled waits/smaller pools may
force boundaries; keep them separate from the frozen formal trace.

Example endpoint gate, only after standalone env/templates are verified:

```bash
.venv/bin/python scripts/validate-cubesandbox-tcp-endpoints.py \
  --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" \
  --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
  --node "$CUBE_NODE" --control-host "$CLAWBOX_CONTROL_HOST" \
  --count 1 --output "$STUDY_ROOT/gates/endpoint-c1-UNIQUE.json"
```

Use count 4 for the corresponding gate, not 8. This script alone does not prove
the complete tier/storage/replay contract. Inventory its actual checks.

Freeze baseline/input hashes, CPU/NUMA layout, capacities, headroom, arrival
schedule, cache/reset policy, random order, timeout, metric definitions, and
provenance. Measure runtime/storage needs before scheduling c40. Configure
WARM as disabled for the old policies and enabled for A/B under a common
resource definition; verify the worker actually implements that distinction.

### Milestone E: all 13 formal c40 outcomes

Run arms sequentially on the host, one formal run each, in the frozen randomized
order. Record every attempt, unique IDs, ownership and cleanup. Check source,
trace/template/prediction hashes and host idle state between arms. Do not reuse
an old success marker after changing implementation or inputs.

The CLI entry point is verified from current source:

```bash
.venv/bin/python -m clawbox.experiments.worker \
  --spec examples/experiments/tiered-oracle-rec-a-c40.yaml \
  --run-id UNIQUE_RUN --attempt-id UNIQUE_ATTEMPT --task-uid UNIQUE_TASK
```

This is a command shape, not authorization to run before gates. Check `--help`
and worker output-root semantics on the final build. Configure the verified
standalone API/SDK environment first. Capture stdout/stderr and manifests.
Do not silently skip poor policies. Genuine policy-induced OOM/admission
timeouts remain results; infrastructure invalidity requires a separate label.

### Milestone F: measured results and a human setup guide

Produce one study directory:

```text
provenance/  workload/  configs/  raw/  per-run/  aggregate/  plots/
report.md   manifest.sha256    analysis/reproduction scripts
```

Required measured outputs include correctness/completion counts, throughput,
per-run JCT distribution, admission/response holds, memory occupancy and
residency integrals, per-tier migration latency/bytes, WARM hit/spill rates,
SSD traffic where measured, telemetry coverage, and failure accounting.
Use a declared rule for representative timelines. Rebuild tables/plots from
raw data and cross-check counts/bytes/manifests. Do not hand-enter results.

Definitions: JCT starts at scheduled arrival and ends at correctness-validated
completion. Throughput window runs from first scheduled arrival to last
completion; report setup/teardown separately. c40 means 40 submitted sessions,
not 40 simultaneously resident pairs. Report actual concurrency. WARM hit rate
is successful WARM restores / successful WARM+COLD restores, by role and total.
Spill rate is distinct WARM generations successfully spilled / generations
admitted to WARM; direct-to-COLD is separate. Zero denominators are N/A.
Operation bandwidth is transferred bytes/service time; include queueing and
readiness separately in end-to-end latency. Node deltas are not isolated
per-operation reclaimed memory. Physical-byte occupancy integrals and logical
snapshot residency must remain distinct.

One run per policy cannot estimate run-to-run variance. Forty sessions are not
forty independent experimental repetitions. The study covers one repository
trace and emulated NUMA pooled memory, not measured CXL/UB hardware. No claim
of maximum concurrency, general optimality, or unmeasured diminishing returns.
Explain unsupported original RQs/plots when sensitivities/controls are absent.

A practical report structure, proposed here rather than claimed to reproduce
the unavailable brief, is: scope/questions; workload/provenance; platform;
policies; protocol/gates; main results; migration/memory/I/O analysis; failures;
limitations/RQ answers; reproduction and artifact inventory.

The final human setup guide must cover prerequisites, privilege requirements,
supported standalone installation, exact versions/patches, registry and
template creation/reuse, actual endpoint configuration, NUMA/cgroup/tmpfs and
cache setup, c1/c4 gates, c40 commands, expected outputs, rollback/recovery,
cleanup ownership, and common errors. Verify the documented sequence on the
actual final deployment; do not present this handoff's unverified candidates
as a tested installation recipe.

## 9. Operating discipline and completion audit

- Inspect state before acting; use isolated versioned deployments and preserve
  user changes. Prefer `rg`; remote `rg` may be unavailable, so use grep/find.
- Use `apply_patch` for local edits. Avoid destructive Git commands, broad
  recursive deletion, unresolved globs, or shell command construction from data.
- Batch independent reads; keep dependent edits/deployments/tests sequential.
  Keep status updates short and roughly once per minute while working.
- On Windows, validate literal absolute paths before deletion/moving; never
  compose destructive operations across shells or repurpose HOME variables.
- Never dump secrets in logs or public Git. Old raw deployment/request evidence
  may contain credentials. Archive it privately; publish redacted provenance.
- Use `python -m pip`, not stale copied launchers. The prior SOCKS dependency
  issue was resolved using the direct package index with proxy env vars unset;
  inspect the environment rather than blindly copying that workaround.
- Capture build/test output to files from the start. Do not call compilation
  or a schema test a passing end-to-end replay.
- User approval to reboot persists; availability of admin credentials does not.
  Do not assume a sudo password or ask for it in chat. Do not force-reboot via
  sysrq as an unreviewed substitute for ordinary reboot.
- User rejected Kubernetes reintroduction, not all historical evidence. Avoid
  deleting unrelated legacy modules/infrastructure merely for keyword matches.
- If host or invariant prerequisites cannot be met, report exact commands,
  failures, and evidence. Do not fabricate results or silently alter the study.

Before declaring completion, verify every baseline outcome, c1/c4 gate,
placement/capacity invariant, trace/input hash, raw artifact, generated metric,
report conclusion, and human reproduction step against authoritative evidence.
Implementation commits, passing unit tests, and a polished report with missing
measurements are insufficient. The task is complete only with the agreed
measured c40 package and verified setup documentation. If an external blocker
requires the user's action, report it explicitly; a blocker is not success.

The previous thread's goal was marked blocked, not complete. This handoff does
not restart experiments or change that claim. The successor should use its own
task tracking and the user's assignment, not any old automatic continuation.
