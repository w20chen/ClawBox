# Historical Kunpeng Kubernetes deployment

> Historical record only. This deployment returned a Kubernetes Pod IP that
> the Runtime VM could not use as the final native-SSH address. Do not use it
> for new ClawBox results. Follow [CubeSandbox setup](cubesandbox-setup.md).

This file retains the host-specific facts needed to understand earlier
Kunpeng diagnostics without presenting them as the normal installation path.

## Host and storage

The historical host used ARM64 Kunpeng 920B, Kubernetes, containerd/CRI v1,
Helm, KVM, cgroup v2, and a reflink-capable XFS filesystem at `/data/cubelet`.
CubeS3lvol ran as a host service and exposed `/var/run/s3lvol.sock` to the
CubeSandbox node components.

The source-controlled installer is:

```bash
export CUBE_MYSQL_PASSWORD='<value>'
export CUBE_MYSQL_ROOT_PASSWORD='<value>'
export CUBE_REDIS_PASSWORD='<value>'
bash scripts/install-cubesandbox-kunpeng920.sh check
bash scripts/install-cubesandbox-kunpeng920.sh install
```

It pins CubeSandbox `v0.7.0`, applies
`deploy/cubesandbox/runtime-values-kunpeng920.yaml`, and mounts only the
S3lvol socket into Cubelet. The node was considered ready only when every
cube-node container was running and Cubelet could use S3lvol.

## Guest kernel

The diagnostic build used OpenCloudOS kernel `6.6.119-49.6` with:

```text
CONFIG_KPROBES=y
CONFIG_KRETPROBES=y
CONFIG_FTRACE_SYSCALLS=y
sha256:f84e3fa28ae692f34645aa3c7034999242760eb25aab0ea667b43f16ac12c27f
```

The configuration and version records remain in:

```text
deploy/cubesandbox/kernel-oc9-arm64-kprobes.config.patch
deploy/cubesandbox/kernel-oc9-arm64-kprobes.version
deploy/cubesandbox/kernel-oc9-arm64-kprobes.version.json
```

Installation was performed by the idempotent helper, which verifies the
checksum, preserves the vendor kernel, registers the component version, and
restarts cube-node only when necessary:

```bash
bash scripts/install-kprobe-kernel-kunpeng920.sh \
  /path/to/vmlinux <cube-node>
```

The active component name was `sha256-f84e3fa28ae6`. Templates built with a
different kernel record were rejected rather than silently reused.

## Historical image records

The images used by that deployment were recorded as:

```text
runtime: sha256:5d1ea3cee703da47b031b26d8439e240b9d39ffb978e084c482fae1e17764ca7
tool:    sha256:750b71f97322467a23537973c77b23160ff37d2adcdcd32aa7bba07d78c4725b
worker:  sha256:f5fd49858a242efda1e0ea1cc1a896161b048e93348fc2402ad1019ccc8e6056
```

These hashes are historical provenance, not current template defaults.

## Validation order used at the time

1. Check CubeS3lvol and cube-node health.
2. Build a new Tool template against the recorded kernel.
3. Run `scripts/diagnose-cube-kprobes.py`.
4. Check Tool pause and restore.
5. Build a new Runtime template.
6. Run `scripts/smoke-cubesandbox-agent-pair.py`.
7. Verify the exact execution-ID join for cgroup and eBPF data.
8. Verify that no owned sandbox remains.

These checks established VM lifecycle and telemetry only. They did not prove
the final Runtime-to-Tool network route, which is why this topology was
retired from the supported path.

## Reboot and rollback notes

After a reboot, the registry, S3lvol socket, cube-node, guest-kernel component,
and template records all had to be checked again. The vendor installer could
restore the vendor guest kernel during startup, so the custom component might
need to be reinstalled before creating a new template.

One observed reboot failure was caused by a missing `cube-dev` interface while
S3lvol remained healthy. That was a CubeVS/node-network startup problem, not a
reason to change the Tool route or disable telemetry.

Rollback was allowed only after confirming that no experiment VM remained.
The preserved vendor kernel files were restored, cube-node was restarted, and
all components were checked again. Existing templates and result directories
were retained.
