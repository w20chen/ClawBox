# Agent Handoff: Tool-VM Telemetry T2-T4

Date: 2026-08-20

This handoff is self-contained. Read it with
`docs/NEXT_PLAN_TOOL_VM_TELEMETRY.md`; older G0 handoffs are historical. The
program goal is T0-T4. T0-T4 are complete; managed sandbox convergence is
next.

## Repository state

ClawBox branch `main`:

- T0: `0bae91a`.
- T1: `9c26605` (last completed milestone).
- `bae0752` preserved the incomplete T2 takeover state.
- The next milestone commit completes T2 with the production-image fixes and
  the real-machine evidence below.
- `daf6654` completed and pushed T2.
- `507dfd2` implements the accepted T3 native ingestion path; the following
  documentation commit records its real-machine evidence.
- `80aca94` records and pushes the T3 production evidence.
- `99f33b8` implements the accepted T4 native shadow feedback loop.

Sibling ClawTune branch `v2` (all pushed):

- `3cb0793`: honest resource source provenance (T0).
- `fcdec7c`: native explicit-cgroup guest smoke (T1).
- `65077f9`: authenticated guest-local collector service.
- `fdc9013`, `7ff4d6d`: BCC tracefs normalization.
- `a493ab7`: mount a legacy tracefs view.
- `984861a`: overlay a masked guest debugfs (latest).
- `e91e60b`: mount the legacy libbcc tracefs view over a private tmpfs
  (production T2 fix, pushed).

ClawTune was clean except for the pre-existing untracked `.tmp/` after
`e91e60b`. Its remote warns that the repository moved
to `git@github.com:w20chen/ClawTune.git`, although the configured URL still
accepted pushes.

## What incomplete T2 implements

- `toolbridge/guest_collector.go`: authenticated versioned Unix-socket client
  and collector supervisor. It uses a random 32-byte token and supports health,
  begin, finish, abort, and shutdown.
- `toolbridge/main.go`: observe-before-exec gating, finalization before cgroup
  cleanup, and explicit fail-open telemetry state. Exit codes, timeout 124,
  output framing, and SSH behavior are preserved.
- The gate starts `/bin/sh -lc` before attribution so login-profile helpers are
  excluded, then releases `exec /bin/sh -c "$CLAWBOX_GATE_COMMAND"`. Including
  profile helpers made ClawTune artifacts ineligible.
- `toolbridge/collector.go`: exclusive non-root per-execution cgroup-v2 leaves.
  It does not write subtree controllers, which Kata does not delegate here.
- ClawTune `tools/guest_collector_server.py`: authoritative native collection
  inside the Tool container and therefore against the Firecracker guest kernel,
  never the physical host kernel.
- `docker/Dockerfile.swe-rebench-tool-telemetry`: Ubuntu 22.04 production
  overlay with distro BCC, exact guest-kernel headers, pinned ClawTune source,
  helper, validator, and bridge.
- ClawTune is imported from pinned source using `PYTHONPATH`. Do not restore the
  removed `pip install`: it needlessly resolved the complete sidecar dependency
  graph and hung on external package access. Standalone ClawTune packaging was
  not changed.
- `scripts/rebuild-swe-rebench-tool-overlay.sh`: accepts and validates an exact
  `CLAWTUNE_REVISION`, embeds it as an OCI label, verifies ARM64, pushes, and
  prints the immutable digest.
- Tool Pods use `kata-fc-arm64-ebpf`; Runtime Pods remain on
  `kata-fc-arm64`. Tool capabilities are `SYS_ADMIN`, `NET_ADMIN`, `NET_RAW`,
  and `SYS_PTRACE`.
- `scripts/runtime-entrypoint.sh` retrieves both `cgroup-resource-*.json` and
  `clause-telemetry-*.json`.
- `scripts/run-toolbridge-ebpf-integration.sh` runs a real SSH bridge workload:
  CPU-heavy command, pipeline, exit 7, timeout 124, two concurrent commands,
  and forced helper death/fail-open.
- `scripts/validate-toolbridge-guest-artifacts.py` requires seven execution
  records, controlled native artifacts, distinct concurrent cgroups, zero loss,
  cleanup success, nonzero CPU/RSS, no raw command leakage, and explicit
  fail-open state.

## Verified evidence

- RuntimeClass: `kata-fc-arm64-ebpf`.
- Guest kernel: `6.18.28`.
- Kernel source SHA-256:
  `f360789483586cf8a20b4ab2bffe76ead6b62c0db1eeb0d917294456c4d77b74`.
- Immutable SWE-Rebench base:
  `127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:d424fb2440bbf8d055f8846d6ba783fb558a8185d5c6436c3260e627209f611a`.
- T1 native smoke passed in real ARM64 Firecracker: native BPF compile/attach,
  nonzero CPU/RSS, zero loss, valid integrity, cleanup, and an eligible call.
- The T2 research image passed the full bridge acceptance: seven executions,
  isolated concurrent cgroups, about one CPU core and 2.34 MB peak RSS for the
  long command, six valid native artifacts, zero loss, cleanup, and explicit
  helper fail-open.
- Focused ClawTune guest/telemetry suite after `984861a`: 43 passed on ARM64.
- Earlier broad ClawTune run: 607 passed and one pre-existing ARM-sensitive
  strict-eBPF test failed outside this guest service.
- ClawBox Kubernetes backend tests: 20 passed.
- Native ARM64 `go test -race ./...`: passed.

## Resolved production failure history

The last completed production image is:

`127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:c61a31898ce742919fb582a32942afc74e3656bb92aa2f07313195c4306e4e2a`

It embeds ClawTune `a493ab7e6e97ad017a472f38eedc08cd7842e294`.
The workload passed, including exit 7 and timeout 124, but the helper failed:

```text
PermissionError: [Errno 1] Operation not permitted:
'/sys/kernel/debug/tracing'
```

Root-cause history:

1. Canonical `/sys/kernel/tracing` mounts and contains the tracepoint.
2. Ubuntu 22.04 BCC 0.18 still opens the legacy
   `/sys/kernel/debug/tracing/events/sched/sched_process_exit/id` from libbcc,
   even when Python `bcc.TRACEFS` is corrected.
3. `a493ab7` treated `Path('/sys/kernel/debug').is_mount()` as readiness. Kata
   exposes an unusable read-only mask there, so the helper skipped overlaying
   debugfs and could not create `tracing`.
4. ClawTune `984861a` now uses presence of the required tracepoint as readiness
   and overlays debugfs otherwise. Unit tests pass. Its production build was
   interrupted for this handoff before producing a digest, so it is not proven.

The takeover reproduced two further strict-gate failures without weakening the
validator:

1. Image `sha256:e7c92d3b6657c8b206116464b2e35f6acd783f480442176ed5d8c63fb54eb1fd`
   pinned `984861a`. `mount -t debugfs` returned zero, but debugfs rejected
   userspace creation of `tracing` with `EPERM`.
2. A retained debug Pod proved that a private tmpfs at `/sys/kernel/debug`
   followed by tracefs at `/sys/kernel/debug/tracing` exposed sched tracepoint
   ID 197 and allowed BPF collection. ClawTune `e91e60b` implements that shim.
3. Image `sha256:b494eaf8a9cce3dcc849c45ce23796a2e6b80588b4f0b6662867e732e6ae40d9`
   then reached native finalization but exposed two source-only overlay gaps:
   NumPy was absent and the CRLF mvdan builder was not prebuilt. The production
   Dockerfile now installs distro NumPy, normalizes the builder in the image,
   and prebuilds its pinned Go 1.26.1 / mvdan v3.13.1 adapter into an immutable
   cache. Standalone ClawTune packaging is unchanged.

Do not weaken the validator and do not call fail-open execution a telemetry
success. The final production image satisfies the original strict gate.

## T2 production acceptance (completed 2026-08-20)

- ClawBox source: `bae0752` plus the production packaging fix in the following
  T2 milestone commit.
- ClawTune source: `e91e60bc1e5f3209fbcf6091013fde96f217e2a7`.
- Base image:
  `127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:d424fb2440bbf8d055f8846d6ba783fb558a8185d5c6436c3260e627209f611a`.
- Accepted production Tool image:
  `127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:1e4db5fefd5b3285bcecf432edfe1cf09335e7004a468e46aa2a044365ec3a36`.
- RuntimeClass / guest: `kata-fc-arm64-ebpf`, ARM64 kernel `6.18.28`.
- Strict integration: `INTEGRATION_RC=0`; seven bridge executions; six native
  artifacts; long-command peak CPU `1.000000268` cores and peak RSS
  `2.27328` MB; loss zero; cleanup `ok`; native validity and KB eligibility
  true; concurrent cgroup IDs `1766` and `1781`; exit 7 and timeout 124
  preserved; forced helper failure remained explicit and fail-open.
- Archived log: ignored local artifact
  `.artifacts/clawbox-t2-production-e91e60b-r2.log`, SHA-256
  `ee6803aca7561d23e9e5df1bd6b816572472ff268b95b6d5806629392454b02c`.
- Post-acceptance tests: focused ClawTune ARM64 suite `52 passed`; ClawBox
  Python suite `167 passed`; native ARM64 `go test -race ./...` passed in
  `6.928s` (Go 1.25 container, `GOPROXY=https://goproxy.cn,direct`).

## Shortest takeover sequence

Remote: `weitianc@193.124.7.2` (SSH key, partial sudo). It has a stale SOCKS
proxy; use `git -c http.proxy= -c https.proxy=`. Do not delete unrelated pods.

Remote isolated paths:

- ClawBox build tree: `/tmp/clawbox-t0-20260820`.
- Exact ClawTune `984861a` archive tree: `/tmp/clawtune-t2-984861a`.
- Registry: `127.0.0.1:5000/clawbox`.

The interrupted `t2-handoff-final` Docker processes were explicitly stopped.
Rebuild the exact source:

```bash
CLAWBOX_ROOT=/tmp/clawbox-t0-20260820 \
BASE_IMAGE=127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:d424fb2440bbf8d055f8846d6ba783fb558a8185d5c6436c3260e627209f611a \
CLAWTUNE_ROOT=/tmp/clawtune-t2-984861a \
CLAWTUNE_REVISION=984861a6bec50e397cc3f31cfec9bb2fd8c79880 \
TAG=t2-984861a \
/tmp/clawbox-t0-20260820/scripts/rebuild-swe-rebench-tool-overlay.sh
```

Run the printed immutable digest, not the tag:

```bash
cd /tmp/clawbox-t0-20260820
CLAWBOX_EBPF_IMAGE='<printed image@sha256:digest>' \
CLAWBOX_EBPF_NAMESPACE=clawbox-t2-production-984861a \
./scripts/run-toolbridge-ebpf-integration.sh
```

If it still fails, retain a purpose-built debug Pod and inspect mountinfo and
the mount return code. Likely alternatives are mounting tracefs directly over
an existing legacy mountpoint, adding an OCI/Kata mount exposing canonical
tracefs at the legacy path, or using a newer BCC package/backend in ClawTune.
The solution must stay inside the Tool VM guest and preserve standalone
ClawTune behavior.

After acceptance, rerun focused ClawTune tests, the ClawBox Python suite, and
ARM64 Go race tests; update the plan; then commit/push completed T2.

## T3 production acceptance

T3 passed on `weitianc@193.124.7.2` in isolated worktree
`/tmp/clawbox-t34-daf6654`; the older dirty handoff tree was not modified.
The production Tool image remained
`127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:1e4db5fefd5b3285bcecf432edfe1cf09335e7004a468e46aa2a044365ec3a36`.
The pinned ARM64 control-plane image was
`127.0.0.1:5000/clawbox/control-plane-arm64@sha256:72907f6203e03a9ba1c2616814b2fea540fa6c5d9efcb4df464ce190d707230e`.
Both record ClawTune `e91e60bc1e5f3209fbcf6091013fde96f217e2a7`.

The strict Firecracker run again passed with seven executions, six native
clause artifacts, non-zero CPU/RSS, zero loss, successful cleanup, distinct
concurrent cgroups, and native artifact validation. T3 preserved the complete
signed raw set for audit, rejected its unpaired fail-open member without
weakening validation, and accepted five exact clause+cgroup pairs (ten
artifacts). The accepted result was:

- generation `0 -> 1`;
- manifest digest `9894010730ee300d710668124eafbdb235b4cd9b870c9f9df21faeac145eae3e`;
- reproducible source digest `67beee6b1274859fa3e9c747de73884bfddfe3c790e5a6895d0361fda8012fcb`;
- atomic pair digest `b9214c1b54d7c4dab60eb100ccdc9fbbbbd66ea4b216169c5a6a622b5e09c2e6`;
- exact replay idempotent, cross-tenant signature reuse rejected, both pinned
  native readers passed, and rollback restored generation 0.

Remote evidence:

- `.artifacts/t3-native-acceptance-r3.json`, SHA-256
  `7a5120eb40e3408c191b97246829f726e8476b9a58b89ab2990f8ed0f6de39cb`;
- `.artifacts/t3-native-ingest-final-r3.log`, SHA-256
  `c179a39caa0dbbdce8edb5e47ba1751da2e33e198b567b0c7132d5a725746b28`;
- `.artifacts/t3-native-run-a-held.log`, SHA-256
  `06ca692c7e608b0ef6c9af9e23f9562a7b5c53d89a0c6b55d70a5c3db6a28784`.

The full ClawBox Python suite passed locally and natively on ARM64; the T3
focused native suite passed against the archived exact ClawTune source.

## T4 production acceptance

T4 passed on `weitianc@193.124.7.2` in the same isolated task worktree. The
production Runtime image is
`127.0.0.1:5000/clawbox/runtime-arm64@sha256:6215a4b10feec3ba8d6dcd39e763b92322fe7605dba62c39bcdae8bc71d04e44`,
labeled with exact ClawBox
`99f33b86c0af38a19706ce76633bfcbe44c9c1c0` and ClawTune
`e91e60bc1e5f3209fbcf6091013fde96f217e2a7` revisions.

Run A advanced native generation `0 -> 1`. Run B loaded exactly generation 1
and atomic pair digest
`b9214c1b54d7c4dab60eb100ccdc9fbbbbd66ea4b216169c5a6a622b5e09c2e6`,
then produced a native `repo:exact_clause` prediction with evidence count 1,
continuous `repo:exact_command` estimates, and explicit evidence lineage
`[run-a]`. A fresh strict production Tool-image run inside Ubuntu 22.04 plus
Kata/Firecracker passed with zero loss, cleanup success, native validity,
non-zero CPU/RSS, and distinct concurrent cgroups. The joined `exec-long`
actual was 4600.499 ms, 0.352865 average cores, and 1,531,904 bytes peak RSS
from cgroup-v2.

The report explicitly retains `FixedProfileSizer` as the authoritative sizer,
uses shadow mode, and states that predictions do not control resources.
Evidence in `/tmp/clawbox-t34-daf6654`:

- `.artifacts/t4-run-a-ingest.log`, SHA-256
  `f20c38b8b8b89bc268bd3187c52a25e089bc42c25d7b87771f25b9d5b533d412`;
- `.artifacts/t4-run-a-snapshot.json`, SHA-256
  `5f100b94d3a18bdb1691853518fda3d7232645320cbba0595b86f04630f7a7b2`;
- `.artifacts/t4-run-b-load.log`, SHA-256
  `2ec3b921fe65a491f8b2de0709ec79fe61e4a4c662256ce6b6f1c1d5d9cee1f3`;
- `.artifacts/t4-run-b-probe.log`, SHA-256
  `ee442ee0e69df4e576e40175bb115138c7fbf3c32d09fdc6ed445335f5db6f49`;
- `.artifacts/t4-run-b-firecracker.log`, SHA-256
  `bdcc8a22fd099f17fda7e4ff6cf16f50a593c8b1c04df4172a028f7056319f13`;
- `.artifacts/t4-run-b/native-shadow-report.json`, SHA-256
  `527f5b0897a01451feb36593f65eae87fe6f09ca30598a224d57032146556658`.

Post-acceptance ARM64 validation passed: exact focused ClawTune suite `52
passed`, full ClawBox suite `176 passed`, and native `go test -race ./...`
passed in 6.937s. T0-T4 are complete. Learned resource control remains out of
scope until its safety gates pass; managed sandbox lifecycle convergence may
now resume.

## Incomplete T2 file inventory

- `clawbox/cell/app.py`, `clawbox/cell/manifests.py`, `clawbox/common/config.py`
- `deploy/runtimeclass-firecracker-ebpf.yaml`
- `docker/Dockerfile.tool-bridge`, `docker/Dockerfile.tool-telemetry-research`
- `docker/Dockerfile.swe-rebench-tool-telemetry`
- `scripts/build-tool-telemetry-research.sh`
- `scripts/rebuild-swe-rebench-tool-overlay.sh`
- `scripts/run-toolbridge-ebpf-integration.sh`
- `scripts/runtime-entrypoint.sh`
- `scripts/validate-toolbridge-guest-artifacts.py`
- `tests/test_kubernetes_backend.py`
- `toolbridge/collector.go`, `toolbridge/guest_collector.go`
- `toolbridge/guest_collector_test.go`, `toolbridge/go.mod`, `toolbridge/go.sum`
- `toolbridge/main.go`

Generated `.artifacts/` archives are ignored and must not be committed.
