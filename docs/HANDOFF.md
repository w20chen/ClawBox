# ClawBox continuation handoff

Updated 2026-09-06 after the semantic CubeSandbox TCP-endpoint cutover,
native-SSH route-gate investigation, host-network datapath audit, and Kunpeng
recovery.

## Fixed direction

The operator-facing setup contract is now consolidated in
`docs/cubesandbox-setup.md`. A fresh machine must use a standalone CubeSandbox
one-click/control-compute deployment built with the checked-in semantic API
patch; an existing machine must pass the semantic c1 endpoint gate before it
can run ClawBox. `docs/kunpeng920-reproduction-runbook.md` and the Kunpeng
profile are explicitly marked as diagnostic Kubernetes material, not final
native-SSH acceptance. The README links to the new guide.

CubeSandbox is the only sandbox/multi-node substrate. One Agent is one Runtime
CubeSandbox plus one Tool CubeSandbox. OpenClaw uses its native SSH sandbox for
`exec`, `process`, `read`, `write`, `edit`, and `apply_patch`; the mutable
workspace is in Tool. PolicyCoordinator synchronously gates execution but never
proxies commands or output. ModelGateway emits request-start, generated,
released, and delivered events that drive model-wait residency. Native
OpenClaw `snapshot_pause` checkpoints both Runtime and Tool; Runtime is restored
before the response is released, and Tool is restored on the next SSH
admission. Resident arms keep both VMs running. Replay-engine compatibility
arms retain their historical Tool-only lifecycle.

Kubernetes/SandboxTask/WorkerBridge/cube_shell/direct-Firecracker paths are not
supported. Legacy files still present in the tree are deletion work, not an
alternate architecture.

## Commits and verification

- `cea2161` — synchronous, concurrent, idempotent native-SSH policy protocol;
- `49ab5a4` — managed runner cut over to native SSH, standalone CLI, explicit
  lifecycle state/timing, model lifecycle events, `cube_shell` removed;
- `de74a26` — live-discovered fix allowing Cube to build Tool templates without
  per-session ephemeral SSH keys.

Those historical commits are on `origin/main`. The local full suite now passes,
including the continuation tests. Kunpeng targeted Python tests passed.
Toolbridge Go tests passed on ARM64 in `golang:1.25-bookworm` using
`GOPROXY=https://goproxy.cn,direct`.

The continuation commits `a4be792`, `a30292a`, and `493206d` add structured
timing spans, bounded Cube command streams, and managed API-path coverage.
They are part of the fast-forward branch being published to `origin/main`.

The current native-endpoint commits are `50db717` (semantic CubeSandbox
endpoint client and initial gates), `1b79924` (synchronous per-admission route
refresh), `b3a1ee7` (long-lived OpenClaw Agent PID witness), and `147cb8e`
(SSH reaping/completion-order proof). `98d3f5d` makes the Runtime shim reject
admissions for any container port other than the Tool SSH port 2222 before
starting OpenSSH. `1903c59` records the current post-stress recovery probe;
`b076890` and `f39cfe0` bound pair creation and document its queue spans. The
companion
CubeSandbox source commit is `64102d9`, which exposes
`Sandbox.get_tcp_endpoint(container_port)` and keeps CubeMaster/CubeProxy route
metadata parsing inside CubeSandbox.

OpenClaw 2026.7.1 was inspected in the installed Runtime image. Its native SSH
backend captures `agents.defaults.sandbox.ssh.target` when the backend is
constructed and reuses that target for later sessions. The old 200 ms config
patch/watch approach is therefore not a valid refresh mechanism and has been
removed. ClawBox now resolves a semantic raw endpoint synchronously during
`/v1/tool/admit`; the admission returns `sandbox_id`, `epoch`, `host`, and
`port`, and the existing policy shim injects that route into only the current
`/usr/bin/ssh` invocation. No allocator, proxy, NodePort, Redis lookup, or
guest-IP discovery was added.

The route contract is identity-bound: stable `HostKeyAlias` and a unique SSH
host key identify the Tool, while `host:port` is treated as ephemeral. Worker
completion requires the recorded SSH subprocess to have been reaped before
completion can release the Tool reservation or trigger pause. Unit tests cover
cross-Tool rejection before subprocess launch, endpoint epoch refresh without
requiring an address change, strict host-key settings, and the OpenClaw Agent
PID surviving Tool pause/restore. A semantic raw endpoint must include its
explicit mapped TCP port; a missing port is rejected rather than interpreted as
OpenSSH's default port 22.

The same installed-source audit verified the subtle operation boundary.
ClawTune can directly envelope `exec` because it has a command parameter;
OpenClaw's filesystem bridge and backend preparation spawn SSH below the tool
hook and therefore do not. ClawBox now adopts only the captured OpenClaw SSH
session argv shape, assigns each real SSH subprocess a unique execution ID,
admits it synchronously, and adds the bridge envelope before launch. Arbitrary
unenveloped SSH remains rejected. Filesystem work is labeled `agent-tool`;
skills/workdir/backend preparation is labeled `backend-maintenance` and excluded
from Agent Tool throughput, but remains admitted and fully measured. The audit
also confirmed that background `exec` retains the local SSH child in OpenClaw's
process supervisor, so the original reservation is held until the remote
process ends and that SSH child is reaped; `process` polling does not start a
second remote command.

The current native lifecycle continuation also snapshots the Runtime during a
model wait for `snapshot_pause` arms. It witnesses the OpenClaw Agent PID before
Runtime checkpoint and after Runtime restore, restores Runtime synchronously in
the ModelGateway response-release barrier, and records Runtime/Tool role,
service-time, and response-hold fields in the event and gateway JSONL. The
Runtime remains resident for `resident` arms, and the replay-engine compatibility
path remains Tool-only by design.

Commit `43c67a2` implements and tests that paired native lifecycle. Commit
`c49c623` records the paired-snapshot validation boundary and updates the
operator documentation. The local full suite, the all-baseline c40
fake-CubeSandbox regression, compileall, and matrix audit pass after the
change. The live Kunpeng check remains a healthy
single-node Kubernetes diagnostic deployment with zero sandboxes; it still has
no standalone one-click bundle, second physical/private node, or
Runtime-reachable semantic TCP endpoint.

Recent continuation milestones `a6c3263`, `74fb823`, `72be43b`, and `1a6349a`
align the baseline catalog and matrix audit with schema-v2, use actual native
Tool names in replay telemetry, document that distinction, and expose the
catalog through the read-only experiment CLI. They are all pushed to
`origin/main`.

The follow-up source gates `50ccc45`, `f1701e9`, and `802bfbc` are also pushed:
the policy shim now has regression coverage for all six native Tool-VM
operation names and rejects unenveloped Agent SSH before subprocess launch;
the local c40 gate materializes every implemented recipe directly from the
canonical baseline catalog; and raw CubeSandbox routes without an explicit
mapped port fail closed instead of defaulting to SSH port 22. `bdf6fba` records
the live Runtime template provenance mismatch described below.

Post-audit milestones `4327903`, `23449da`, `24a96f0`, and `62858a7` are also
pushed. They bind native artifact joins to the session identity, exercise the
managed API listener and API-to-replay export, and correct Runtime egress so
explicitly internet-enabled arms are not accidentally closed by the control
plane allowlist. The full local suite and compile/diff gates remain green.

Commit `53b1733` adds a creation-time provenance gate: whenever schema-v2 pins
an image digest, Worker verifies the corresponding CubeSandbox Template record
through the official Template API before creating either VM. A `READY` status
alone is insufficient, so the currently mismatched Runtime template fails
closed rather than producing misleading live evidence.

Commit `5a9598b` makes result provenance explicit for both VM roles: new
bundles record separate Runtime and Tool template references, source images,
and digests, while retaining the historical Tool provenance keys.

Commit `65321cb` makes the matrix audit require immutable Runtime/Tool
template IDs, source image references, and SHA-256 digests for formal
OpenClaw experiments. Commit `0060297` protects a Tool before restore or
memory admission begins, resolves the native route only after that boundary,
and adds the focused admission-versus-delayed-pause race regression.
Commits `2c09e5a` and `3b0ce0e` make both standalone Kunpeng route gates use
the semantic route object and fail closed for missing or non-2222 mapped
ports.
The obsolete environment-based endpoint fallback and standalone
known-host refresh were removed afterward; no supported path can supply a
route outside CubeSandbox resolution plus the per-invocation policy shim.
Commit `5b41a48` hardens the final ownership barrier: failed lifetime admission
cannot underflow a reservation during session cleanup, and one failed sandbox
kill no longer prevents cleanup from attempting the rest of the task-owned set.
The cleanup operation still fails closed after a fresh ownership-list check.
The follow-up artifact gate also binds policy, Runtime trace, and Tool bridge
records to the expected session ID before accepting an exact execution-ID join;
the Worker-only route descriptor refresh is explicitly limited to post-agent
artifact collection and never attempts to refresh the long-lived OpenClaw
backend.

The local c40 Worker gate now materializes every entry in the baseline catalog
directly against a concurrent fake CubeSandbox: 17 policy arms (10 canonical
recipes plus 7 compatibility aliases) x 40 sessions complete, all
session/sandbox/agent/cleanup spans are present, and the owned-sandbox set
returns to zero. This is scheduler and logging evidence only; it is not a
substitute for the blocked live Runtime-to-Tool route gate. The policy shim
tests cover `exec`, `process`, `read`, `write`, `edit`, and `apply_patch`, plus
fail-closed behavior for an unenveloped Agent SSH operation.

The session timing contract is now richer than the original aggregate metrics:
`session_timing` and result `performance.session_time_spans` include role-labeled
Runtime/Tool lifecycle spans, native Tool-operation spans, replay model-wait
spans, and managed model response-hold/delivery spans. CubeSandbox lifecycle
spans retain wall-clock and monotonic timestamps, service time, state
transitions, and status for machine-readable cross-process analysis.

The managed result path now closes the evidence join after collection: every
admitted native Tool execution is enriched from its exact-ID cgroup-v2 record
with guest peak RSS, duration, CPU utilization, telemetry/KB eligibility, and
prediction error. Result provenance contains the frozen KB hash and evidence
class; arm summaries expose fallback and error distributions. The KB remains
immutable during a comparison arm so held-out evaluation cannot silently train
on itself.

Lifecycle timing records now include node-wide physical-memory observations
before and after CubeSandbox operations, with observed reclaimed/growth bytes.
These host measurements are explicitly distinct from guest Tool cgroup RSS and
remain subject to concurrent-node noise. A host `oom_kill` delta invalidates the
arm, while PolicyCoordinator records budget and emergency-free-memory safety
interventions in its admission metrics.

The managed experiment schema can now run a fixed heterogeneous trajectory mix
inside one arm. `workload.session_assignment: round_robin` maps session index
to the ordered case list and preserves the same mapping across policies;
configured trace hashes and the realized session assignment are emitted with
the result. `execution.arrival_schedule` distinguishes an explicit simultaneous
burst from a deterministic fixed stagger, avoiding an implicit startup pattern.
The current checked-in OpenClaw replay fixture is still the documented
single-trace smoke input; representative API-captured A/B/C trajectories must
replace it before formal c20/c40/c60 evidence is claimed.

The alternate GitHub child commit `edcbd8a97f47bbe81c3119931bc449bc28f54fbd`
was reviewed against this post-`50db717` tree and is intentionally not
cherry-picked. Its useful lifecycle-failure, per-execution timing, FIFO
admission-metric, and Runtime-local-tool ideas are incorporated selectively in
`7a0740f`; its older endpoint-keyed known-host refresh and restore-must-change-
address assumptions are incompatible with this contract. The commit remains
available by its original SHA; no branch or tag is force-updated or deleted.

The Runtime-side `/usr/local/bin/ssh` shim:

1. reads the ClawTune `__CBX_EXEC_1__` envelope;
2. sends session ID, execution ID, operation, command SHA-256, and prediction
   metadata to `/v1/tool/admit`;
3. blocks until `ADMIT` (including restore/admission wait);
4. launches `/usr/bin/ssh` exactly once with inherited stdio;
5. sends an idempotent completion; it never retries the command.

If admission returns an idempotent duplicate response, the shim fails closed
before starting OpenSSH rather than repeating the Tool side effect. This keeps
the exact execution-ID join requirement stricter than the control-plane
idempotency mechanism.

PolicyControl has per-session `ACTIVE -> DRAINING -> CLOSED` state, exact
execution-ID idempotency, a threaded backlog of 256, and no lock held while an
admission callback blocks. Its c60 unit HOL test proves 59 short independent
sessions finish admission before one slow session.

## Live Kunpeng boundary

Host: `weitianc@193.124.7.2`

The semantic CubeSandbox route was proved from CubeMaster/CubeProxy metadata:
Tool port 2222 already produces a per-sandbox `HostIP:mappedPort` mapping, and
pause/resume can change the mapped port. The public SDK contract now exposes
that route without exposing those internal fields. The local route gate then
found a deployment-topology blocker: the current CubeNode is a normal pod, so
the semantic endpoint resolves to its pod IP (for example
`192.168.3.166:mappedPort`). The host can reach that mapping, but a Runtime VM
cannot; the Runtime receives `Connection refused` even though the Tool bridge
is listening and the host-side TCP probe succeeds. The isolated Tool guest
address is not a valid substitute.

During the latest diagnosis, a temporary `hostNetwork=true` CubeNode
experiment was run with zero sandboxes. A live `cubevsmapdump` showed the
existing `remote_port_mapping` entries for guest port 2222 and `tc` showed the
CubeSandbox `from_world` ingress program attached. Nevertheless, host
self-connect, CubeProxy-to-endpoint, and an external TCP probe all returned
`Connection refused`; Tool bridge setup consequently received a CubeProxy 502
from the same upstream endpoint. This proves that CubeSandbox creates the
semantic mapping and programs its datapath, but does not prove Runtime
reachability in this deployment. The experiment was reverted to
`hostNetwork=false`/`ClusterFirst`. Rollback briefly reproduced
`gateway mac for eth0 via 169.254.1.1 not found`; a root-initiated reboot with
zero sandboxes restored CubeNode to `3/3`, restored the API, and left
`GET /v2/sandboxes` empty. This is a deployment topology/recovery blocker, not
a reason to add a ClawBox networking layer. Existing kernel, S3lvol, templates,
and results were not intentionally modified or deleted. The remote ClawBox
checkout is at `d42da58` with the native-endpoint production files synchronized
in its working tree; pre-existing untracked `results/` and `uv.lock` are
preserved.

The updated Cube API image was built and deployed as
`127.0.0.1:5000/clawbox/cube-api:route-endpoint-b3a1ee7`, registry digest
`sha256:5556089ac9167040f29f6bac36bfc90043f4cc2d105d43c51dcdd8c3192113de`.

The post-reboot c1 route gate created a fresh Tool and resolved
`192.168.3.157:20004` for container port 2222, then the Runtime identity probe
returned `Connection refused`. The gate failed closed at the intended identity
check and cleaned up; `/v2/sandboxes` was `[]` afterward. No c4/c8 run was
started because the required c1 identity gate is not green.

Published images:

```text
Runtime (source 49ab5a4)
127.0.0.1:5000/clawbox/runtime-cube-arm64@sha256:05cb920d0c79ee381263f2d57663a8c068d889394b6a9ad942af34810931422f

Tool (source de74a26)
127.0.0.1:5000/clawbox/tool-cube-arm64@sha256:b175fea75b4cd43d5116d93e0c2fc1b745a68d24d84446d5af0b3c72d42a8781
```

Fresh accepted Tool template:

```text
template_id: tpl-b5cb6f5ee26a41448000b9c2
alias: clawbox-tool-native-ssh-de74a26
ports: 49983, 2222
kernel: sha256-f84e3fa28ae6
image: http://172.17.0.1:5001/clawbox/tool-cube-arm64@sha256:b175fea...
```

That block is historical. The 2026-09-06 live inventory reports current kernel
component `sha256-a63aa77e9c2d` for both accepted templates. Current immutable
records are Runtime `tpl-67569219b64f4a80836a1f35` / image
`sha256:e3b0bb69751c40d48421eaef53dbfd7b8d6b9bb5735eb1aa259b88862d4718ab`
(ClawBox `346da48`) and Tool `tpl-06b699a92c694c7ba3e6465b` / image
`sha256:b175fea75b4cd43d5116d93e0c2fc1b745a68d24d84446d5af0b3c72d42a8781`.
One first registration attempt is a reconciled `FAILED` record because the
private registry relay was absent; the successful alias was submitted once.
The temporary relay and isolated build checkout were removed, and the sandbox
inventory remained empty.

The public port-5001 registry mirror was absent after reboot. Do not expose it
again. A user-owned `socat` process is currently bound only to private Docker
bridge address `172.17.0.1:5001` and forwards to the loopback registry at
127.0.0.1:5000; Cube API pods successfully fetched `/v2/` through it. PID was
written to `/tmp/clawbox-registry-mirror.pid`; it is not reboot-persistent.

## Resumed Kunpeng audit

After SSH recovery, the old Cube API image was found not to contain the
semantic `/sandboxes/<id>/ports/<containerPort>` route: it returned an empty
body 404 even while direct CubeMaster `/cube/sandbox/info` returned the
existing `host_port` mapping. CubeSandbox source commit `64102d9` already
contains the route and its SDK implementation, so no ClawBox metadata fallback
was added. The already-built ARM64 API image
`127.0.0.1:5000/clawbox/cube-api:route-endpoint-b3a1ee7` was rolled out through
the node-local registry. A live Tool then returned JSON from the semantic SDK
path, for example `192.168.3.175:20019` for container port 2222.

The post-reboot normal-network templates used for the latest Runtime-to-Tool
probe were Runtime `tpl-3871262f976946fa835f3035` and Tool
`tpl-bc7533c482984dcc9594efdf`, both with the current kernel and checked-in
image digests. The Tool bridge listened on guest 2222 and CubeSandbox returned
the existing pod-IP `HostIP:mappedPort` route, but the Runtime VM received
`Connection refused`. The fresh host-network diagnostic templates were
Runtime `tpl-55ad06ce2a3a4d61b5682ef2` and Tool
`tpl-980d2310ac4c4dfcbd077128`; they are not valid follow-up templates after
the host-network experiment was reverted. No successful native c1/c4/c8 claim
exists.

The route gate now includes a bounded readiness wait after the semantic route
is resolved (commit `c8debc4`); it does not change the endpoint or retry the
SSH command. The host-network diagnostic temporarily advertised the node
public IP, but the existing CubeSandbox BPF route still refused TCP from host,
CubeProxy, and outside the node. Do not leave that experiment enabled and do
not work around the topology with a proxy, NodePort, Redis lookup, or guest IP.

Two failed template records are expected from discovery and must not be reused:

- initial image URL used unavailable public `193.124.7.2:5001`;
- `tpl-56851ed069e44912a13b5904` exposed the template-build/session-key bug fixed
  by `de74a26`.

## Current continuation result

The pre-reboot c40 replay bundles recorded in the earlier handoff are historical
evidence only. Their remote result directory was lost by the reboot and must
not be presented as the current source/configuration result. A post-reboot
smoke c40 attempt initially omitted the existing CubeProxy SDK transport
variables and failed with virtual-host DNS errors after creating and cleaning
all sessions. A second attempt supplied that transport and completed all
creates/cleans, but used the legacy `sandbox-code` template without the
ClawTune Tool bridge; its 40 sessions per arm failed validation. Commit
`65ed9cf` now points every replay suite at the accepted instrumented Tool
template and updates the real-LLM example to current templates. The corrected
c40 suites remain pending a Runtime-reachable semantic endpoint; the latest
reboot recovered CubeNode control-plane health but did not change that
datapath result.

The final recovery check on 2026-09-05 found CubeNode `3/3` with
`hostNetwork=false`/`ClusterFirst`, a responding Cube API, and
`GET /v2/sandboxes` equal to `[]`. The host-network diagnostic was not left
enabled. No c40 retry was started and no native c1 was claimed after recovery;
the normal-network c1 identity probe still fails at Runtime-to-mapped-endpoint
TCP reachability.

### HostPort reachability classification

The latest diagnostic separates CubeProxy's same-host path from CubeSandbox's
physical-NIC HostPort path. The cluster has exactly one Kubernetes node and the
node has only one addressed physical interface (`enp193s0f0` at
`193.124.7.2`), so a second cluster node/private-NIC probe is unavailable.

In normal networking, a fresh Tool returned semantic endpoint
`192.168.3.190:20002`; direct CubeMaster metadata for that same Tool reported
`sandbox_ip=172.16.0.2`. CubeProxy command setup succeeded, but a Runtime
attempt to `executor@172.16.0.2:2222` timed out. Thus CubeProxy's same-host
control path does not establish Runtime-to-Tool SSH reachability, and no
same-node SandboxIP-to-HostPort hairpin support is claimed.

With the temporary host-network diagnostic, a Tool returned
`193.124.7.2:20000`. Cubelet's root-visible map contained
`ifindex=33, host_port=20000, guest_port=2222`, and `tc` showed the JITed
`from_world` program attached to `enp193s0f0` (program id 511, the expected
program tag). A workstation probe to mapped port 20000 failed while the same
workstation reached node SSH port 22. This proves mapping publication and TC
attachment, but not a BPF hit: this build exposes no per-filter BPF counter,
and the aggregate ingress qdisc counter cannot attribute packets to the probe.
The host-network diagnostic was cleaned up, reverted, and followed by a
zero-sandbox reboot; the final state is `hostNetwork=false`, CubeNode `3/3`,
and `GET /v2/sandboxes` equal to `[]`.

The normal endpoint still reports a Kubernetes Pod IP as `HostIP`. That legacy
deployment topology must not be optimized in ClawBox; the formal native SSH
path remains pending a deployment-owned semantic endpoint that Runtime can
actually reach.

The standalone one-click bundle was not installed on Kunpeng. Two build-only
attempts were made from the prepared CubeSandbox checkout: the first stalled
in the ARM64 builder's Ubuntu package download, and the retry (with host
networking requested and a China Go proxy build argument) was canceled while
still in the Ubuntu/LLVM package layers; the Dockerfile does not consume that
Go proxy argument. No builder image, standalone service, K8s object,
host-network setting, or persistent Cube data was changed by either attempt.
The current host remains the healthy diagnostic Kubernetes profile described
above.

A former read-only `GET /templates/<id>` probe found that Runtime
`tpl-39efe4ad90384a1fbea3caff` reported image `sha256:79a492d2...` rather than
the digest declared by the checked-in matrix. That ID is retired from current
examples. The mismatch was resolved by building and registering the immutable
Runtime/Tool records listed above; live runs must use those current IDs and
must still call `validate_template_image` before creating a VM.

The same inventory shows the Runtime alias used by the older replay matrices,
`clawbox-runtime-arm64-2g-v3`, resolves to
`tpl-743b4bf146c642328ebe4e70` with image digest `sha256:02ae0ff3...`, so
those unpinned historical matrix configs are not current-artifact evidence
either. The checked-in OpenClaw configs are digest-pinned and will now fail
closed through the Worker gate until a matching Runtime template is
registered.

Current-source runs after `a4be792` emit `session_timing` events containing
`session`, `agent`, per-sandbox create, validation, hashing, and cleanup spans.
`7a0740f` additionally records failed lifecycle attempts, control admission and
completion service spans, FIFO admission wait distributions, and keeps the
Runtime-local retrieval/memory tool set separate from the six Tool-VM tools.
The current Worker marks a Tool active before restore or memory admission and
resolves its native route only after that boundary; this closes the competing
pressure-admission pause race and is covered by a focused regression.
The reproducible OpenClaw replay c40 smoke input is
`examples/experiments/openclaw-cube-replay-c40.yaml` with its checked-in model
trace; it is a pending live gate, not a passed run. A 2026-09-05 replay-contract
audit adds an important qualification: that checked-in marker trace is
hand-authored and its `raw_request` contains only the marker conversation. A
representative API-captured OpenClaw request contains the full six-field
chat-completions envelope, the evolving conversation, and six configured Tool
functions; strict replay correctly rejects that marker trace at step 0. It is
therefore a loadable smoke fixture, not exact OpenClaw replay evidence. Exact
OpenClaw replay evidence must come from an API-mode c1 run, the gateway's
exported request/response trace, and a subsequent replay run. That export path
was exercised locally against a retained 27-step API-captured artifact:
27/27 streaming responses were delivered, all canonical inputs matched, and
the replay completeness verdict passed. That artifact is historical and is
not a current live c40 result.
The local full Python suite passes; no new live c40 success claim is made until
the corrected runs complete and each run is checked for zero remaining
CubeSandbox resources.

The schema-v2 baseline audit is now independent of the unavailable host. The
read-only `scripts/audit-experiment-matrices.py` command loads all eight
checked-in experiment YAMLs, verifies their deterministic arm counts, rejects
the removed `sandbox-code` Tool template and `cube_shell` OpenClaw prompt
name, and checks every policy tuple against the immutable baseline catalog.
For formal OpenClaw matrices it also requires immutable Runtime and Tool
template IDs plus source image references and SHA-256 digests; alias-based
replay-engine capacity matrices remain historical systems fixtures only.
The four paper matrices plan c20/c40/c60; the native OpenClaw replay fixture
plans c40; the remaining c1/c4 files remain intentionally small gates. The
old baseline names are retained only as explicit schema-v2 compatibility
aliases. The pre-schema-v2 non-paper study translator now fails closed with a
migration message rather than importing removed workflow types or selecting a
second backend. The older `paper_experiment` direct-Firecracker runner remains
legacy code and is not part of the supported native Worker path.

The current replay-worker observation path also uses the actual replay/OpenClaw
Tool name (for example `exec`) in ClawTune spans and worker events; it no
longer emits the retired `cube_shell` label.

The managed gateway has both replay and API implementations. Replay is covered
by session-local cursor, retry, delivery, and HOL tests. API mode is covered by
both the lower-level forwarding test and an HTTP-listener integration test that
verifies model forwarding and server-side credential handling. A real upstream
model request was not sent by automation: the operator credential is sensitive,
and the live native SSH route is still unavailable.

The compatibility replay worker's snapshot planner is now causal as well: the
actual trace latency terminates the wait but is never supplied as a policy
prediction. Wait-aware/proactive formal matrices must provide
`model_wait_prediction_seconds` and `model_wait_prediction_source`. Proactive
restore is scheduled from that estimate and the full recorded model delay is
preserved after prefetch.

The remaining live blocker is concrete and is not a ClawBox endpoint-resolution
bug: this deployment does not route the already-existing CubeSandbox mapping
from a Runtime VM. `get_host(2222)` remains an HTTP ingress authority and is not
an OpenSSH endpoint. Do not claim native c1/c4/c8 or scale results until the
CubeSandbox deployment supplies a deployment-owned, Runtime-reachable raw TCP
endpoint for the same semantic API. Do not work around this with guest IPs,
NodePort, a ClawBox proxy, or a second allocator.

## Highest-priority next steps

1. Recover the CubeSandbox node/Cubelet deployment and verify the existing
   semantic endpoint still has the documented Runtime reachability boundary.
2. Correct the CubeSandbox deployment topology so its existing semantic raw
   endpoint is reachable from Runtime, without adding a ClawBox networking
   layer. Re-run the host/Runtime reachability proof.
3. Run the Cube-only pair smoke with Runtime template
   `tpl-67569219b64f4a80836a1f35` and Tool template
   `tpl-06b699a92c694c7ba3e6465b`, then verify admitted SSH, Tool pause, demand
   restore, exact execution count, and no policy command/output leakage.
4. Rerun the corrected replay c40 suites sequentially with the accepted
   instrumented Tool template, bounded pair creation, and a zero-leak audit.
5. Run managed OpenClaw c1 in replay and API modes with the operator-provided
   credential, export the API response trace, and require replay equivalence
   before any native OpenClaw scale claim.
6. Join Runtime ClawTune spans, PolicyControl records, Tool bridge JSONL,
   cgroup artifacts, and eBPF artifacts by `(session_id, execution_id)` and
   retain the new `session_timing` events in the result bundle.
7. Validate every native file tool, not merely `exec`, against the installed
   backend. Confirm recognized below-hook filesystem SSH is assigned a unique
   ID, admitted, enveloped, and exactly joined; confirm arbitrary unenveloped
   SSH fails closed. `process` must continue polling the retained original SSH
   child without launching a duplicate command.
8. Run native OpenClaw c4/c8 HOL/cross-routing and leak tests; the focused
   admission-versus-delayed-pause race test is now covered by `0060297`.
9. Only after those native gates pass, run c20 and finally c40/c60 native
   OpenClaw policy arms. The formal replay c40 matrix is also pending a
   corrected live run after CubeNode recovery.

## Easier cleanup left intentionally

Once native c1 is green, delete the unsupported Kubernetes and HTTP-execution
surface: `clawbox/cell`, managed API/dispatcher, Kubernetes controller/backend,
old allocator/scheduler/tool-agent/node-agent services, SandboxTask manifests,
NodePort/RBAC deployment files, WorkerBridge stress/pair scripts, and their
tests. Rewrite or remove Kubernetes-era examples and status docs. Do not delete
CubeSandbox installation/kernel recovery assets merely because CubeSandbox's
own deployment happens to use Kubernetes internally; ClawBox itself must not
call that layer.

## Experimental acceptance

For every formal arm retain separate create, Agent, model wait, Tool, checkpoint,
restore, validation, hashing, destroy, and stabilization boundaries. Required
correctness is exact-ID join rate 1.0, telemetry loss 0, duplicate Tool execution
0, wrong-session routing 0, replay divergence 0, correct validation, and zero
owned sandbox leaks. Report correct agents/min, correct steps/min, JCT
mean/p50/p90/p95, Tool latency, physical mean/peak/RSS-time, pause/restore
service time and counts, reclaimed/transient memory, admission blocked time,
prediction error, and fallback rate.
