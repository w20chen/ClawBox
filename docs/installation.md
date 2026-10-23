# Install an experiment host

Install on a dedicated ARM64 Linux host with KVM, systemd, cgroup v2, Docker,
and enough NUMA and storage capacity for the intended experiment. The supported
backend is the patched standalone CubeSandbox release.

The repository does not contain ready-to-run guest images. Obtain these matched
artifacts from a compatible deployment:

- Runtime and Tool VM images.
- `vmlinux-bm`, `version`, and `version.json` for the guest kernel.
- ClawTune source matching both images.
- Access to a registry where the new host can push immutable image references.

Export from an existing host when available:

```bash
bash scripts/export-machine-assets.sh /data/clawbox-transfer \
  --profile "$HOME/.config/clawbox/host.json" --with-clawtune
```

The export includes checksums. It does not export live VMs, host configuration,
credentials, or experiment results.

## 1. Install host tools and ClawBox

The following package names target Ubuntu 24.04 ARM64:

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv python3-dev git docker.io \
  docker-buildx build-essential curl jq rsync numactl e2fsprogs util-linux
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Log out and back in after adding the Docker group, then verify the host and
install the project:

```bash
test "$(uname -m)" = aarch64
test -c /dev/kvm
test "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs
docker info >/dev/null
docker buildx version

mkdir -p "$HOME/src"
cd "$HOME/src"
git clone https://github.com/w20chen/ClawBox.git
cd ClawBox

read -r -p 'Source SSH address (user@host): ' ASSET_HOST
sudo install -d -o "$USER" -g "$(id -gn)" /data/clawbox-transfer
rsync -a "$ASSET_HOST:/data/clawbox-transfer/" /data/clawbox-transfer/
(cd /data/clawbox-transfer && sha256sum -c SHA256SUMS)
tar -xzf /data/clawbox-transfer/clawtune-source.tar.gz -C "$HOME/src"

bash scripts/bootstrap-python.sh \
  --python python3.12 --clawtune "$HOME/src/ClawTune"
source .venv/bin/activate
clawbox experiment --help
```

Reuse existing packages and environments when they satisfy these checks. Use
`--direct-network` only when the installation must bypass inherited proxy
settings. ClawTune, the host Python package, and both VM images must share a
compatible integration contract.

## 2. Build and install CubeSandbox

Build the pinned patched release with the exported kernel:

```bash
cd "$HOME/src/ClawBox"
(cd /data/clawbox-transfer && sha256sum -c SHA256SUMS)
export CUBE_SOURCE_DIR="$HOME/src/CubeSandbox"
bash scripts/build-cubesandbox-release.sh \
  --assets /data/clawbox-transfer --source "$CUBE_SOURCE_DIR" --jobs 1
python -m pip install -e "$CUBE_SOURCE_DIR/sdk/python"
```

The helper pins CubeSandbox v0.7.0, applies the required endpoint, networking,
image-provenance, snapshot, cgroup-routing, and bounded control/lifecycle-RPC patches,
and builds Cubelet and Shim. It does not install or restart services. Repeating the same patch set
reuses the source checkout. If the patch set changes incompatibly, preserve the
old checkout and pass a new empty `--source` directory.

For an existing SDK already carrying `sdk-control-timeouts.patch`, add the
lifecycle timeout patch without rebuilding VM images or restarting services:

```bash
git -C "$CUBE_SOURCE_DIR" apply --check "$PWD/deploy/cubesandbox/sdk-lifecycle-timeouts.patch"
git -C "$CUBE_SOURCE_DIR" apply "$PWD/deploy/cubesandbox/sdk-lifecycle-timeouts.patch"
python -m pip install -e "$CUBE_SOURCE_DIR/sdk/python"
```

If already applied, verify with `git apply --reverse --check` instead of applying
it again. Start a new run ID; existing workers keep their loaded SDK code.

`--mirror cn` selects China package and Go mirrors; `--direct-network` bypasses
inherited host proxies. These options do not solve inaccessible GitHub release
assets. Confirm the build network before retrying a failed build.

Install the exact archive printed by the build:

```bash
read -r -p 'Absolute path of the new release archive: ' RELEASE_ARCHIVE
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

Review `.env` before installation, especially storage devices and ports. If the
deployment uses S3lvol, enable and configure its backend before accepting the
Cubelet as ready. Service startup without a successful smoke test is not an
installation result.

## 3. Import images and save machine settings

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
sudo install -d -o "$USER" -g "$(id -gn)" \
  /data/clawbox-results /data/clawbox-specs
```

Edit `machine.env` and supply:

| Variable | Required value |
| --- | --- |
| `CUBE_API_URL`, `CUBE_NODE` | Installed API URL and actual registered node name |
| `CUBE_PROXY_NODE_IP`, `CUBE_PROXY_PORT_HTTP` | Installed proxy listener |
| `CLAWBOX_CONTROL_HOST`, `CLAWBOX_MODEL_GATEWAY_HOST` | Host address reachable from guests |
| `CLAWBOX_RUNTIME_IMAGE`, `CLAWBOX_TOOL_IMAGE` | Immutable `registry/name@sha256:...` references |
| `CLAWTUNE_ROOT`, `CLAWTUNE_SIDECAR_SRC` | Absolute paths to the matching source |
| `CUBE_SOURCE_DIR` | Patched CubeSandbox checkout |
| `CLAWBOX_OUTPUT_ROOT` | Writable result directory outside the checkout |

Add control and proxy addresses to both `NO_PROXY` and `no_proxy`. Leave template
IDs empty until registration.

## 4. Register templates and verify VM access

Register the standard 1 GiB Runtime and 3 GiB Tool pair:

```bash
cd "$HOME/src/ClawBox"
bash scripts/register-machine-templates.sh \
  --output-dir "$HOME/clawbox-templates/first" \
  --runtime-memory-mib 1024 --tool-memory-mib 3072 \
  --cpu-millicores 2000 --runtime-disk 20G --tool-disk 40G

set -a
source "$HOME/.config/clawbox/machine.env"
set +a
```

The helper registers both templates, waits for READY, verifies kernel binding,
and updates `machine.env` only after both succeed. Use a new output directory and
new template aliases when changing an image or VM size.

Verify CubeSandbox's per-VM TCP endpoint instead of guessing guest IPs:

```bash
python scripts/validate-cubesandbox-tcp-endpoints.py \
  --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" \
  --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
  --node "$CUBE_NODE" --control-host "$CLAWBOX_CONTROL_HOST" \
  --count 1 --output "$CLAWBOX_OUTPUT_ROOT/endpoint-one.json"

python scripts/validate-cubesandbox-tcp-endpoints.py \
  --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" \
  --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
  --node "$CUBE_NODE" --control-host "$CLAWBOX_CONTROL_HOST" \
  --count 4 --output "$CLAWBOX_OUTPUT_ROOT/endpoint-four.json"

python scripts/smoke-cubesandbox-agent-pair.py \
  --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" \
  --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
  --node "$CUBE_NODE" --control-host "$CLAWBOX_CONTROL_HOST" \
  --cancel-probe --output "$CLAWBOX_OUTPUT_ROOT/agent-pair.json"
```

Do not launch formal experiments if endpoint identity, cancellation, snapshot
restore, or guest telemetry fails. BCC telemetry requires headers prepared for
the running guest kernel configuration; build a corrected Tool image and register
a new template if the probe reports invalid kernel coverage.

## 5. Configure the host

Follow the [experiment guide](guide.md#configure-or-recover-a-host) to inspect
NUMA topology, create and review `host.yaml`, run `host check`, and apply it on
an idle VM pool. Match WARM, COLD, and output paths between `machine.env` and the
host YAML.

For tiered snapshots, verify the configured storage:

```bash
python scripts/validate-tiered-storage.py \
  --template "$CLAWBOX_TOOL_TEMPLATE" --node "$CUBE_NODE" \
  --warm "$CLAWBOX_WARM_ROOT" --cold "$CLAWBOX_COLD_ROOT" \
  --helper-image "$CLAWBOX_TOOL_IMAGE" \
  --expected-memory-mib 3072 \
  --output "$CLAWBOX_OUTPUT_ROOT/storage-check.json"
```

This proves the storage path, not the full experiment workflow. Finish with a
smoke run using the current configuration format from the guide.

## After reboot

Check services and storage before applying the saved host plan:

```bash
systemctl is-active cube-sandbox-cube-api.service \
  cube-sandbox-cubemaster.service cube-sandbox-cubelet.service \
  cube-sandbox-cube-egress.service
df -h /data/cubelet
bash scripts/clawbox experiment host check "$HOME/.config/clawbox/host.yaml"
bash scripts/clawbox --sudo experiment host apply \
  "$HOME/.config/clawbox/host.yaml"
```

For S3lvol, also require an active backend and `/var/run/s3lvol.sock`. The
snapshot filesystem must remain below 85% use. Preview recognized crash-dump
cleanup with `experiment host clean`; never delete Cubelet data or Docker volumes
by size. Resume interrupted experiments only after host readiness is restored.

## Verification boundary

Fresh installation, existing-host recovery, unit tests, CLI syntax checks,
qualification, and full VM experiments are different evidence. Accept a new host
only after endpoint, guest telemetry, configured storage, host apply, qualification,
task validation, and cleanup all succeed.
