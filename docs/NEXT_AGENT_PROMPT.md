# Prompt for the next ClawBox code agent

Continue the ClawBox/CubeSandbox project from
C:\Users\29068\Desktop\ClawBox. The branch is main; the latest pushed commit
is c49c623 (Record paired VM snapshot validation boundary), based on 43c67a2
and a8f43f5. The special GitHub commit
edcbd8a97f47bbe81c3119931bc449bc28f54fbd must remain available and must not
be deleted, reset over, force-rewritten, or blindly cherry-picked. Review the
complete docs/HANDOFF.md, docs/implementation-status.md, and
docs/cubesandbox-setup.md before changing code.

## Final objective

Deliver a standalone-CubeSandbox-native-OpenClaw implementation in which each
logical Agent owns one Runtime VM and one Tool VM. OpenClaw uses native SSH for
all six file/process operations: exec, process, read, write, edit, and
apply_patch. During a native model wait, snapshot_pause must checkpoint and
swap out both Runtime and Tool; Runtime must be restored before ModelGateway
releases the pending model response, and Tool may remain swapped until the next
SSH admission. resident keeps both VMs running. Replay compatibility arms may
retain their documented Tool-only behavior.

The formal acceptance path is live c1 identity and lifecycle proof, then c4
and c8 correctness/stress, then only c20/c40/c60. Required evidence includes
exact execution-ID joins, zero duplicate execution, zero wrong-Tool routing,
zero policy command/output leakage, zero owned-sandbox leaks, complete timing
spans, Runtime Agent PID continuity, pause/restore behavior, and replay/API
equivalence. Update README and setup/status docs as the deployment becomes
reproducible on a fresh or already-setup machine.

## Non-negotiable architecture

CubeSandbox already owns the per-sandbox L4 mapping for Tool container port
2222. ClawBox must ask the semantic CubeSandbox API for a raw TCP endpoint for
(sandbox_id, container_port) and must not interpret CubeProxy internals,
HostIP, SandboxIP, or Redis metadata itself. Do not add a ClawBox port
allocator, SSH proxy, NodePort, guest-IP fallback, direct guest-IP assumption,
Redis lookup, or new networking layer. HostIP:mappedPort is ephemeral; Tool
identity is stable and must be enforced with a stable HostKeyAlias and a
unique SSH host key per Tool sandbox. The policy shim must use the endpoint
returned synchronously for the current /usr/bin/ssh invocation.

The installed OpenClaw backend was inspected and captures
agents.defaults.sandbox.ssh.target when constructed. Do not revive the old
200 ms config-patch/watcher design or restart openclaw agent to refresh a
route. /complete must not release or pause a Tool until the SSH child belonging
to that logical operation has been reaped. Duplicate admissions must fail
closed before a second SSH process starts.

## What is already implemented and tested

- CubeSandboxClient.get_tcp_endpoint() and CubeSandboxTcpEndpoint provide the
  semantic endpoint, with validation and retries after create/restore.
- Native admission returns sandbox identity, endpoint epoch, host, and mapped
  port. Epoch/stale endpoint, missing-port, cross-Tool, strict-host-key,
  duplicate-execution, all-six-operation, and child-reaping tests exist.
- Native route refresh is synchronous per admission; the captured OpenClaw
  target is not patched asynchronously.
- Worker lifecycle spans, ModelGateway event/hold timing, API/replay coverage,
  Runtime egress allowlisting, template provenance checks, bounded pair
  creation, and zero-leak cleanup are present.
- c49c623 and 43c67a2 implement local native snapshot_pause coverage for both
  VM roles. The regression witnesses the Runtime Agent PID before checkpoint
  and after restore and proves response release waits for Runtime restore.
- Local .venv\Scripts\python.exe -m pytest -q, the fake-CubeSandbox
  all-baseline c40 gate, compileall, git diff --check, and matrix audit passed
  at the latest milestone. These are not live Runtime-to-Tool evidence.

## Live Kunpeng facts and blocker

Use ssh kunpeng for the real machine. The last verified host was a single
OpenEuler 24.03 aarch64 Kubernetes node (193.124.7.2) with Cube K8s pods and
an empty ClawBox/CubeAPI inventory. CubeNode was healthy (3/3) after the
diagnostic rollback/reboot, hostNetwork=false, and no sandboxes remained.
There is no second physical/private addressed NIC or second node, so an
independent cross-node HostPort test is unavailable.

CubeMaster/CubeProxy metadata and the semantic API prove that Tool 2222 gets a
per-sandbox mapped endpoint. In the current legacy K8s topology the returned
HostIP is a pod IP. Host-side access to the mapping worked, but Runtime to
that endpoint returned Connection refused; a separate Runtime to Tool
SandboxIP:2222 attempt timed out. A temporary hostNetwork=true diagnostic
showed the existing remote_port_mapping and from_world TC attachment, but
host, CubeProxy, and external probes still refused TCP. That diagnostic was
fully reverted and the host rebooted. Do not claim same-node hairpin support or
BPF hit-path success from a populated map.

The final supported deployment must be standalone CubeSandbox/control-compute,
not ClawBox's legacy Kubernetes execution path. The one-click ARM64 bundle was
not installed. Two build-only attempts were stopped before producing a builder
image or bundle because of package-layer stalls; they made no K8s or persistent
host changes. Do not install over /usr/local/services/cubetoolbox or
migrate/stop the diagnostic K8s services without a deliberate, documented
topology plan.

The remote /home/weitianc/ClawBox checkout is old and dirty (HEAD was d42da58)
with native files modified and untracked artifacts; do not reset or overwrite
it. The remote /home/weitianc/CubeSandbox checkout is detached at 64102d9 and
has unrelated dirty config changes; preserve them. The checked-in CubeSandbox
source contains the semantic endpoint route and SDK. Existing published ARM64
Runtime/Tool images and template IDs/digests are documented in
docs/HANDOFF.md; the accepted Runtime template had a provenance mismatch and
must be rebuilt or reconciled before live evidence.

## Ordered continuation

1. Inspect current git state and read the three docs above. Preserve unrelated
   work and the special commit. Verify Kunpeng is healthy read-only.
2. Make the CubeSandbox standalone deployment reproducible. Build the ARM64
   one-click bundle if needed, diagnose its package/proxy failure in the
   CubeSandbox build itself, and document exact fresh-machine and existing-
   machine commands. Do not modify the K8s diagnostic topology as a workaround.
3. Register or reconcile immutable Runtime and Tool templates from the checked-
   in image digests. Confirm both template provenance records before creating
   VMs.
4. Prove the deployment-owned semantic endpoint is reachable from Runtime. If
   HostPort is used, test from a genuinely different physical node/private NIC
   when available, inspect the from_world TC attachment and attributable BPF
   hit evidence, and separately test Runtime to Tool SandboxIP. Restore every
   diagnostic host-network change after each test.
5. Run the c1 pair gate. Verify endpoint identity, including a negative test
   where Tool A's expected identity is presented to Tool B's endpoint, epoch
   refresh and stale invalidation, pause/resume, strict ssh -G settings,
   stable OpenClaw Agent PID, next-invocation endpoint consumption, exact
   command count, remote Tool identity marker, and zero leaks.
6. Run c4/c8 sequentially with unique mappings and cross-routing checks. Only
   after c1/c4/c8 are green run corrected replay c40 and native c20/c40/c60.
   Run replay and real managed-API c1; use the operator credential only through
   the documented secret-handling path and never commit it.
7. For each milestone, update status, handoff, and README docs, run proportional
   tests, make a focused git commit, and push origin/main. Never claim live
   acceptance from local fakes, a TCP-only probe, or historical post-reboot
   artifacts.

If the live endpoint remains unreachable, stop at the topology blocker, leave
diagnostics restored, record exact commands and results, and do not fix it in
ClawBox. The correct next request is a CubeSandbox deployment or topology
change, not a proxy or allocator.
