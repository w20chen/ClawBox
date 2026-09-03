# CubeSandbox v0.7.0 on Kunpeng 920B

ClawBox uses native ARM64 KVM through CubeSandbox. PVM is explicitly disabled.
The pin is tag `v0.7.0`, commit
`d0081641c59822e4e5653b7462e914410b81910a`, with Python package
`cubesandbox==0.7.0`.

The target node needs Kubernetes, containerd with a working CRI v1 endpoint,
Helm, `/dev/kvm`, cgroup v2, and at least 210 GiB free beneath `/data`. The
initial profile creates a 200G loopback XFS filesystem at `/data/cubelet`.
If `/data/cubelet` is already a mounted reflink-capable XFS filesystem, the
script uses it without formatting a disk. Pause snapshots live below the same
allocation at `/data/cubelet/snapshot_pack`.

Set passwords in the environment, then install:

```bash
export CUBE_MYSQL_PASSWORD='...'
export CUBE_MYSQL_ROOT_PASSWORD='...'
export CUBE_REDIS_PASSWORD='...'
scripts/install-cubesandbox-kunpeng920.sh check
scripts/install-cubesandbox-kunpeng920.sh install
```

The chart is layered as upstream `values-single-node.yaml` followed by
`deploy/cubesandbox/runtime-values-kunpeng920.yaml`. Because v0.7.0 has no Helm
values for two required controls, the installer deterministically changes the
pinned source inputs before rendering: CubeMaster CPU/memory overcommit is
`1.0/1.0`, and Cubelet `host.quota.paused_resource_release_ratio` is `1.0`.
These settings must remain fixed across all arms.

Register one ARM64 template, then run the lifecycle smoke test from a machine
that can reach CubeAPI and CubeProxy:

```bash
export CUBE_API_URL=http://127.0.0.1:30030
export CUBE_PROXY_NODE_IP=127.0.0.1
export CUBE_PROXY_PORT_HTTP=30080
python scripts/register-cube-template.py ghcr.io/example/task@sha256:... --alias clawbox-task --node NODE
python scripts/smoke-cubesandbox-kunpeng920.py --template clawbox-task --node NODE
```

Use `status` for inspection and `uninstall` to remove Kubernetes objects.
Uninstall intentionally retains `/data/cubelet` and database paths.

The BoostKit irqbypass XArray patch is not used. It may be evaluated later
only after profiling the stock kernel.
