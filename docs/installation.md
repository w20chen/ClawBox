# Install an experiment host

Scope: a dedicated ARM64 Linux host with KVM, systemd, cgroup v2, and Docker.
Package commands below target Ubuntu 24.04 ARM64 and install standalone CubeSandbox.
Commands must finish successfully before proceeding.

## Required artifacts

| Input | Required contents |
| --- | --- |
| Guest image archive | ARM64 Runtime image with OpenClaw, ClawTune and the SSH launcher; Tool image with its workspace, dependencies, SSH service and telemetry collectors |
| Guest kernel | `vmlinux-bm`, `version`, `version.json`, with the required eBPF/kprobe support |
| Registry | A registry reachable from the new host, with permission to push the images |

These guest artifacts are not published with this repository. The existing
Dockerfiles reference private base images; cloning the repository alone cannot
rebuild them. Obtain an image/kernel bundle from an existing compatible deployment
or supply equivalent artifacts with the same entrypoints. Task repositories live
in the Tool image; no particular benchmark trace is an installation dependency.

On an existing deployment whose `machine.env` identifies the image digests:

```bash
bash scripts/export-machine-assets.sh /data/clawbox-transfer
```

## 1. Install host tools and Python package

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv python3-dev git docker.io \
  build-essential curl jq rsync numactl e2fsprogs util-linux
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Log out and back in to activate Docker group membership. Then:

```bash
test "$(uname -m)" = aarch64
test -c /dev/kvm
test "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs
docker info >/dev/null
python3.12 --version
mkdir -p "$HOME/src"
cd "$HOME/src"
git clone https://github.com/w20chen/ClawBox.git
git clone https://github.com/w20chen/ClawTune.git
cd ClawBox
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
source .venv/bin/activate
clawbox experiment --help
```

Expected: ARM64, accessible KVM, `cgroup2fs`, a working Docker daemon, and the
experiment command list. Use fresh checkout directories for the clone commands.

## 2. Build and install patched CubeSandbox

Copy the supplied bundle to the destination, then prepare the server from
`$HOME/src/ClawBox`:

```bash
read -r -p 'Source SSH address (user@host): ' ASSET_HOST
sudo install -d -o "$USER" -g "$(id -gn)" /data/clawbox-transfer
rsync -a "$ASSET_HOST:/data/clawbox-transfer/" /data/clawbox-transfer/
test -s /data/clawbox-transfer/guest-images.tar
test -s /data/clawbox-transfer/kernel/vmlinux-bm
test -s /data/clawbox-transfer/kernel/version.json
export CUBE_SOURCE_DIR="$HOME/src/CubeSandbox"
bash deploy/cubesandbox/prepare-semantic-source.sh
python -m pip install -e "$CUBE_SOURCE_DIR/sdk/python"
cd "$CUBE_SOURCE_DIR"
cp deploy/one-click/build.env.example deploy/one-click/build.env
make builder-image
ONE_CLICK_BUILD_JOBS=1 \
ONE_CLICK_CUBE_KERNEL_VMLINUX=/data/clawbox-transfer/kernel/vmlinux-bm \
bash deploy/one-click/build-release-bundle-builder.sh
```

The preparation script pins v0.7.0 and applies the repository's endpoint,
networking, image-provenance, and snapshot patches. Use its matching SDK.
Build variables are defined in the pinned [upstream build configuration](https://github.com/TencentCloud/CubeSandbox/blob/v0.7.0/deploy/one-click/build.env.example).

Select the exact archive printed by the build; do not extract a wildcard of old
archives. The installer uses `/usr/local/services/cubetoolbox`:

```bash
read -r -p 'Absolute path of newly built release archive: ' RELEASE_ARCHIVE
test -s "$RELEASE_ARCHIVE"
INSTALL_STAGE=$(mktemp -d /tmp/clawbox-install.XXXXXX)
tar -xzf "$RELEASE_ARCHIVE" -C "$INSTALL_STAGE"
cd "$INSTALL_STAGE"/cube-sandbox-one-click-*
cp env.example .env
read -r -p 'Host IPv4 address reachable from guests: ' HOST_IP
printf '\nCUBE_SANDBOX_NODE_IP=%s\n' "$HOST_IP" >> .env
sudo bash install.sh
sudo bash smoke.sh
systemctl list-units 'cube-sandbox-*' --no-pager
```

Before running `install.sh`, review `.env` for storage devices and conflicting
ports on this host. The commands follow the pinned [standalone installer](https://github.com/TencentCloud/CubeSandbox/blob/v0.7.0/deploy/one-click/README.md#target-machine-installation).
If your Cubelet storage uses S3lvol, set `ONE_CLICK_ENABLE_S3LVOL=1` before
installation and configure its S3/MinIO backend using the supplied environment
file. The upstream default is disabled; an active Cubelet alone does not prove
that storage is ready.
Expected: the smoke command succeeds and services are active. A failed smoke
check must be resolved before template registration.

## 3. Import images and write machine settings

```bash
cd "$HOME/src/ClawBox"
source .venv/bin/activate
docker load -i /data/clawbox-transfer/guest-images.tar
read -r -p 'Registry namespace (host[:port]/namespace): ' REGISTRY
docker tag clawbox-transfer/runtime:research "$REGISTRY/runtime:experiment"
docker tag clawbox-transfer/tool:research "$REGISTRY/tool:experiment"
docker push "$REGISTRY/runtime:experiment"
docker push "$REGISTRY/tool:experiment"
docker image inspect --format '{{json .RepoDigests}}' "$REGISTRY/runtime:experiment"
docker image inspect --format '{{json .RepoDigests}}' "$REGISTRY/tool:experiment"
mkdir -p "$HOME/.config/clawbox"
cp examples/clawbox-machine.env.example "$HOME/.config/clawbox/machine.env"
chmod 600 "$HOME/.config/clawbox/machine.env"
sudo install -d -o "$USER" -g "$(id -gn)" /data/clawbox-results /data/clawbox-specs
```

Authenticate with `docker login` first if required by your registry. Edit
`machine.env` using the values from this installation:

| Variable | Value to supply |
| --- | --- |
| `CUBE_API_URL` | CubeAPI URL; `http://127.0.0.1:3000` for a local default installation |
| `CUBE_NODE` | Actual compute-node registration name, not an old experiment's hostname |
| `CUBE_PROXY_NODE_IP`, `CUBE_PROXY_PORT_HTTP` | Proxy listener address and HTTP port from the installed configuration |
| `CLAWBOX_CONTROL_HOST`, `CLAWBOX_MODEL_GATEWAY_HOST` | Host IP reachable from guest VMs; default listeners use TCP 18080 and 18081 |
| `CLAWBOX_RUNTIME_IMAGE`, `CLAWBOX_TOOL_IMAGE` | Corresponding pushed `registry/name@sha256:...` references from `RepoDigests` |
| `CLAWTUNE_ROOT` | `$HOME/src/ClawTune`, expanded to an absolute path |
| `CLAWTUNE_SIDECAR_SRC` | Absolute `ClawTune/services/sidecar/src` path |
| `CUBE_SOURCE_DIR` | Absolute patched CubeSandbox checkout path |
| `CLAWBOX_OUTPUT_ROOT` | Writable result directory outside the checkout |
| `NO_PROXY`, `no_proxy` | Control/proxy host addresses plus `localhost,127.0.0.1` |

Leave template IDs unset until registration. Snapshot paths are only used for
tiered storage. The file is a shell environment file, loaded explicitly:

```bash
set -a
source "$HOME/.config/clawbox/machine.env"
set +a
curl -fsS "$CUBE_API_URL/health"
python scripts/audit-cube-sandboxes.py --json
```

## 4. Register templates and verify SSH

Read the installed guest-kernel component and register new aliases. The commands
match the starting example's VM sizes; disk capacity belongs to the template:

```bash
GUEST_KERNEL_COMPONENT=$(jq -er '.variants.bm.version' \
  /usr/local/services/cubetoolbox/cube-kernel-scf/version.json)
TEMPLATE_SUFFIX=$(date -u +%Y%m%d%H%M%S)
python scripts/register-cube-template.py "$CLAWBOX_RUNTIME_IMAGE" \
  --alias "runtime-$TEMPLATE_SUFFIX" --node "$CUBE_NODE" \
  --cpu-millicores 2000 --memory-mib 2048 --writable-layer-size 20G \
  --exposed-port 49983 --probe-port 49983 \
  --command /usr/local/bin/cube-runtime-entrypoint.sh \
  --expected-kernel-version "$GUEST_KERNEL_COMPONENT" > /data/clawbox-specs/runtime-template.json
python scripts/register-cube-template.py "$CLAWBOX_TOOL_IMAGE" \
  --alias "tool-$TEMPLATE_SUFFIX" --node "$CUBE_NODE" \
  --cpu-millicores 2000 --memory-mib 4096 --writable-layer-size 40G \
  --exposed-port 49983 --exposed-port 2222 --probe-port 49983 \
  --command /usr/local/bin/cube-tool-entrypoint.sh \
  --expected-kernel-version "$GUEST_KERNEL_COMPONENT" > /data/clawbox-specs/tool-template.json
export CLAWBOX_RUNTIME_TEMPLATE=$(jq -er .template_id /data/clawbox-specs/runtime-template.json)
export CLAWBOX_TOOL_TEMPLATE=$(jq -er .template_id /data/clawbox-specs/tool-template.json)
```

Save both IDs in `machine.env`. Registration waits for READY and checks kernel
binding. If it fails ambiguously, inspect the alias inventory before resubmitting.
Check the semantic TCP endpoint instead of guessing a guest IP:

```bash
python scripts/validate-cubesandbox-tcp-endpoints.py \
  --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
  --node "$CUBE_NODE" --control-host "$CLAWBOX_CONTROL_HOST" \
  --count 1 --output "$CLAWBOX_OUTPUT_ROOT/endpoint-one.json"
```

Expected: endpoint reachability and identity checks succeed. Repeat with
`--count 4` and a new output filename before testing higher concurrency.
Then follow [configure and run](guide.md#5-run-on-an-installed-host).

## Optional: isolated memory and tiered snapshots

This section requires two NUMA nodes and an idle CubeSandbox VM pool. Choose
capacities from available node memory; the numbers below are example budgets,
not universal requirements. Inspect the nodes first:

```bash
numactl --hardware
cat /sys/fs/cgroup/cube_sandbox/sandbox/cgroup.events
```

Continue only if `populated 0`. Set paths in `machine.env` and matching values in
the experiment's `resources` section:

```yaml
pool_memory_budget_mib: 16384
local_memory_capacity_mib: 16384
warm_memory_capacity_mib: 8192
checkpoint_restore_headroom_mib: 1024
local_memory_cgroup: /sys/fs/cgroup/cube_sandbox/sandbox
local_numa_node: 0
warm_numa_node: 1
warm_snapshot_root: /data/clawbox/warm
cold_snapshot_root: /data/clawbox/cold
```

On the idle host, install the storage settings and activate the service flags:

```bash
sudo env CLAWBOX_LOCAL_MIB=16384 CLAWBOX_WARM_MIB=8192 \
  CLAWBOX_LOCAL_NODE=0 CLAWBOX_WARM_NODE=1 \
  bash scripts/setup-tiered-memory.sh /data/clawbox/warm /data/clawbox/cold "$USER"
sudo systemctl restart cube-sandbox-cubelet.service
sudo systemctl restart cube-sandbox-cube-egress.service
sudo env CLAWBOX_LOCAL_MIB=16384 CLAWBOX_WARM_MIB=8192 \
  CLAWBOX_LOCAL_NODE=0 CLAWBOX_WARM_NODE=1 \
  bash scripts/setup-tiered-memory.sh /data/clawbox/warm /data/clawbox/cold "$USER"
cat /sys/fs/cgroup/cube_sandbox/sandbox/memory.max
cat /sys/fs/cgroup/cube_sandbox/sandbox/memory.swap.max
findmnt -n -o TARGET,FSTYPE,OPTIONS --target /data/clawbox/warm
test -w /sys/fs/cgroup/cube_sandbox/sandbox/memory.reclaim
```

Expected: `memory.max` is `17179869184`, swap is `0`, and the snapshot mount is
tmpfs with `mpol=bind:1`, `noswap`, and the requested size. Repeat the setup command
after reboot or service recreation; restart services only to activate changed
service flags. Never restart them during an experiment.

Validate physical snapshot placement:

```bash
python scripts/validate-tiered-storage.py \
  --template "$CLAWBOX_TOOL_TEMPLATE" --node "$CUBE_NODE" \
  --warm /data/clawbox/warm --cold /data/clawbox/cold \
  --helper-image "$CLAWBOX_TOOL_IMAGE" --require-local-numa 0 \
  --expected-memory-mib 4096 --output "$CLAWBOX_OUTPUT_ROOT/storage-check.json"
```

This helper uses privileged Docker inspection. Memory setup, endpoint checks,
and storage checks do not establish a completed agent benchmark.

## After a reboot

On an installed host, check the API and compute services before starting work:

```bash
systemctl is-active cube-sandbox-cube-api.service cube-sandbox-cubemaster.service \
  cube-sandbox-cubelet.service cube-sandbox-cube-egress.service
```

For an S3lvol-backed installation, also check `cube-sandbox-s3lvol.service` and
`test -S /var/run/s3lvol.sock`. Restore its configured backend before restarting
Cubelet if the socket is missing. Reapply the memory setup above on an idle pool.

When using the bundled MinIO backend, start it before S3lvol:

```bash
sudo systemctl start cube-sandbox-minio.service
sudo systemctl start cube-sandbox-s3lvol.service
test -S /var/run/s3lvol.sock
```

An active S3lvol supervisor alone is not sufficient: it may be retrying while
MinIO is unavailable. Check the socket and service log before creating VMs.
Keep interrupted results and start with a new run ID; reboot does not resume a run.
Disable the old kubelet on a dedicated host migrated from Kubernetes, after
confirming it has no unrelated workloads.

## Verification boundary

CLI examples and repository helper arguments are checked against source. The
upstream release commands are checked against the pinned v0.7.0 files. A fresh
physical-host installation has not been executed as part of this documentation
change; the VM and kernel checks above remain required on the target host.
