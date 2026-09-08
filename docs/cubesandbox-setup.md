# Install on a new machine

This guide is for a dedicated ARM64 Linux host. It installs standalone CubeSandbox, not Kubernetes. Do not run the installer on a machine that is currently running experiments.

The most repeatable route is to transfer the working guest images and guest kernel from kunpeng, then build and install the patched CubeSandbox server. The images contain the benchmark repository, Python environment, OpenClaw, ClawTune, and telemetry tools. Recreating only the VM size is not enough to reproduce that environment.

## 1. Check the host

You need:

- ARM64 Linux, Python 3.12 or newer, Git, Docker, tar, and standard build tools.
- Working KVM: `test -c /dev/kvm`.
- cgroup v2: `stat -fc %T /sys/fs/cgroup` should print `cgroup2fs`.
- At least two NUMA nodes for this experiment. Check `lscpu` and `numactl --hardware`.
- Enough free memory on NUMA0 for 160 GiB LOCAL and on NUMA1 for 64 GiB WARM, plus host services.
- SSD storage with room for images, writable VM layers, snapshots, and logs. Keep well below CubeMaster's disk scheduling threshold.
- A host address reachable from the guest VMs. Do not use localhost as the guest-facing control address.
- Permission to run Docker and sudo for installation and memory setup.

CubeSandbox's installer prepares its service dependencies. Review its storage checks before choosing a data directory. Do not reformat an existing disk as a troubleshooting shortcut.

## 2. Transfer the working guest environment

On kunpeng, use a current ClawBox checkout and add the registered Runtime and Tool image references to `machine.env` as `CLAWBOX_RUNTIME_IMAGE` and `CLAWBOX_TOOL_IMAGE`. The references are in the active experiment YAML under `runtime.source_image_reference` and `sandbox.source_image_reference`.

```bash
bash scripts/export-machine-assets.sh /data/clawbox-transfer
```

Expected files:

```text
guest-images.tar
kernel/vmlinux-bm
kernel/version
kernel/version.json
```

This is a read-only copy of the working images and kernel, apart from two local Docker export tags. It does not copy live VMs, databases, or credentials. It can require substantial disk space.

Copy the directory to the new host. Also copy the approved rec-a trace and its original recording/provenance to your research-data directory. On kunpeng the approved trace is:

```text
/home/weitianc/clawbox-tiered-study-20260907/workload/healthy-reference-v1/rec-a-healthy-reference.jsonl
```

Do not assume that benchmark data or images are downloadable from GitHub. The local registry addresses in the Dockerfiles refer to assets on the original machine.

## 3. Install ClawBox and prepare CubeSandbox

Use sibling checkouts:

```bash
mkdir -p ~/src
cd ~/src
git clone https://github.com/w20chen/ClawBox.git
git clone https://github.com/w20chen/ClawTune.git
cd ClawBox
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
export CUBE_SOURCE_DIR="$HOME/src/CubeSandbox"
bash deploy/cubesandbox/prepare-semantic-source.sh
.venv/bin/python -m pip install -e "$CUBE_SOURCE_DIR/sdk/python"
```

The preparation command applies the maintained patches to CubeSandbox v0.7.0. It refuses unrelated local changes. Use the matching SDK from this source, not an unpatched SDK with the same version number.

Build the standalone release with the transferred guest kernel:

```bash
cd "$CUBE_SOURCE_DIR"
cp deploy/one-click/build.env.example deploy/one-click/build.env
ONE_CLICK_BUILD_JOBS=1 \
ONE_CLICK_CUBE_KERNEL_VMLINUX=/data/clawbox-transfer/kernel/vmlinux-bm \
bash deploy/one-click/build-release-bundle-builder.sh
```

The output is a release archive under `deploy/one-click/dist`. The build may take considerable time; it is installation work, not required before every experiment. Using the supplied kernel preserves the guest's eBPF/kprobe support.

Extract the newly produced archive into a new directory. Use its exact filename rather than extracting every old archive in `dist`. Enter the extracted directory:

```bash
cp env.example .env
# Edit .env and set CUBE_SANDBOX_NODE_IP to the new host's reachable address.
sudo bash install.sh
sudo bash smoke.sh
```

Expected result: the smoke check succeeds and `systemctl list-units 'cube-sandbox-*'` shows the installed services. Keep the release archive for later installation; do not copy kunpeng's service configuration wholesale to another IP address.

## 4. Load images into a registry the new host can use

```bash
docker load -i /data/clawbox-transfer/guest-images.tar
export REGISTRY='YOUR_REGISTRY/clawbox'
docker tag clawbox-transfer/runtime:research "$REGISTRY/runtime:research"
docker tag clawbox-transfer/tool:research "$REGISTRY/tool:research"
docker push "$REGISTRY/runtime:research"
docker push "$REGISTRY/tool:research"
docker image inspect --format '{{json .RepoDigests}}' "$REGISTRY/runtime:research"
docker image inspect --format '{{json .RepoDigests}}' "$REGISTRY/tool:research"
```

Replace `YOUR_REGISTRY` with a real reachable registry and log in if it requires authentication. A registry listening only on the old machine's localhost is not transferable. Save the pushed image references printed by Docker; use them below.

## 5. Configure this machine and register templates

```bash
cd ~/src/ClawBox
mkdir -p ~/.config/clawbox
cp examples/clawbox-machine.env.example ~/.config/clawbox/machine.env
```

Edit every placeholder. For an all-in-one host, CubeAPI can use `http://127.0.0.1:3000`; the guest-facing control and model-gateway addresses must use the reachable host IP. The current kunpeng proxy uses port 80; verify the new install's proxy listener rather than assuming it.

Set ClawTune paths, WARM/COLD paths, and an output directory outside the repository. Set the pushed image references. Leave the quoted template-ID placeholders until registration finishes. For this guide's example directories, make them writable by the experiment user:

```bash
sudo install -d -o "$USER" -g "$(id -gn)" \
  /data/clawbox-results /data/clawbox-specs /data/clawbox-traces
```

Load the settings:

```bash
set -a
source ~/.config/clawbox/machine.env
set +a
curl -fsS "$CUBE_API_URL/health"
.venv/bin/python scripts/audit-cube-sandboxes.py --json
```

Read the installed kernel component ID from `/usr/local/services/cubetoolbox/cube-kernel-scf/version.json`, at `variants.bm.version`. Then:

```bash
export GUEST_KERNEL_COMPONENT='REPLACE_WITH_INSTALLED_COMPONENT_ID'
.venv/bin/python scripts/register-cube-template.py "$CLAWBOX_RUNTIME_IMAGE" \
  --alias runtime-research-01 --node "$CUBE_NODE" \
  --cpu-millicores 2000 --memory-mib 2048 --writable-layer-size 20G \
  --exposed-port 49983 --probe-port 49983 \
  --command /usr/local/bin/cube-runtime-entrypoint.sh \
  --expected-kernel-version "$GUEST_KERNEL_COMPONENT"

.venv/bin/python scripts/register-cube-template.py "$CLAWBOX_TOOL_IMAGE" \
  --alias tool-research-01 --node "$CUBE_NODE" \
  --cpu-millicores 2000 --memory-mib 4096 --writable-layer-size 40G \
  --exposed-port 49983 --exposed-port 2222 --probe-port 49983 \
  --command /usr/local/bin/cube-tool-entrypoint.sh \
  --expected-kernel-version "$GUEST_KERNEL_COMPONENT"
```

Save the returned IDs as `CLAWBOX_RUNTIME_TEMPLATE` and `CLAWBOX_TOOL_TEMPLATE`. Reload the environment file. A successful registration must reach READY and match the expected guest kernel. Do not reuse an old template after changing its image or kernel.

## 6. Configure memory and validate

Create the local YAML using `study init` as shown in the [experiment guide](experiment-operations.md), then:

```bash
bash scripts/clawbox study setup-memory --spec /data/clawbox-specs/rec-a.yaml
sudo systemctl restart cube-sandbox-cubelet.service
sudo systemctl restart cube-sandbox-cube-egress.service
bash scripts/clawbox study setup-memory --spec /data/clawbox-specs/rec-a.yaml
bash scripts/clawbox study check --spec /data/clawbox-specs/rec-a.yaml
```

The first setup installs the memory-isolation service settings. Restart only on this idle, newly installed host. Reapply temporary cgroup settings after restart. For normal later runs, do not restart services.

Check native SSH, identity, pause/restore, and telemetry first at c1, then c4:

```bash
.venv/bin/python scripts/validate-cubesandbox-tcp-endpoints.py \
  --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
  --node "$CUBE_NODE" --control-host "$CLAWBOX_CONTROL_HOST" \
  --count 1 --output "$CLAWBOX_OUTPUT_ROOT/endpoint-c1.json"
```

Repeat with `--count 4` and a new output filename. On the idle host, validate snapshot placement. The helper uses privileged Docker access to inspect the VM's host memory mappings:

```bash
.venv/bin/python scripts/validate-tiered-storage.py \
  --template "$CLAWBOX_TOOL_TEMPLATE" --node "$CUBE_NODE" \
  --warm "$CLAWBOX_WARM_ROOT" --cold "$CLAWBOX_COLD_ROOT" \
  --helper-image "$CLAWBOX_TOOL_IMAGE" --require-local-numa 0 \
  --expected-memory-mib 4096 --output "$CLAWBOX_OUTPUT_ROOT/storage-check.json"
```

After a replay run, inspect the standalone report:

```bash
bash scripts/clawbox study report /path/to/study-directory
```

Check that Tool eBPF data is present, execution IDs join exactly, and loss is zero. A failed check is not a valid performance result.

Only after those checks should you start the c40 sweep. This repository's deployment components and patch preparation have been tested on kunpeng; this rewritten guide has not been certified by reinstalling a second empty physical host during the ongoing study. Report any failed check rather than treating an incomplete installation as ready.

## Common failures

- **No more resource:** inspect disk space, node readiness, and template replicas; free RAM alone is insufficient.
- **LOCAL limit or WARM mount mismatch:** run memory setup on an idle pool, particularly after reboot.
- **VM cannot reach Tool:** inspect the semantic TCP endpoint and guest-to-host routing. Do not substitute a guessed guest IP.
- **Missing eBPF data:** check the guest kernel, collector readiness, execution IDs, and image versions.
- **Replay mismatch:** check the repository, Python environment, workspace path, and actual tool output. Do not weaken matching to hide an environment error.
