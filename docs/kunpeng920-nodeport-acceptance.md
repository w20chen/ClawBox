# Kunpeng 920 NodePort bridge acceptance

Run date: 2026-09-04. Host: `hostname-txyuq.foreman.pxe` (`193.124.7.2`).
Source milestone: `dbb3ef9`.

The live validation used fresh immutable templates:

```text
Runtime template: tpl-72fecb8e388d4c9fa3a61054
Runtime image:    sha256:5d1ea3cee703da47b031b26d8439e240b9d39ffb978e084c482fae1e17764ca7
Tool template:    tpl-4ffe6e6abd574be99b2869e1
Tool image:       sha256:750b71f97322467a23537973c77b23160ff37d2adcdcd32aa7bba07d78c4725b
Guest kernel:     sha256-f84e3fa28ae6
Worker endpoint:  http://193.124.7.2:31853
```

The temporary Worker Pod listened on `0.0.0.0:18080`. Its task-specific
NodePort Service used `targetPort: 18080` and `externalTrafficPolicy: Local`.
The Runtime VM reached the node InternalIP endpoint and returned the expected
unauthenticated `401` probe response. Wrong-token requests returned `401` and
valid-token/wrong-session requests returned `403`.

The real two-VM pair smoke passed with Tool pause/resume and zero-loss
telemetry. Both pre-pause and post-resume checks reported cgroup-v2
`sampling_quality=valid`, native `collection_validity=valid`, `cleanup=ok`, and
`telemetry_loss_total.total=0`. The two bridge execution IDs were joined across
the Worker response, Worker record, cgroup artifact, and native artifact with
`exact_id_join=1.0`.

The dependency-free ARM64 bridge stress harness also passed through the live
NodePort:

```json
{"completed_requests":141,"hol_short_finished_before_long":true,
 "levels":[{"level":1,"requests":1},{"level":8,"requests":8},
 {"level":20,"requests":20},{"level":40,"requests":40},
 {"level":60,"requests":60}],"registered_sessions":101,
 "registry_after_cleanup":0,"request_records":142,
 "secrets_logged":false,"status":"PASS"}
```

This stress run uses independent synthetic executors to measure bridge
concurrency without provisioning 100 Tool VMs. The two-VM pair smoke keeps one
Tool VM by design, so its two registered session checks are sequential; it does
not claim to be the full 40/60-agent exact-ID experiment. The WorkerBridge
tests and live stress run establish concurrent dispatch/HOL behavior, while the
pair establishes the real Runtime-to-NodePort-to-Tool and telemetry path.

Final cleanup after the live run:

```text
Cube API /sandboxes: []
task-owned NodePort Services: none
cube-sandbox-s3lvol.service: active/running
/var/run/s3lvol.sock: listening UNIX socket
cubelet-mounted socket: present
cube-node: 3/3 Running
```

The temporary Pod and Service were deleted. No persistent Cube data, existing
templates, guest-kernel artifacts, or platform services were deleted or
restarted for this bridge test.

## Controlled reboot persistence check (historical first observation)

The controlled reboot completed and SSH/Kubernetes/S3lvol recovered. The
reboot initially restored the vendor active guest-kernel file
(`a63aa77e…`), so the committed recovery script was run after the direct
regression evidence. It now verifies the kprobe artifact checksum, preserves
the vendor files as `*.original-a63aa77e`, and restores the active file and
component registration to `f84e3fa…`.

The reduced acceptance was initially blocked by a separate Cube node bootstrap
failure: `cubelet` repeatedly exits before listening on port 9999, and
`cube-egress-net` reports that host interface `cube-dev` is absent after its
300-second wait. `effective-pvm` and the pod configuration both specify PVM
disabled (`0`); `cube-sandbox-s3lvol.service` is active and
`/var/run/s3lvol.sock` is present. No CubeVS, containerd, kubelet, S3lvol,
routing, persistent data, or existing template repair was applied for this
symptom. The next operator action must be a site-specific CubeVS/node-network
bootstrap recovery, after which the reduced template/kprobe/bridge acceptance
can resume. It subsequently recovered without changing persistent Cube data,
existing templates, S3lvol, containerd, kubelet, routing, or the host kernel.

## Post-reboot reduced acceptance completed

After the node recovered, the reduced persistence acceptance passed:

```text
node: Ready
cube-node: 3/3 Running
cube-sandbox-s3lvol.service: active/running
/var/run/s3lvol.sock: listening UNIX socket
cubelet-mounted socket: present
Tool template: tpl-4ffe6e6abd574be99b2869e1
Runtime template: tpl-72fecb8e388d4c9fa3a61054
guest kernel: 6.6.119-cube.bm.guest.001, CONFIG_KPROBES=y
post-reboot NodePort: 31209
pair smoke: PASS; exact bridge join: 1.0; cgroup/eBPF loss: 0
bridge stress: PASS; 141 requests; HOL isolation; secrets_logged=false
Cube API /sandboxes: []
task-owned NodePort Services: none
```

The temporary Pod and Service were deleted. This closes the post-reboot
kernel, template, bridge, pause/resume, telemetry, and cleanup acceptance
milestone. It remains a reduced two-VM acceptance, not a claim that the full
paper experiment matrix has completed.
