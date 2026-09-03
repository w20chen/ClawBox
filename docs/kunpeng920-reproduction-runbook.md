# Reproducible Kunpeng 920B CubeSandbox setup

This is the shortest verified path to reproduce the ARM64 CubeSandbox
environment used by ClawBox. It records the important live-host steps as well
as the source-controlled files that implement them. Do not use old READY
templates as acceptance evidence: templates must be rebuilt after changing the
guest kernel.

## 1. Prepare the host

Use an ARM64 Kunpeng 920B host with Kubernetes, containerd/CRI v1, Helm,
`/dev/kvm`, cgroup v2, and at least 210 GiB free under `/data`. The host must
have a reflink-capable XFS filesystem at `/data/cubelet`, or the installer can
create its 200 GiB loopback filesystem there.

Install and start CubeS3lvol as a host systemd service before CubeSandbox:

```bash
sudo systemctl enable --now s3lvol
sudo systemctl is-active s3lvol
test -S /var/run/s3lvol.sock
```

The S3 backend (MinIO in the default profile) must be reachable before the
cube-node rollout. If the socket is recreated, restart cube-node so the new
socket inode is mounted into the cubelet container:

```bash
kubectl -n cube-system rollout restart daemonset/cube-node
kubectl -n cube-system rollout status daemonset/cube-node --timeout=10m
```

Do not delete `/data/cubelet`, MinIO data, S3 objects, results, or kernel
backups during recovery.

## 2. Install the pinned CubeSandbox chart

From this repository, set secrets without committing them and run the normal
installer:

```bash
export CUBE_MYSQL_PASSWORD='...'
export CUBE_MYSQL_ROOT_PASSWORD='...'
export CUBE_REDIS_PASSWORD='...'
bash scripts/install-cubesandbox-kunpeng920.sh check
bash scripts/install-cubesandbox-kunpeng920.sh install
```

The installer pins CubeSandbox v0.7.0 and layers
`deploy/cubesandbox/runtime-values-kunpeng920.yaml`. It also deterministically
patches the pinned chart to mount exactly `/var/run/s3lvol.sock` into the
cubelet container using a `hostPath` whose type is `Socket`. It must not mount
all of `/var/run` or use `FileOrCreate`.

Verify both source rendering and the installed manifest:

```bash
helm template cube ... | grep -A5 -B2 s3lvol-socket
helm -n cube-system get manifest cube | grep -A5 -B2 s3lvol-socket
kubectl -n cube-system get pods -o wide
kubectl -n cube-system exec <cube-node-pod> -c cubelet -- test -S /var/run/s3lvol.sock
```

The node is ready only when all three cube-node containers are running. Also
check that the cubelet logs show successful S3lvol/CubeCoW initialization.

## 3. Build and install the kprobe guest kernel

Use the pinned OpenCloudOS 6.6.119-49.6 source and the checked-in config patch:

```text
deploy/cubesandbox/kernel-oc9-arm64-kprobes.config.patch
```

Build it through the CubeSandbox ARM64 `scripts/build-kernel.sh` flow. The
required result is:

```text
CONFIG_KPROBES=y
CONFIG_KRETPROBES=y
CONFIG_FTRACE_SYSCALLS=y
sha256:f84e3fa28ae692f34645aa3c7034999242760eb25aab0ea667b43f16ac12c27f
```

Install it through the privileged `cube-kernel-install` container, preserving
the vendor files as `*.original-a63aa77e` backups. The active files are
`vmlinux-bm`, `version`, and `version.json`; make `vmlinux` point to
`vmlinux-bm`. Use the checked-in metadata:

```text
deploy/cubesandbox/kernel-oc9-arm64-kprobes.version
deploy/cubesandbox/kernel-oc9-arm64-kprobes.version.json
```

The repository provides the idempotent installer below. It verifies the local
artifact checksum, stages it under persistent `/data/cubelet`, preserves vendor
backups, registers the component only when it is missing or different, and
restarts cube-node only when a change was made:

```bash
bash scripts/install-kprobe-kernel-kunpeng920.sh /path/to/vmlinux <cube-node>
```

Its underlying installation shape is:

```bash
POD=$(kubectl -n cube-system get pod -l app=cube-node-installer \
  -o jsonpath='{.items[0].metadata.name}')
kubectl -n cube-system cp /path/to/vmlinux "$POD:/tmp/vmlinux-kprobes" \
  -c cube-kernel-install
kubectl -n cube-system exec "$POD" -c cube-kernel-install -- sh -s <<'EOF'
set -eu
ROOT=/usr/local/services/cubetoolbox/cube-kernel-scf
test -f /tmp/vmlinux-kprobes
test -f "$ROOT/vmlinux-bm-original-a63aa77e" || cp -a "$ROOT/vmlinux-bm" "$ROOT/vmlinux-bm-original-a63aa77e"
test -f "$ROOT/version.original-a63aa77e" || cp -a "$ROOT/version" "$ROOT/version.original-a63aa77e"
test -f "$ROOT/version.json.original-a63aa77e" || cp -a "$ROOT/version.json" "$ROOT/version.json.original-a63aa77e"
install -m 0644 /tmp/vmlinux-kprobes "$ROOT/vmlinux-bm"
ln -sfn vmlinux-bm "$ROOT/vmlinux"
EOF
```

Copy the two checked-in metadata files into the same container and install
them as `version` and `version.json`. Then register the component copy below;
the directory name and the contents of `version` must match exactly.

The active component identity is `sha256-f84e3fa28ae6`. Register the same
kernel under the component-version store, otherwise CubeMaster will reject a
new VM even when the template metadata is correct:

```text
/data/cubelet/root/component_versions/cube-kernel-scf/sha256-f84e3fa28ae6/
  vmlinux-bm
  vmlinux -> vmlinux-bm
  variant  # bm
  version  # sha256:f84e3fa28ae692f34645aa3c7034999242760eb25aab0ea667b43f16ac12c27f
```

The component registration can be completed with:

```bash
kubectl -n cube-system exec "$POD" -c cube-kernel-install -- sh -s <<'EOF'
set -eu
C=/data/cubelet/root/component_versions/cube-kernel-scf/sha256-f84e3fa28ae6
mkdir -p "$C"
install -m 0644 /tmp/vmlinux-kprobes "$C/vmlinux-bm"
printf 'bm\n' > "$C/variant"
printf 'sha256:f84e3fa28ae692f34645aa3c7034999242760eb25aab0ea667b43f16ac12c27f\n' > "$C/version"
ln -sfn vmlinux-bm "$C/vmlinux"
EOF
```

Restart cube-node and wait for `3/3 Running`. Never mutate old template
metadata and never disable the kernel compatibility check.

## 4. Publish immutable ARM64 images

Build and publish the Runtime, Tool, and Worker images to the reachable
registry. The Runtime and Tool Dockerfiles support the
`CUBE_GUEST_KERNEL_DIGEST` OCI label. The verified image digests are:

```text
runtime: sha256:5d1ea3cee703da47b031b26d8439e240b9d39ffb978e084c482fae1e17764ca7
tool:    sha256:750b71f97322467a23537973c77b23160ff37d2adcdcd32aa7bba07d78c4725b
worker:  sha256:f5fd49858a242efda1e0ea1cc1a896161b048e93348fc2402ad1019ccc8e6056
```

Use image references containing the immutable digest, not a mutable tag. After
a host reboot, confirm the registry container/service is running before
building templates.

## 5. Build fresh templates and validate in order

Run the SDK scripts from an environment with the project dependencies:

```bash
python -m pip install -e '.[dev,postgres]'
export CUBE_API_URL=http://<cube-host>:30030
export CUBE_PROXY_NODE_IP=<cube-host>
export CUBE_PROXY_PORT_HTTP=30080
# Python SDK traffic must bypass any workstation HTTP proxy for the host API.
export NO_PROXY=<cube-host>,localhost,127.0.0.1
export no_proxy="$NO_PROXY"
```

Build unique Runtime and Tool aliases from the two immutable image digests:

```bash
python scripts/register-cube-template.py \
  'http://<cube-host>:5001/clawbox/tool-cube-arm64@sha256:750b71f...' \
  --alias clawbox-tool-<kernel-generation> \
  --node <cube-node> --probe-port 49983

python scripts/register-cube-template.py \
  'http://<cube-host>:5001/clawbox/runtime-cube-arm64@sha256:5d1ea3...' \
  --alias clawbox-runtime-<kernel-generation> \
  --node <cube-node> --probe-port 49983
```

For each returned template ID, query `GET /templates/<id>` and require every
replica to report `kernel_version=sha256-f84e3fa28ae6` before launching it.
The validation order is:

1. `cube-node` healthy and socket visible/usable inside cubelet.
2. Fresh Tool template builds successfully.
3. `scripts/diagnose-cube-kprobes.py` passes, including the manual probe on
   `__arm64_sys_execve`.
4. Tool VM pause/resume succeeds.
5. Fresh Runtime template builds successfully.
6. `scripts/smoke-cubesandbox-agent-pair.py` passes with the fresh IDs.
7. Telemetry is `complete`, joined by the exact execution ID, with valid
   cgroup-v2 and eBPF artifacts.
8. `scripts/audit-cube-sandboxes.py` reports no leaked sandboxes.

The logical agent must remain exactly two VMs: Runtime owns OpenClaw and
prediction instrumentation; Tool owns `/workspace`, repository commands, and
command telemetry. Tool-only pause is the accepted reclamation behavior.
Run the pair smoke from the Worker process/pod, or provide a worker-host
address and port reachable from the Runtime VM; a workstation-local bridge is
not evidence of the Runtime-to-Worker path.

On the Kunpeng validation host, the measured network behavior was: Worker Pod
IP works from Worker and cube-node namespaces, but PodCIDR and ClusterIP are
unroutable from a Cube guest. A temporary NodePort on the node address was
reachable and returned the expected `401`. This means the production bridge
must use a node-routed fixed endpoint (or another route explicitly provided by
the deployment); do not advertise a normal Pod IP until the guest route exists.

## 6. Reboot and rollback notes

After reboot, validate S3lvol/socket, registry, cube-node readiness, active
guest-kernel identity, and the custom component-version directory before
creating templates. The vendor cube-node installer may restore the vendor
kernel during startup, so the custom kernel and component registration must be
reapplied (or made part of the site's privileged startup procedure) before
acceptance testing.

Rollback is allowed only after a zero-sandbox audit: restore the preserved
`vmlinux-bm`, `version`, and `version.json` vendor backups in the privileged
kernel-install container, restart cube-node, and verify `3/3 Running`. Keep
both kernel generations and all existing template/result data.

The BoostKit irqbypass XArray patch is unrelated to this setup and should not
be applied unless later profiling demonstrates a relevant contention problem.
