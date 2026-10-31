# Install an experiment host

Install on a dedicated ARM64 Linux host with KVM, systemd, cgroup v2, Docker,
and enough NUMA and storage capacity for the intended experiment. The supported
backend is the patched standalone CubeSandbox release.

For an installed host, use [updates](#update-an-existing-host) or
[recovery](#recover-after-reboot). The steps below provision a new host; they do
not need repeating for ordinary source or KB updates. Commands run from the
ClawBox checkout unless a block explicitly changes directory.

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
image-provenance, snapshot, cgroup-routing, bounded RPC, and guest-agent reconnect
patches, and builds Cubelet and Shim. It does not install or restart services.
Repeating the same patch set
reuses the source checkout. If the patch set changes incompatibly, preserve the
old checkout and pass a new empty `--source` directory.

Managed NUMA placement starts shims directly in their assigned cgroup. The host
must use cgroup v2 and permit `clone3(CLONE_INTO_CGROUP)` (Linux 5.7 or newer).
Install matching Cubelet and CubeShim binaries from the same prepared source;
guest templates do not need rebuilding for this placement update.

`--mirror cn` selects China package and Go mirrors; `--direct-network` bypasses
inherited host proxies. GitHub release assets must also be reachable.

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
Cubelet as ready. Continue after `smoke.sh` passes.

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
| `CLAWTUNE_KB_SEED` | Optional immutable seed path; defaults to ClawTune `bootstrap-v2`. Applies to new state and image builds; existing state is retained. |
| `CUBE_SOURCE_DIR` | Patched CubeSandbox checkout |
| `CLAWBOX_OUTPUT_ROOT` | Writable result directory outside the checkout |

Add control and proxy addresses to both `NO_PROXY` and `no_proxy`. Leave template
IDs empty until registration.

The normal `experiment launch` path reuses saved workload readiness metadata and
does not repeat a full corpus audit. Use `experiment validate SPEC --inputs` after
you intentionally replace trace or prediction files; it is the slower command that
reparses and rehashes all inputs.

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

Verify CubeSandbox's semantic TCP endpoint instead of guessing guest IPs:

```bash
python scripts/validate-cubesandbox-tcp-endpoints.py \
  --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" \
  --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
  --node "$CUBE_NODE" --control-host "$CLAWBOX_CONTROL_HOST" \
  --count 4 --output "$CLAWBOX_OUTPUT_ROOT/endpoint-check.json"

python scripts/smoke-cubesandbox-agent-pair.py \
  --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" \
  --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
  --node "$CUBE_NODE" --control-host "$CLAWBOX_CONTROL_HOST" \
  --cancel-probe --output "$CLAWBOX_OUTPUT_ROOT/agent-pair.json"
```

These probes check endpoint identity, cancellation, snapshot restore, and guest
telemetry. Resolve probe failures before launching. BCC requires headers prepared for
the running guest kernel configuration; build a corrected Tool image and register
a new template if the probe reports invalid kernel coverage.

## 5. Configure the host

Create an editable plan from detected topology and saved machine settings:

```bash
bash scripts/clawbox experiment host inspect
bash scripts/clawbox experiment host init \
  "$HOME/.config/clawbox/host.yaml" --detect
```

Review `host.yaml` before applying it. Set the actual template IDs and node,
select compute CPU lists and available memory budgets, and choose dedicated
WARM, COLD, and output paths. Detection reports physical facts; it does not
measure how much capacity other workloads can spare. On a host without a
separate WARM NUMA node, set `warm: false`. See the
[host-field reference](reference.md#host-plan) for units and defaults.

```bash
bash scripts/clawbox experiment host check "$HOME/.config/clawbox/host.yaml"
bash scripts/clawbox --sudo experiment host apply \
  "$HOME/.config/clawbox/host.yaml" \
  --profile "$HOME/.config/clawbox/host.json"
```

Apply on an idle VM pool. It checks services and storage, probes VM access, and
writes the applied profile only after success. Repeat it after reboot or host
configuration changes. To reconstruct an editable plan from an existing profile,
use `host init NEW.yaml --from-profile PROFILE.json`. `host init` never overwrites
an existing plan.

For tiered snapshots, verify the configured storage:

```bash
python scripts/validate-tiered-storage.py \
  --template "$CLAWBOX_TOOL_TEMPLATE" --node "$CUBE_NODE" \
  --warm "$CLAWBOX_WARM_ROOT" --cold "$CLAWBOX_COLD_ROOT" \
  --helper-image "$CLAWBOX_TOOL_IMAGE" \
  --expected-memory-mib 3072 \
  --output "$CLAWBOX_OUTPUT_ROOT/storage-check.json"
```

Finish with the [smoke experiment](guide.md#2-run-a-smoke-experiment), including
final task validation and owned-VM cleanup.

## Recover after reboot

Recover the Cubelet and egress dependency chain, then check services and
storage before applying the saved host plan. The optional argument is the
maximum number of seconds to wait for Cubelet's delegated cgroup and storage-node
heartbeat (default 1200). A large preallocated TAP pool increases startup time:

```bash
sudo bash scripts/recover-cubesandbox-after-reboot.sh
systemctl is-active cube-sandbox-cube-api.service \
  cube-sandbox-cubemaster.service cube-sandbox-cubelet.service \
  cube-sandbox-cube-egress.service cube-sandbox-cube-egress-net.service
df -h /data/cubelet
bash scripts/clawbox experiment host check "$HOME/.config/clawbox/host.yaml"
bash scripts/clawbox --sudo experiment host apply \
  "$HOME/.config/clawbox/host.yaml"
```

The apply step requires an idle Cube VM cgroup and reclaims clean cache retained
in its reusable leaves; it never deletes VM/template files or unrelated data.

For S3lvol, also require an active backend and `/var/run/s3lvol.sock`. The
snapshot filesystem must remain below 85% use. Preview recognized crash-dump
cleanup with `experiment host clean`; never delete Cubelet data or Docker volumes
by size. Resume interrupted experiments only after host readiness is restored.

## Update an existing host

Use this table to select the smallest required update. Keep immutable inputs and
results for previous runs. Any change to an experiment specification requires a
new run ID; `launch` reuses qualification when its capability inputs still match.

| Change | Required action |
| --- | --- |
| ClawBox Python, scheduling, reporting, or CLI | Update the checkout. An editable installation sees the changes in new processes; bootstrap again when dependencies change. No image rebuild. |
| Host-side ClawTune predictor or selected prediction artifact | Update the host source/artifact and experiment evidence path. No Tool-image rebuild. Update Runtime separately if its predictor or seed must also change. |
| Runtime predictor, seed, prepared cache, or plugin | Rebuild and register **one shared Runtime template**. Start fresh Runtime state and qualify the new template. Reuse all task-specific Tool templates. |
| Tool collector, Tool Bridge, guest measurement contract, or guest dependencies | Rebuild the affected Tool image layers and register new Tool templates. For a corpus, use the [collector refresh](workloads.md#refresh-tool-collectors) to preserve task dependencies. |
| VM vCPU, RAM, disk, image, or workspace | Register new templates with the intended shape/content, update the host plan or workload, and qualify. `configure` does not resize templates. |
| SDK-only patch | Reinstall the patched host SDK. No service restart or VM-image rebuild. |
| CubeSandbox server/placement/snapshot patch | Build matching Cubelet/Shim binaries, install on an idle host, recover services, and apply the host plan. Guest templates remain reusable when their content and kernel are unchanged. |
| Host CPU lists, budgets, mounts, network settings, or Cube quotas | Check/apply the reviewed host YAML, then reconfigure the experiment. No image rebuild. |
| Trained KB's environment, measurement semantics, or training data | Train new immutable artifacts against the matching environment. A cold-start seed is selected explicitly and does not require workload-specific training. |
| Only timing diagnostics, arrivals, concurrency, or scheduler parameters | Validate the new specification and use a new run ID. No image rebuild or retraining. |

### Refresh the Runtime predictor and KB

The Python dependency is pinned to unchanged ClawTune
`4a6d0af66e76b377bfc957baf84a5f414c6d8bf5`. ClawBox owns its host KB adapters,
Runtime plugin adaptation, and compact Tool collector. Keep the ClawTune source
unchanged. The canonical seed contains ToolKB, TrieKB, and LatticeKB; the removed
execution-context KB is not restored.

Export the pinned source without switching or editing the ClawTune checkout:

```bash
python scripts/prepare-clawtune.py --clawtune-root /absolute/path/ClawTune \
  --output "$HOME/clawtune-hostkb-pinned"
bash scripts/bootstrap-python.sh --clawtune "$HOME/clawtune-hostkb-pinned"
```

Use a new export directory; the command refuses to overwrite one. Save the export
path as `CLAWTUNE_ROOT` in `machine.env` and set `CLAWTUNE_SIDECAR_SRC` to
`$CLAWTUNE_ROOT/services/sidecar/src`. The explicit sidecar path takes precedence
over the root and must not still point to an older checkout. Select its `seeds/bootstrap-v2` in the
experiment configuration. Reusing the same seed bytes needs no retraining.

For the first host-live deployment, refresh one Runtime and one Tool template,
then validate a small experiment before refreshing the task corpus:

```bash
bash scripts/clawbox experiment images --role runtime \
  --profile "$HOME/.config/clawbox/host.json"
bash scripts/clawbox experiment images --role sandbox \
  --profile "$HOME/.config/clawbox/host.json" \
  --kernel-source /absolute/path/to/guest-kernel-source \
  --kernel-build /absolute/path/to/guest-kernel-build
```

Docker, Go, and Node.js/npm are required. Use headers matching the actual Cube
guest kernel; the command verifies the configuration. Build settings are saved
for repeat execution. The commands update the template entries in the profile.
Copy the new IDs into the editable host plan and update corresponding machine
settings before host apply/export. Preserve the previous profiles and templates
for existing runs. Generate and qualify a new experiment from the updated profile.

After the small experiment passes, use the
[Tool collector corpus refresh](workloads.md#refresh-tool-collectors), preserving
each task's workspace and dependencies. Further host-only KB sharing-policy
changes within the same wire protocol require no Runtime or Tool rebuild.

### Apply SDK timeout patches

The release builder applies the required source patches. For an existing SDK
checkout that needs only the timeout updates, source the saved settings and
apply these patches idempotently:

```bash
cd "$HOME/src/ClawBox"
source .venv/bin/activate
set -a
source "$HOME/.config/clawbox/machine.env"
set +a
for patch in sdk-control-timeouts.patch sdk-lifecycle-timeouts.patch \
  sdk-filesystem-timeouts.patch sdk-filesystem-watch-timeouts.patch; do
  patch_path="$PWD/deploy/cubesandbox/$patch"
  if ! git -C "$CUBE_SOURCE_DIR" apply --reverse --check "$patch_path" 2>/dev/null; then
    git -C "$CUBE_SOURCE_DIR" apply --check "$patch_path" || exit 1
    git -C "$CUBE_SOURCE_DIR" apply "$patch_path" || exit 1
  fi
done
python -m pip install -e "$CUBE_SOURCE_DIR/sdk/python"
```

New workers load the updated SDK. If a patch conflicts, use a newly prepared
source checkout rather than modifying a running experiment's environment.

## Maintain storage

Preview recognized crash dumps, then explicitly apply the cleanup:

```bash
bash scripts/clawbox --sudo experiment host clean
bash scripts/clawbox --sudo experiment host clean --apply
```

Defaults select recognized dumps at least 24 hours old and 100 MiB in size under
`/var/crash` and `/var/lib/systemd/coredump`. `--root` selects dedicated crash
directories; `--older-than-hours` and `--min-size-mib` adjust selection. Recent,
growing, or unrelated files are excluded. The command preserves VM/template
storage, results, Docker volumes, and unrelated datasets.

## Common remedies

| Failure | Action |
| --- | --- |
| Incomplete Python environment | Run `bootstrap-python.sh` with matching ClawTune and patched SDK sources; verify `pip check`. |
| Fresh bootstrap cannot use an inherited SOCKS proxy | Use `bootstrap-python.sh --direct-network` when direct package access is available. |
| Missing/mismatched template or workspace | Register the correct image and update the profile/workload; inspect with `doctor SPEC --probe-vm`. |
| Cube scheduler quota rejection (`130597`) | Compare offered VM shapes against Cube's reported quotas, then review host quota settings. Configured quota exhaustion does not establish physical-memory exhaustion. |
| Runtime readiness timeout | Inspect `openclaw/SESSION/sidecar-startup.log` and the configured readiness deadline. |
| Snapshot storage rejected | Check WARM capacity and COLD filesystem use; Cube rejects snapshot writes above 85% filesystem use. |
| Interrupted or ambiguous VM creation | Preserve the run, execute `experiment abort RUN_ID`, recover host readiness, then resume or use a new run ID for changed inputs. |
