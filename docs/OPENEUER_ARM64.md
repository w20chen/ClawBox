# openEuler / Kunpeng arm64 stage-0 and stage-1 gate

This is the required gate before running ClawBox sandboxes on a Kunpeng host.
It deliberately separates installation from verification: ClawBox must not
create a RuntimeClass that points at a handler which is not installed.

## Runtime choice

Use `kata-qemu-runtime-rs` for the first openEuler acceptance run. It is the
Rust shim arm64 baseline selected by ClawBox (and the upstream default from
Kata 4.x onward). `kata-fc` remains supported, but
only select it after the host has an arm64 Firecracker binary, matching Kata
guest kernel/rootfs, containerd handler, and a passing live smoke gate.

The exact handler names come from the host's Kata installation. List them with:

```bash
kubectl get runtimeclass
```

If the installation uses another QEMU handler name, pass
that exact name to every command below. `deploy/runtimeclass.yaml` is an example
mapping only; applying it does not install Kata.

ClawBox pins the automated install to Kata `3.31.0` by default. Do not use
runtime-rs `<= 3.30.0`: it is affected by the critical virtio-fs host escape
[CVE-2026-47243](https://github.com/kata-containers/kata-containers/security/advisories/GHSA-2gv2-cffp-j227).
`KATA_VERSION` may select a newer reviewed release, while the host gate rejects
versions below `3.31.0`.

## Dedicated bare-host bootstrap

For a new single-node host, inspect the exact plan first:

```bash
bash scripts/bootstrap-openeuler-arm64.sh plan
```

The plan rejects non-openEuler/non-arm64 hosts, insufficient CPU/RAM/disk,
a missing KVM device, conflicting Pod/Service/host CIDRs, and an existing
cluster not owned by this bootstrap. Override a conflicting network explicitly,
for example:

```bash
bash scripts/bootstrap-openeuler-arm64.sh plan \
  --pod-cidr 172.30.0.0/16 \
  --service-cidr 172.31.0.0/16
```

After reviewing the plan, apply it as the intended non-root administrator:

```bash
bash scripts/bootstrap-openeuler-arm64.sh apply
```

`apply` first obtains sudo authorization and verifies that root can read/write
`/dev/kvm` before changing the host.

The pinned profile is Kubernetes `1.35.x`, containerd `2.3.4`, runc `1.5.1`,
Calico `3.32.1` with VXLAN/NetworkPolicy, Helm `4.2.4`, and Kata `3.31.0` with
only `qemu-runtime-rs` enabled. The script disables swap and firewalld, places
SELinux in persistent permissive mode as required by the kubeadm RPM baseline,
initializes the control plane, installs the runtimes, and executes both
stage-0 gates. Replaced host configuration is backed up below
`/var/lib/clawbox-bootstrap/backups`; no automatic `kubeadm reset` or destructive
rollback is performed.

Inspect an installed host without mutation:

```bash
bash scripts/bootstrap-openeuler-arm64.sh status
```

## Stage 0: host and live cluster gates

On the target openEuler machine:

```bash
cd ~/ClawBox
bash deploy/check-host.sh --runtime-class kata-qemu-runtime-rs
bash scripts/arm64-kata-smoke.sh --runtime-class kata-qemu-runtime-rs
```

The read-only preflight checks openEuler, arm64, kernel >= 5.10, `/dev/kvm`,
cgroup v2, containerd CRI, Kata shim, the selected RuntimeClass, Ready arm64
nodes, and the NetworkPolicy API.

The live gate creates a temporary namespace containing exactly two Kata Pods
(`runtime` and `tool`) plus one small non-Kata attacker Pod. It verifies:

- both sandbox images actually execute as `aarch64`/`arm64`;
- Runtime can reach only its Tool Service;
- the attacker cannot reach the Tool Service, proving CNI policy enforcement;
- Runtime and Tool expose different guest boot IDs, proving distinct VMs;
- the temporary namespace and all resources are deleted on exit.

Use `--keep` only for diagnosis. Delete a retained namespace with the exact
command printed by the script.

Do not continue when either command reports `FAIL`. Save the complete output,
plus these diagnostics:

```bash
uname -a
cat /etc/os-release
kubectl get nodes -o wide
kubectl get runtimeclass -o yaml
kubectl get pods -A -o wide
sudo journalctl -u containerd --since '-10 min'
```

## ARM64 image rule

Firecracker does not emulate x86_64 on an arm64 host. Runtime, bundle, Tool
executor, and SWE-Rebench task images must therefore publish a `linux/arm64`
manifest or be rebuilt natively for arm64. Host Docker binfmt/QEMU success is
not accepted as proof that an image works inside a Kata guest; the live gate's
`uname -m` checks are authoritative.

## Stage 1: build directly from ClawTune v2

Keep `ClawBox` and `ClawTune` as sibling checkouts. ClawBox now consumes the
current ClawTune source locations directly:

```text
ClawTune/packages/clawtune-plugin
ClawTune/services/sidecar
ClawTune/swe_rebench/.runtime/assets
```

Prepare the generated single-Pod compatibility assets, then build native arm64
images:

```bash
cd ~/ClawTune
python3 -m swe_rebench.runner prepare

cd ~/ClawBox
REGISTRY=registry.example.com/clawbox \
TAG=arm64-dev \
PUSH=1 \
bash scripts/build-kubernetes-images.sh
```

Run the build on the Kunpeng host, or configure BuildKit with a real arm64
builder. The Runtime image compiles the current `clawtune-plugin`, installs the
current `clawtune-sidecar`, copies the tracked cold-start KB snapshots, and
records the exact ClawTune Git revision in an OCI image label:

```text
org.opencontainers.image.source.clawtune.revision
```

Validate the published manifests before deployment:

```bash
docker buildx imagetools inspect registry.example.com/clawbox/runtime:arm64-dev
docker buildx imagetools inspect registry.example.com/clawbox/clawtune-swe-bundle:arm64-dev
```

The legacy names `packages/openclaw-plugin`, `services/scheduler`,
`agent_scheduler`, and `.runtime/bundle` are no longer part of the build path.

## Current boundary

Passing these gates proves the host substrate and the ClawTune v2 Runtime
artifact. It does not yet turn the existing SWE launcher into a task-scoped
Runtime Pod + task-image Tool Pod pair; that is the next lifecycle phase.
