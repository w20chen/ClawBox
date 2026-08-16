# openEuler / Kunpeng arm64 stage-0 and stage-1 gate

This is the required gate before running ClawBox sandboxes on a Kunpeng host.
It deliberately separates installation from verification: ClawBox must not
create a RuntimeClass that points at a handler which is not installed.

## Runtime choice

Use `kata-qemu` for the first openEuler acceptance run. It is the conservative
arm64 baseline and is the default in ClawBox. `kata-fc` remains supported, but
only select it after the host has an arm64 Firecracker binary, matching Kata
guest kernel/rootfs, containerd handler, and a passing live smoke gate.

The exact handler names come from the host's Kata installation. List them with:

```bash
kubectl get runtimeclass
```

If the installation calls the QEMU handler `kata` rather than `kata-qemu`, pass
that exact name to every command below. `deploy/runtimeclass.yaml` is an example
mapping only; applying it does not install Kata.

## Stage 0: host and live cluster gates

On the target openEuler machine:

```bash
cd ~/ClawBox
bash deploy/check-host.sh --runtime-class kata-qemu
bash scripts/arm64-kata-smoke.sh --runtime-class kata-qemu
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
