# ClawBox continuation handoff

Updated 2026-09-04 after the post-reboot acceptance milestone, with live
Kunpeng evidence. The source commit is recorded in the repository history.

## Latest clean stopping point: managed c1 readiness

This handoff is intentionally at the next important milestone, before any
formal paper matrix. The latest pushed source is `386170a`. It adds the
task-namespace Service permissions required by the controller's NodePort
adapter and sanitized top-level Worker exception logging. Local targeted
managed/controller tests and `git diff --check` pass; the full suite had
already passed with five environment skips before this small change.

The live controller on Kunpeng is healthy and uses control-plane image digest
`sha256:2b3814b06de6da7ef00b86201db0cbcfc23061a6fe441ef0520146bae4e0a1d5`.
Applying the updated `deploy/control-plane-rbac.yaml` made these checks pass
for `system:serviceaccount:clawbox-system:clawbox-cell-controller` in
`clawbox-benchmarks`: Service `get`, `create`, and `delete`. A fresh task then
created its two-port NodePort Service and Worker Job through the normal
controller path, proving the RBAC defect was real and is fixed.

The temporary synthetic Worker attempt used Worker digest
`sha256:086f6556f345f76e2c763b3a77f4f53a0de7e3b3c05527f1c5d48d01063475b6`.
It failed in pre-arm readiness before writing evidence; the prior image did
not emit a useful top-level exception, which is why `386170a` adds sanitized
failure logging. Its rebuild was deliberately stopped, so do not claim that
this is a valid managed c1 result and do not infer the exact readiness cause
from the silent old-image exit. The temporary task, failed Job, mock Pod,
mock Service, credential Secret, and task-owned NodePort Service were removed.
The result directory was left untouched; no persistent results, `/data/cubelet`,
MinIO/S3 data, templates, kernel artifacts, or validated services were
deleted or restarted.

The live namespace retains only the pre-existing historical `cube-cancel-smoke`
and `cube-vertical-slice` task records/worker result, which were not created by
this continuation and were preserved. A direct Cube inventory using the SDK
returned no Sandbox rows and listed the existing templates, including the
fresh Runtime `tpl-72fecb8e388d4c9fa3a61054` and Tool
`tpl-4ffe6e6abd574be99b2869e1`; the active guest-kernel hash remains
`f84e3fa28ae692f34645aa3c7034999242760eb25aab0ea667b43f16ac12c27f`.

## Exact next milestone

No usable real LLM credential exists on the host: `clawbox-benchmarks` has no
`clawbox-llm` Secret, and the old stored traces use `exec/read/edit` rather
than the current managed `cube_shell` contract. Do not reuse or relabel them.
The next operator must provide a task-scoped Secret with the configured API-key
environment name, then:

1. Build and publish a fresh immutable Worker image from `386170a` and record
   its digest. Do not use the interrupted build as a published artifact.
2. Create a unique real c1 `SandboxTask` using the fresh Runtime/Tool template
   IDs and the immutable Worker image. Reconcile only through the controller.
3. Require the real API record to show command-specific Runtime prediction
   metadata, actual model request count, request-event-driven policy actions,
   separate JCT/validation/hash/cleanup timing, valid Tool telemetry, exact
   execution-ID joins, correct outputs, and zero losses/leaks.
4. Freeze the real trace, immutable prediction/KB artifact, and Tool trajectory
   hashes. Replay with independent session-local state and require exact
   trajectory/output equivalence before c4, c8, or c20.
5. Preserve raw success and failure artifacts and classify evidence as live
   inference or deterministic replay on real Kunpeng. Do not start c40/c60
   until c20 meets every stated acceptance condition.

## Latest continuation status

The post-reboot reduced acceptance is now complete. The custom guest kernel was
restored idempotently after reboot, cube-node returned to `3/3 Running`, the
fresh Tool template passed the direct kprobe diagnostic, and a fresh Runtime /
Tool pair passed through the node-routed NodePort bridge. The pair reported
valid cgroup-v2 and native telemetry, zero loss, Tool-only pause/resume with
Runtime remaining active, and an exact bridge execution-ID join of `1.0`.
The concurrent bridge stress passed with 141 requests, head-of-line isolation,
and no logged secrets. Final owner-based cleanup found no sandboxes and no
task-owned NodePort Services.

This closes the post-reboot kernel/template/bridge/telemetry acceptance
milestone. It does not claim that the full paper experiment matrix is finished;
the next gate is the semantically correct managed OpenClaw measurement path,
followed by the higher-level Runtime-to-Worker-to-Tool experiment matrix,
real/replay inference comparison, policy-arm results, and final statistical
report.

## Final goal and non-negotiable design

ClawBox evaluates memory admission and reclamation policies while coding agents
run in CubeSandbox ARM64 microVMs on Kunpeng 920B. CubeSandbox is the only
supported sandbox backend.

- one Attempt = one SandboxTask = one Kubernetes ExperimentWorker Job;
- one arm = one in-process PolicyCoordinator;
- one logical Agent = one in-process AgentPairSession = exactly one Runtime
  CubeSandbox plus exactly one Tool CubeSandbox;
- Kubernetes places the Worker; the Worker pins both VMs to that node and uses
  the official CubeSandbox SDK;
- the controller remains a thin SandboxTask-to-Job reconciler;
- the Worker orchestrates but does not run OpenClaw or repository commands;
- Runtime runs OpenClaw, ClawTune prediction/model instrumentation, and
  `cube_shell`; it does not own or modify the task repository;
- Tool owns `/workspace`, runs every repository command in a dedicated guest
  cgroup, and collects cgroup-v2 plus eBPF telemetry; it does not run OpenClaw.

Every real tool call follows `Runtime OpenClaw -> cube_shell -> Worker bridge ->
PolicyCoordinator -> Cube SDK -> Tool VM`. One exact `execution_id` joins the
Runtime span/prediction, policy charge/wait, Tool result, cgroup/eBPF records,
and pause/restore context. Never combine the pair or collapse either VM into
the Worker.

Near-term reclamation is explicitly `snapshot_pause, scope=tool`: Runtime stays
resident while an idle Tool VM pauses. Do not claim pair pause until separately
validated. Admission arms are `pair_lifetime_full`, `tool_full`, `tool_static`,
`tool_p90`, and replay-only `tool_oracle`. Charge measured physical memory plus
unrealized incremental commitments plus lifecycle headroom, without counting
realized growth twice. Cube quota is a fixed safety bound, not experiment
policy.

The paper path must run OpenClaw inside Runtime with both real API inference and
an OpenAI-compatible deterministic replay endpoint. The Worker now exposes one
fixed node-routed ModelGateway at `0.0.0.0:18081` behind a second NodePort on
the task Service. Each Runtime session has independent replay actions/cursor,
fingerprints, idempotency/delivery state, logical model-step count, and timing
records. `tool_p90` consumes command-specific Runtime prediction metadata from
the immutable ClawTune-compatible artifact and fails closed when it is absent;
it never uses the constant `cube_shell` key. Decision policies consume real
gateway request-start/response-ready events through a nonblocking per-arm event
executor, and fixed-delay timers are anchored to the request event rather than
realized future latency. Agent JCT, validation, hashing, cleanup, and
infrastructure-inclusive durations are recorded separately.

## Existing implementation

The original paired implementation landed in `111587b`; it has since been
extended through `4ade359`, `9e7084b`, `867093c`, and `386170a`. The current
source includes the fixed NodePort bridge, session-local gateway, prediction
provenance, memory-accounting safeguards, and controller RBAC/logging fixes.
Preserve the design and continue only from the c1 gate described above.

The local gate passed before this handoff: `compileall` and all pytest tests;
five were skipped and only a pytest cache warning appeared.

## Live Kunpeng state

Host: `weitianc@193.124.7.2`

- `/home/weitianc/ClawBox` is at `386170a`, with only pre-existing untracked
  results/data; preserve them. `/home/weitianc/ClawBox-cube` is the no-git live
  export.
- `/home/weitianc/CubeSandbox` is at `d0081641c59822e4e5653b7462e914410b81910a`
  with pre-existing user changes: master/chart CPU-memory ratios 1, paused
  release ratio 1, and untracked `=|e2b`. Preserve them.
- Cube node was 3/3 Running; S3lvol was active; registry mirror port 5001
  works. No additional host reboot was performed during this c1 continuation.

Published ARM64 digests:

```text
runtime original        sha256:3c83eae918b62e272e51e34d762c277068050c830a5cfc2ba7f2cb03610bcb66
tool original           sha256:68b5e12b6f570200b8ed777dd1061d13b60ccbf4116166dc1178da98b8e2fde0
worker                  sha256:f5fd49858a242efda1e0ea1cc1a896161b048e93348fc2402ad1019ccc8e6056
runtime kernel-labelled sha256:5d1ea3cee703da47b031b26d8439e240b9d39ffb978e084c482fae1e17764ca7
tool kernel-labelled    sha256:750b71f97322467a23537973c77b23160ff37d2adcdcd32aa7bba07d78c4725b
```

The Dockerfiles now accept `CUBE_GUEST_KERNEL_DIGEST` as an OCI label so a
kernel generation can deliberately change image/template cache identity.

## ARM64 kprobe kernel work

The vendor guest config had `# CONFIG_KPROBES is not set`, so ClawTune failed
closed attaching `__arm64_sys_execve`. Pinned OpenCloudOS `6.6.119-49.6` source
is at `/home/weitianc/.cache/clawbox-kernel/oc-6.6.119-49.6/source`. It was
patched per `deploy/cubesandbox/kernel-oc9-arm64-kprobes.config.patch` and built
through Cube's `scripts/build-kernel.sh` in Ubuntu 24.04 ARM64.

```text
/home/weitianc/CubeSandbox/_output/kernel/aarch64-kprobes/vmlinux
sha256:f84e3fa28ae692f34645aa3c7034999242760eb25aab0ea667b43f16ac12c27f
CONFIG_KPROBES=y
CONFIG_KRETPROBES=y
CONFIG_FTRACE_SYSCALLS=y
```

`CONFIG_KPROBES_ON_FTRACE` was requested but omitted by `olddefconfig`; basic
kprobes are the required facility. Installed layout is reversible:

```text
vmlinux -> vmlinux-bm
vmlinux-bm -> vmlinux-bm-kprobes-6.6.119-49.6-111587b  # f84e3fa...
vmlinux-bm-original-a63aa77e                           # vendor backup
version.original-a63aa77e
version.json.original-a63aa77e
```

Cube's entrypoint always resets `vmlinux -> vmlinux-bm`, hence selection must
occur through `vmlinux-bm`. Active `version` and `version.json` advertise
f84e3fa; checked-in copies are under `deploy/cubesandbox/`.

Rollback only after a zero-sandbox audit: in container `cube-kernel-install`,
replace `vmlinux-bm`, `version`, and `version.json` from their
`*.original-a63aa77e` backups, restart the exact cube-node pod, and wait for
3/3. Do not delete the custom or vendor image.

## Historical template-recovery blocker (resolved)

Old templates booted the August 12 vendor kernel and exposed no kprobe event
source. Templates built before metadata correction are rejected correctly:

```text
snapshot metadata not match: kernel version not eq:
sha256:f84e3fa... sha256:a63aa77e...
```

After metadata correction and cube-node restart, two bounded Tool template
builds failed before snapshot creation:

```text
allocate cubecow template build rootfs fail:
initialize cubecow default medium tpl-tpl-<id>-build-rootfs ...:
initExt4BlockDevice failed
```

Failed IDs: `tpl-26b825d6202d40a7b05fd701` and
`tpl-20ec6adf72764217bcb3e5c4`. About 906 GiB and 98% of inodes are free. This
repeated failure was the earlier stopping condition. It was later resolved by
the narrow host S3lvol/socket-mount recovery; the fresh Runtime and Tool
templates listed in the latest clean stopping point are now the accepted
post-kernel templates. Do not rebuild the kernel, delete `/data/cubelet`,
CubeCoW/S3 objects, MinIO, or results, or use old templates as acceptance
evidence.

`scripts/diagnose-cube-kprobes.py` creates one short-lived Tool VM and directly
tests the guest kernel interface. A post-kernel Tool template must report the
new build timestamp, `CONFIG_KPROBES=y`, a kprobe event source, and a successful
manual probe on `__arm64_sys_execve` before running ClawTune.

## Historical continuation order

1. Check node, S3lvol, and zero active sandboxes with
   `scripts/audit-cube-sandboxes.py`. The 300-second cube-node startup patch is
   applied; Cubelet may need about five minutes and several retries for 3/3.
2. Diagnose `initExt4BlockDevice failed` using Cubelet, CubeCoW, and S3lvol
   logs. Keep recovery narrowly scoped and reversible.
3. Register fresh Runtime/Tool templates from the kernel-labelled digests with
   `--probe-port 49983`. Inspect `GET /templates/<id>` and require replica
   `kernel_version=sha256-f84e3fa28ae6` before launch.
4. Run `diagnose-cube-kprobes.py`, then
   `smoke-cubesandbox-agent-pair.py` with the new template IDs and node
   `hostname-txyuq.foreman.pxe`.
5. Require `telemetry_state=complete`, exact-ID valid cgroup/eBPF artifacts,
   Tool pause/restore while Runtime stays running, and zero leaked sandboxes.
6. The remaining current work is the managed c1 record + replay gate. Follow
   the numbered steps under `Exact next milestone` above; the source-level
   implementation is ready, but real API credentials are still required.
7. Only after that gate is green: run separated admission, reclamation, and
   decision studies; randomize policy order within workload/concurrency/
   repetition blocks; preserve failed arms and label evidence type. Do not
   start c40/c60 until c20 has zero replay divergence, telemetry loss, ID-join
   failure, output-validation failure, or resource leak.

Do not call the project paper-ready until real Kunpeng evidence proves the two
VM roles, full Runtime-to-Tool path, exact-ID prediction/policy/telemetry join,
real and replay inference, model-wait policies, explicit Tool-only scope,
separate role timings, no hidden resident pause, correct memory accounting,
one Attempt/SandboxTask/Worker, thin controller, identical Cube backend across
arms, consistent docs/manifests, and zero leaks.

At handoff, the real two-VM command path, kprobe telemetry, NodePort bridge,
pause/resume, exact-ID joins, and cleanup are accepted at the reduced
post-reboot level. Higher-level managed OpenClaw behavior remains
unit/static/synthetic evidence until a real c1 record and exact replay pass.
