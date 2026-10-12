# Install an experiment host

Scope: a dedicated ARM64 Linux host with KVM, systemd, cgroup v2, and Docker.
Package commands below target Ubuntu 24.04 ARM64 and install standalone CubeSandbox.
Commands must finish successfully before proceeding.

After installation, use the [self-service workflow](self-service.md) to save
NUMA, capacity and disk settings in one YAML, check them, and run the first experiment.

## Required artifacts

| Input | Required contents |
| --- | --- |
| VM image archive | ARM64 agent VM image (Runtime) with OpenClaw, ClawTune, and the SSH launcher; tool VM image (Tool) with its workspace, dependencies, SSH service, and telemetry collectors |
| Guest kernel | `vmlinux-bm`, `version`, and `version.json`, with the required eBPF/kprobe support |
| ClawTune source | Complete source matching both VM images, including contracts, sidecar and seed data; exported with `--with-clawtune` |
| Registry | A registry reachable from the new host, with permission to push the images |
| Build network | Access to the Python package index, container registry, Git sources and GitHub release assets; a package mirror alone does not cover all of them |

These guest artifacts are not published with this repository. The existing
Dockerfiles reference private base images; cloning the repository alone cannot
rebuild them. Obtain an image/kernel bundle from an existing compatible deployment
or supply equivalent artifacts with the same entrypoints. Task repositories live
in the tool VM image; no particular benchmark trace is an installation dependency.

On an existing deployment, export the images identified by its applied host profile:

```bash
bash scripts/export-machine-assets.sh /data/clawbox-transfer \
  --profile "$HOME/.config/clawbox/host.json" --with-clawtune
```

Without `--profile`, the export uses image references from `machine.env`. The
explicit profile avoids having to copy image digests by hand on an older host.
`--with-clawtune` checks the configured source against both image revisions and
the integration contract before exporting it. Use this matching source on the
new host; a moving ClawTune branch can have an incompatible configuration schema.

## 1. Install host tools and Python package

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv python3-dev git docker.io docker-buildx \
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
docker buildx version
python3.12 --version
mkdir -p "$HOME/src"
cd "$HOME/src"
git clone https://github.com/w20chen/ClawBox.git
cd ClawBox
read -r -p 'Source SSH address (user@host): ' ASSET_HOST
sudo install -d -o "$USER" -g "$(id -gn)" /data/clawbox-transfer
rsync -a "$ASSET_HOST:/data/clawbox-transfer/" /data/clawbox-transfer/
(cd /data/clawbox-transfer && sha256sum -c SHA256SUMS)
test ! -e "$HOME/src/ClawTune"
tar -xzf /data/clawbox-transfer/clawtune-source.tar.gz -C "$HOME/src"
bash scripts/bootstrap-python.sh --python python3.12 --clawtune "$HOME/src/ClawTune"
source .venv/bin/activate
clawbox experiment --help
```

Expected: ARM64, accessible KVM, `cgroup2fs`, a working Docker daemon, and the
experiment command list. Use fresh checkout directories for the clone commands.
On a host that already has the required OS packages, skip their installation and
use its Python >= 3.12 executable with `--python`. The helper reads saved machine
settings when present; explicit source arguments override those paths. Use
`--direct-network` only when installation should bypass inherited HTTP/SOCKS
proxies. `--venv PATH` installs an isolated environment; select it later through
`CLAWBOX_PYTHON=PATH/bin/python`.

### Optional: rebuild images from a compatible ClawTune checkout

Skip this subsection when installing from a supplied guest-image bundle.
Rebuilding requires access to the private base images referenced by the
Dockerfiles. Select a ClawTune Git checkout compatible with this ClawBox revision
and export it into a new directory. The bundled source is already matched to the
supplied images; updating it is a separate operation. Check the integration
contract before installing a replacement or building images:

```bash
read -r -p 'Absolute path of compatible ClawTune Git checkout: ' CLAWTUNE_CHECKOUT
python scripts/prepare-clawtune.py --clawtune-root "$CLAWTUNE_CHECKOUT" --working-tree --output /data/clawtune-build-source
export CLAWTUNE_ROOT=/data/clawtune-build-source
export CLAWTUNE_SIDECAR_SRC="$CLAWTUNE_ROOT/services/sidecar/src"
python scripts/validate_clawtune_integration.py --clawtune-root "$CLAWTUNE_ROOT"
python -m pip install --upgrade "$CLAWTUNE_ROOT/services/sidecar"
CLAWTUNE_REVISION="$(cat "$CLAWTUNE_ROOT/CLAWTUNE_REVISION")"
export CLAWTUNE_REVISION
docker buildx build --load --platform linux/arm64 \
  --build-context "clawtune=$CLAWTUNE_ROOT" --build-arg "CLAWTUNE_REVISION=$CLAWTUNE_REVISION" \
  -f docker/Dockerfile.runtime-cube -t clawbox/runtime-cube:main .
docker buildx build --load --platform linux/arm64 \
  --build-context "clawtune=$CLAWTUNE_ROOT" --build-arg "CLAWTUNE_REVISION=$CLAWTUNE_REVISION" \
  -f docker/Dockerfile.tool-cube -t clawbox/tool-cube:main .
```

Use the same export for both images, then push and register their immutable
image digests using steps 3 and 4 below. A clean checkout records its commit; local
edits produce a content-derived revision. Use a new export directory for each
update.

Each agent VM starts with independent prediction state. If setting
`CLAWTUNE_COLD_START_DIR`, supply a complete seed directory with its manifest
and required snapshots. After a change to the stored prediction format, use a
new run directory and retrain from raw measurements; old snapshots are not
converted automatically. Guest eBPF uses ClawTune's collector, so the running
guest kernel and BCC headers must match.

## 2. Build and install patched CubeSandbox

Using the bundle already copied in step 1, prepare the server from
`$HOME/src/ClawBox`:

```bash
(cd /data/clawbox-transfer && sha256sum -c SHA256SUMS)
test -s /data/clawbox-transfer/guest-images.tar
test -s /data/clawbox-transfer/kernel/vmlinux-bm
test -s /data/clawbox-transfer/kernel/version.json
export CUBE_SOURCE_DIR="$HOME/src/CubeSandbox"
bash scripts/build-cubesandbox-release.sh \
  --assets /data/clawbox-transfer --source "$CUBE_SOURCE_DIR" --jobs 1
python -m pip install -e "$CUBE_SOURCE_DIR/sdk/python"
```

The build helper checks bundle hashes, prepares the patched source, preserves an
existing `deploy/one-click/build.env`, and builds with the supplied kernel. It uses
a user-writable Buildx activity directory at `~/.cache/clawbox/buildx`, avoiding
permission conflicts left by earlier root Docker builds. It does not install or
restart services. `--jobs` controls build concurrency; `CUBE_SOURCE_URL` can select
a reachable Git mirror of the pinned upstream tag. A build failure stops here;
keep its log and rerun after resolving the reported dependency or network error.
The first build downloads dependencies and can take a long time. While a component
is running, its live log is under `$CUBE_SOURCE_DIR/deploy/one-click/.work/build-logs/`;
the terminal prints that component's output when it finishes. Keep the checkout
and build cache to reuse successful downloads on a retry.
Use `--mirror cn` for China APT/LLVM mirrors and the Go module proxy; the
default is `--mirror upstream`. `--direct-network` bypasses inherited proxy
environment variables for host-side source preparation and build commands.
`--go-proxy URL` (or `GOPROXY`) selects another Go module proxy. The default is
`https://proxy.golang.org,direct`, or `https://goproxy.cn,direct` with `--mirror cn`;
module checksum verification stays enabled.
These mirrors do not cover GitHub release assets. Before a full build,
check that the required release asset is reachable from the build environment:

```bash
curl -IL --max-time 30 \
  https://github.com/protocolbuffers/protobuf/releases/download/v28.3/protoc-28.3-linux-aarch_64.zip
```

If this times out, configure a working network/proxy that both the host and Docker
build containers can reach before building. A proxy on host `127.0.0.1` is not the
same endpoint as `127.0.0.1` inside a container. Do not bypass a failed build and
install an older archive found in the output directory.

The preparation script pins v0.7.0 and applies the repository's endpoint,
networking, image-provenance, snapshot, and AgentCgroup priority patches. Rebuild
and install both Cubelet and Shim: they place each VM in its session's cgroup
before allocating guest RAM, including cached-template creation and restore.
Use the matching SDK. Applying a patch without installing the rebuilt services
does not enable priority control.
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

For the usual matched pair, use the registration helper from the ClawBox checkout:

```bash
bash scripts/register-machine-templates.sh --output-dir "$HOME/clawbox-templates/first" \
  --runtime-memory-mib 2048 --tool-memory-mib 4096 \
  --cpu-millicores 2000 --runtime-disk 20G --tool-disk 40G
set -a
source "$HOME/.config/clawbox/machine.env"
set +a
```

It reads immutable image references and the node from `machine.env`, reads the
installed guest-kernel component, registers both templates, waits for READY,
checks kernel binding, and saves both IDs to `machine.env` only after both succeed.
The output directory retains aliases, registration records, and a private backup
of the previous settings. An existing output directory is refused. After an
ambiguous registration failure, inspect its aliases and the CubeSandbox template
inventory before submitting again. Use `--machine-env FILE` for another machine
file and `--kernel-manifest FILE` for a nonstandard kernel installation.

When resizing an existing deployment, choose a new output directory, then update
the template IDs in `host.yaml`, run host check/apply, and configure new experiment
YAMLs. CLI resource declarations do not resize an existing template.
Add `--profile "$HOME/.config/clawbox/host.json"` to reuse the applied image
digests and node when these are absent from an older `machine.env`. After both
registrations succeed, the helper also saves these image references and node.

Check CubeSandbox's semantic TCP endpoint, which identifies a VM and port,
instead of guessing a guest IP:

```bash
python scripts/validate-cubesandbox-tcp-endpoints.py \
  --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
  --node "$CUBE_NODE" --control-host "$CLAWBOX_CONTROL_HOST" \
  --count 1 --output "$CLAWBOX_OUTPUT_ROOT/endpoint-one.json"
```

Expected: endpoint reachability and identity checks succeed. Repeat with
`--count 4` and a new output filename before testing higher concurrency.
Check command cancellation and telemetry across a snapshot/restore cycle too:

```bash
python scripts/smoke-cubesandbox-agent-pair.py \
  --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
  --node "$CUBE_NODE" --control-host "$CLAWBOX_CONTROL_HOST" --cancel-probe \
  --output "$CLAWBOX_OUTPUT_ROOT/agent-pair.json"
```

### If the tool VM kernel headers do not match

BCC requires headers prepared for the running guest kernel configuration, not
only the same version. If the agent-pair telemetry probe reports invalid or
missing eBPF CPU/memory measurements, build a corrected tool VM image from the
initial tool VM template:

```bash
python scripts/prepare-guest-kernel-headers.py \
  --template "$CLAWBOX_TOOL_TEMPLATE" --node "$CUBE_NODE" \
  --image "$CLAWBOX_TOOL_IMAGE" --tag "$REGISTRY/tool:matching-headers" \
  --output /data/clawbox-specs/header-build-01
docker push "$REGISTRY/tool:matching-headers"
CLAWBOX_TOOL_IMAGE=$(docker image inspect "$REGISTRY/tool:matching-headers" \
  --format '{{index .RepoDigests 0}}')
export CLAWBOX_TOOL_IMAGE
```

This reads `/proc/config.gz` from a temporary guest and preserves the build
log. Register a **new** tool VM template with a fresh alias and the corrected
image digest:

```bash
GUEST_KERNEL_COMPONENT=$(jq -er '.variants.bm.version' \
  /usr/local/services/cubetoolbox/cube-kernel-scf/version.json)
TEMPLATE_SUFFIX=$(date -u +%Y%m%d%H%M%S)
python scripts/register-cube-template.py "$CLAWBOX_TOOL_IMAGE" \
  --alias "tool-headers-$TEMPLATE_SUFFIX" --node "$CUBE_NODE" \
  --cpu-millicores 2000 --memory-mib 4096 --writable-layer-size 40G \
  --exposed-port 49983 --exposed-port 2222 --probe-port 49983 \
  --command /usr/local/bin/cube-tool-entrypoint.sh \
  --expected-kernel-version "$GUEST_KERNEL_COMPONENT" \
  > /data/clawbox-specs/tool-template-headers.json
CLAWBOX_TOOL_TEMPLATE=$(jq -er .template_id \
  /data/clawbox-specs/tool-template-headers.json)
export CLAWBOX_TOOL_TEMPLATE
```

Save `CLAWBOX_TOOL_TEMPLATE` and `CLAWBOX_TOOL_IMAGE` in `machine.env`, then
repeat the endpoint and agent-pair probes. Do not launch formal runs until
the telemetry probe passes.

Then use the [step-by-step guide](self-service.md) to inspect the NUMA layout,
apply an editable host configuration, and run the starter experiment.

## Configure memory and snapshot storage

Use the editable host YAML described in the [self-service workflow](self-service.md)
and [host and memory reference](supernode.md). `host inspect` reads the actual NUMA
topology; `host check` checks prerequisites; `host apply` configures the
VM cgroup, shared-pool tmpfs, snapshot roots, and CubeSandbox services on an
idle host. The example uses NUMA 0 and 1 for compute, NUMA 2 for the shared pool.
Change node IDs, CPU lists, memory capacities and storage paths to fit the host.
The resulting experiment spec must use the same templates and snapshot roots.
Match `CLAWBOX_WARM_ROOT`, `CLAWBOX_COLD_ROOT`, and `CLAWBOX_OUTPUT_ROOT` in
`machine.env` to `warm_root`, `cold_root`, and `output_root` in the host YAML;
source the file again before the storage probe.

For tiered snapshots, run a live storage probe after applying the host YAML:

```bash
python scripts/validate-tiered-storage.py \
  --template "$CLAWBOX_TOOL_TEMPLATE" --node "$CUBE_NODE" \
  --warm "$CLAWBOX_WARM_ROOT" --cold "$CLAWBOX_COLD_ROOT" \
  --helper-image "$CLAWBOX_TOOL_IMAGE" \
  --expected-memory-mib 4096 --output "$CLAWBOX_OUTPUT_ROOT/storage-check.json"
```

Set `--expected-memory-mib` to the registered tool VM template memory.
The probe reports checkpoint and restore times, allocated bytes and disk I/O.
It tests storage behavior; the full Agent experiment is the acceptance step.

## After a reboot

On an installed host, check the API and compute services before starting work:

```bash
systemctl is-active cube-sandbox-cube-api.service cube-sandbox-cubemaster.service \
  cube-sandbox-cubelet.service cube-sandbox-cube-egress.service
```

On an idle pool, run `clawbox experiment host check HOST.yaml`, then
`clawbox experiment host apply HOST.yaml` after reboot before launching a run.
Use the saved host YAML, not an old machine-specific example. Re-run the live
storage probe above when tiered storage is selected.

For an S3lvol-backed installation, also check `cube-sandbox-s3lvol.service` and
`test -S /var/run/s3lvol.sock`. Restore its configured backend before restarting
Cubelet if the socket is missing. Reapply the saved host YAML on an idle pool.

When using the bundled MinIO backend, start it before S3lvol:

```bash
sudo systemctl start cube-sandbox-minio.service
sudo systemctl start cube-sandbox-s3lvol.service
test -S /var/run/s3lvol.sock
```

An active S3lvol supervisor alone is not sufficient: it may be retrying while
MinIO is unavailable. Check the socket and service log before creating VMs.
If the host address changed, check that `/data/cubelet/s3.cfg` points to the
actual MinIO listener and uses credentials accepted by that server. Preserve a
copy before editing; do not print credentials into experiment logs.

Also check free disk space with `df -h /data/cubelet`. The pinned CubeSandbox
snapshot backend rejects new writes at 85% filesystem usage. This can surface
as `no more resource` even when CPU and RAM are idle; the CubeMaster log then
reports `snapshot storage unavailable`. Remove confirmed obsolete crash dumps
or unused build cache, keeping templates, recordings and experiment results.
Allow additional space for snapshots during the run, not just enough to pass
the initial check. Run the storage validation above again before experiments.

Preview and remove old crash dumps using the maintained cleanup command:

```bash
bash scripts/clawbox --sudo experiment host clean
bash scripts/clawbox --sudo experiment host clean --apply
df -h /data/cubelet
```

The default retention is 24 hours and minimum size 100 MiB. For completed recent
dumps use `--older-than-hours 1` on both commands. Only recognized dump files are
selected; VM storage and results are retained. See [disk maintenance](self-service.md).

For an interrupted run, keep its result directory and launch the same frozen
YAML with the same run ID after restoring the host. Choose a new ID when changing
the experiment.
Disable the old kubelet on a dedicated host migrated from Kubernetes, after
confirming it has no unrelated workloads.

## Verification boundary

Do not use service startup alone as installation acceptance. Run the endpoint,
Guest kernel/telemetry and configured snapshot checks on each target host, then
complete the self-service smoke experiment before increasing concurrency.
