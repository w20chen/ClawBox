# CubeSandbox setup

This guide covers CubeSandbox installation, networking, and the checks required
before a ClawBox experiment. Experiment configuration and model settings are in
the [experiment guide](experiment-operations.md).

ClawBox uses CubeSandbox for both the Runtime VM and Tool VM. It asks
CubeSandbox for the current TCP address of Tool port `2222`; it does not read
CubeProxy databases, allocate ports, create a NodePort, or connect directly to
an assumed guest address.

## Supported deployment

Use CubeSandbox's standalone deployment on a Linux KVM host, or its documented
multi-node control/compute layout. The returned Tool address must use a
physical or private deployment address that the Runtime VM can reach.

A Kubernetes Pod IP is not a valid final Tool address. The old Kunpeng
Kubernetes deployment remains documented in
`kunpeng920-reproduction-runbook.md` for historical diagnosis only.

CubeSandbox base version: `v0.7.0`. ClawBox applies source-controlled
patches for the semantic TCP endpoint, same-node port forwarding, template
image provenance, tiered snapshots, checkpoint phase timing, and physical
memory tier isolation.

## Install on a new machine

Use CubeSandbox's upstream instructions for packages, firewall settings,
storage, and service layout:

- [bare-metal deployment](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/bare-metal-deploy.md)
- [multi-node deployment](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/multi-node-deploy.md)
- [one-click deployment files](https://github.com/TencentCloud/CubeSandbox/tree/master/deploy/one-click)

The host needs `/dev/kvm`, cgroup v2, an ARM64 CubeSandbox build, and enough
reflink-capable XFS storage for VM layers and checkpoints.

Prepare a clean CubeSandbox `v0.7.0` checkout and apply the required patches:

```bash
cd ClawBox
export CUBE_SOURCE_DIR="$PWD/.cubesandbox"
bash deploy/cubesandbox/prepare-semantic-source.sh
```

The helper refuses to overwrite a dirty checkout. Build and install the
one-click bundle using CubeSandbox's normal release process:

```bash
cd "$CUBE_SOURCE_DIR"
test -e deploy/one-click/build.env || \
  cp deploy/one-click/build.env.example deploy/one-click/build.env
ONE_CLICK_BUILD_JOBS=1 ./deploy/one-click/build-release-bundle-builder.sh

tar -xzf deploy/one-click/dist/cube-sandbox-one-click-*.tar.gz
cd cube-sandbox-one-click-*
cp env.example .env
# Set CUBE_SANDBOX_NODE_IP to a routable physical or private address.
sudo ./install.sh
sudo ./smoke.sh
```

Install the Python SDK from the same prepared source used by the server:

```bash
cd <ClawBox-checkout>
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,postgres]'
.venv/bin/python -m pip install -e "$CUBE_SOURCE_DIR/sdk/python"
```

## Configure the NUMA memory tiers

After installing the patched bundle, configure LOCAL on NUMA0 and a WARM
tmpfs on NUMA1. Use a directory on SSD for COLD:

```bash
sudo bash scripts/setup-tiered-memory.sh /data/clawbox/warm /data/clawbox/cold "$USER"
sudo systemctl restart cube-sandbox-cubelet.service
```

Run setup while experiments are stopped. Repeat it after reboot before starting
experiments. It configures a 64 GiB LOCAL cgroup and 64 GiB WARM tmpfs without
swap, and enables independent anonymous LOCAL restore and separate WARM page
charging. The paths must match the experiment YAML. See
[memory tier simulation](tiered-memory-simulation.md) for the measurement
interpretation and the limits of this CXL/UB approximation.

## Required TCP endpoint behavior

CubeSandbox must provide:

```text
GET /sandboxes/<sandbox-id>/ports/2222
  -> {"sandboxID":"...", "containerPort":2222, "address":"host:port"}
```

ClawBox calls the matching SDK method `get_tcp_endpoint(2222)`. Do not replace
it with `get_host(2222)`: that method returns an HTTP service address, not the
raw SSH address.

After a Tool VM is restored, ClawBox asks CubeSandbox for the address again,
increments the endpoint generation, and verifies the Tool's SSH identity before
allowing the command. A restored VM may receive the same address; it is still
treated as a new endpoint generation.

## Check an installed machine

Load the machine-specific settings and verify the services:

```bash
set -a
. "$HOME/.config/clawbox/machine.env"
set +a

test -c /dev/kvm
test "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs
curl -fsS "$CUBE_API_URL/health"
.venv/bin/python scripts/audit-cube-sandboxes.py --json
```

Runtime and Tool templates must be newly built immutable templates whose image
digests and guest-kernel version match the experiment YAML. Tool must expose
ports `49983` for readiness and `2222` for SSH. Template registration is shown
in the [experiment guide](experiment-operations.md#2-install-clawbox-and-build-templates).

## Validate c1 and c4

Run the same connectivity, identity, checkpoint, telemetry, and cleanup check
at increasing sizes:

```bash
.venv/bin/python scripts/validate-cubesandbox-tcp-endpoints.py \
  --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" \
  --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
  --node "$CUBE_NODE" --control-host "$CLAWBOX_CONTROL_HOST" \
  --count 1 --output "$CLAWBOX_OUTPUT_ROOT/endpoint-c1.json"

.venv/bin/python scripts/validate-cubesandbox-tcp-endpoints.py \
  --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" \
  --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
  --node "$CUBE_NODE" --control-host "$CLAWBOX_CONTROL_HOST" \
  --count 4 --output "$CLAWBOX_OUTPUT_ROOT/endpoint-c4.json"

```

A passing check must confirm all of the following:

- Runtime reaches the correct Tool VM over native SSH;
- SSH host-key checking and the Tool identity marker match;
- an old endpoint is rejected while the Tool is paused;
- the endpoint generation advances after restore;
- one Tool's identity cannot be used with another Tool's endpoint;
- cgroup and eBPF records join to the exact execution ID;
- no owned VM remains after cleanup.

For the tiered oracle study, continue to c40 only after these checks and the
full replay, tier-transition, capacity, and placement gates pass. Run each of
the 13 policies once; c8/c60 and extra repetitions are outside the agreed scope.

## Diagnose network failures

If CubeSandbox returns an address but Runtime cannot connect, test the route
from the Runtime VM rather than only from the host. A populated BPF map or a
successful host TCP connection does not prove Runtime reachability.

Use the bounded topology probe:

```bash
python scripts/probe-cubesandbox-network-topology.py \
  --runtime-template <runtime-template-id> \
  --tool-template <tool-template-id> \
  --node <compute-node> \
  --cube-master-url <CubeMaster-URL> \
  --physical-host <routable-host-address> \
  --output /data/clawbox-topology.json
```

Inspect the identity result for each tested route. Do not work around a failed
CubeSandbox route by adding an SSH proxy, Redis lookup, NodePort, port
allocator, or guest-IP fallback in ClawBox. Restore any temporary host-network
diagnostic changes after the test.

After a host reboot, repeat service health, template provenance, c1, and
pause/restore checks before starting a large experiment.
