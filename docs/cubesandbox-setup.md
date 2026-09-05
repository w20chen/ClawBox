# CubeSandbox setup and run guide

This is the operational guide for the supported ClawBox topology. ClawBox
uses CubeSandbox for both VMs and asks CubeSandbox for the Tool's semantic raw
TCP endpoint for container port `2222`. ClawBox does not allocate ports,
interpret Redis/CubeProxy metadata, proxy SSH, create NodePorts, or discover a
guest IP.

## Choose the deployment path

Use one of these two paths:

1. **Fresh machine:** install CubeSandbox's standalone one-click deployment
   on a Linux host with KVM. For a multi-node deployment, use the official
   control-node/compute-node layout. The Tool endpoint must resolve to a
   deployment-owned physical or private address that the Runtime can reach.
2. **Already set up:** keep the existing CubeSandbox deployment and run the
   preflight below. Do not modify ClawBox networking to compensate for a
   failed route.

The final native-SSH gate does not accept a Kubernetes Pod IP as `HostIP`.
The repository's `install-cubesandbox-kunpeng920.sh` is a reproducible
Kunpeng/Kubernetes profile and is useful for CubeSandbox lifecycle checks, but
the current single-node Pod-IP topology is not an admissible native-SSH
deployment. CubeSandbox may use Kubernetes internally; ClawBox must not use
Kubernetes objects as its execution path.

## Fresh machine

The official CubeSandbox deployment documentation is the source of truth for
OS packages, storage, services, firewall rules, and one-click release
artifacts:

- [bare-metal deployment](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/bare-metal-deploy.md)
- [multi-node deployment](https://github.com/TencentCloud/CubeSandbox/blob/master/docs/guide/multi-node-deploy.md)
- [one-click deployment files](https://github.com/TencentCloud/CubeSandbox/tree/master/deploy/one-click)

For ARM64, use an ARM64 release bundle or build one from source; the official
online installer is not the ARM64 path. The host must have Linux, `/dev/kvm`,
cgroup v2, Docker/containerd support, eBPF support, and the storage required
by the selected CubeSandbox deployment. Keep CubeProxy's host-network data
path and its documented physical/private node address intact.

The semantic endpoint API is a small CubeSandbox source addition. It is not in
the public `v0.7.0` tag, so prepare the pinned source before building the
CubeSandbox API/release bundle:

```bash
cd ClawBox
export CUBE_SOURCE_DIR="$PWD/.cubesandbox"
bash deploy/cubesandbox/prepare-semantic-source.sh
export CUBE_SOURCE_DIR="$PWD/.cubesandbox"
```

The helper refuses to overwrite a dirty checkout and applies two narrow
CubeSandbox patches: `deploy/cubesandbox/semantic-tcp-endpoint.patch` adds the
CubeAPI route and matching Python SDK method, while
`deploy/cubesandbox/hostport-hairpin.patch` lets one CubeSandbox VM consume
another VM's existing mapped TCP endpoint on the same node. The latter is the
source-identical patch from CubeSandbox commit `6b2d63e`; it reuses CubeVS's
existing port maps and conntrack state and adds no SSH proxy or port allocator.
Build the CubeSandbox one-click bundle from that prepared source using
CubeSandbox's documented release-bundle flow:

```bash
cd "$CUBE_SOURCE_DIR"
test -e deploy/one-click/build.env || \
  cp deploy/one-click/build.env.example deploy/one-click/build.env
ONE_CLICK_BUILD_JOBS=1 ./deploy/one-click/build-release-bundle-builder.sh
```

Copy the generated `deploy/one-click/dist/*.tar.gz` to the target machine and
install it with the official one-click procedure:

```bash
tar -xzf cube-sandbox-one-click-*.tar.gz
cd cube-sandbox-one-click-*
cp env.example .env
# Set CUBE_SANDBOX_NODE_IP to this machine's routable private/physical address.
sudo ./install.sh
sudo ./smoke.sh
```

The resulting CubeAPI must serve:

```text
GET /sandboxes/<sandbox_id>/ports/2222
  -> {"sandboxID":"...", "containerPort":2222, "address":"host:port"}
```

Install ClawBox and then install the SDK from the same prepared source so the
worker and the CubeAPI agree on this contract:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,postgres]'
.venv/bin/python -m pip install -e "$CUBE_SOURCE_DIR/sdk/python"
```

Do not use `Sandbox.get_host(2222)` as the SSH target. That value is an HTTP
ingress authority, not the native SSH endpoint.

The endpoint is resolved again after every Tool restore and before the next
SSH admission. If CubeSandbox places the restored Tool behind a new endpoint
host, ClawBox replaces the running Runtime's CubeSandbox egress policy through
the official SDK `update_network` call before returning `ADMIT`; a changed
mapped port needs no network-policy update. This is still a direct Runtime to
Tool TCP connection and does not cache, allocate, or proxy the route.

For a native OpenClaw arm using `snapshot_pause`, the model-wait lifecycle
applies the same CubeSandbox checkpoint/swap operation to both VMs. The Runtime
is restored before the pending ModelGateway response is released so its
long-lived Agent can continue; the Tool can stay swapped until the next
`/v1/tool/admit`, which restores it and resolves the current endpoint. A
`resident` arm keeps both VMs resident. Replay-engine compatibility runs keep
their historical Tool-only lifecycle and are not evidence for this native
paired-VM behavior.

## Existing machine

Set the API and CubeProxy transport variables for the already-installed
deployment. For standalone one-click, CubeAPI is normally on port `3000` and
CubeProxy's HTTP transport is normally on its host HTTP port (often `80`);
use the values actually configured by that deployment. A Kubernetes NodePort
is not a replacement for the native Tool endpoint.

```bash
export CUBE_API_URL='http://<cube-control-host>:3000'
export CUBE_PROXY_NODE_IP='<CubeProxy transport address>'
export CUBE_PROXY_PORT_HTTP='<CubeProxy HTTP port>'
export NO_PROXY='<cube-control-host>,<cube-proxy-host>,localhost,127.0.0.1'
export no_proxy="$NO_PROXY"

.venv/bin/python scripts/audit-cube-sandboxes.py --json
curl -fsS "$CUBE_API_URL/health"
```

Before a run, require fresh immutable Runtime and Tool templates. The Tool
template must expose both `49983` (Cube readiness) and `2222` (SSH), and the
Runtime/Tool image digests must match the experiment file. Register templates
with `scripts/register-cube-template.py`; never reuse a failed, stale, or
pre-kernel template as evidence.
Pass the target node's exact current replica kernel component with
`--expected-kernel-version`; registration intentionally has no default because
a stale default can silently bind a paper arm to the wrong guest kernel.

Run the endpoint and identity gate from a host that can reach CubeAPI,
CubeProxy, and the policy listener:

```bash
export CLAWBOX_CONTROL_HOST='<address reachable from Runtime VMs>'
export CLAWBOX_MODEL_GATEWAY_HOST="$CLAWBOX_CONTROL_HOST"

.venv/bin/python scripts/validate-cubesandbox-tcp-endpoints.py \
  --runtime-template '<fresh-runtime-template-id>' \
  --tool-template '<fresh-tool-template-id>' \
  --node '<cube-node-name>' \
  --control-host "$CLAWBOX_CONTROL_HOST" \
  --count 1 \
  --output results/endpoint-c1.json
```

This gate creates and destroys its own pair. It must prove all of the
following before the result is usable: semantic endpoint identity, strict
`ssh -G` settings, Runtime reaching the intended Tool marker, stale endpoint
rejection while paused, a new endpoint epoch after restore, cross-Tool
identity rejection before SSH, exact Tool telemetry, and zero owned
sandboxes. A successful TCP handshake by itself is not evidence.

Only after c1 passes, repeat the same gate with `--count 4` and `--count 8`.
Then run the pair smoke and the selected experiment. Promote to c20/c40/c60
only after the c4/c8 results show zero wrong-Tool executions, duplicate
executions, telemetry loss, and owned-sandbox leaks.

```bash
.venv/bin/python scripts/validate-cubesandbox-tcp-endpoints.py \
  --runtime-template '<fresh-runtime-template-id>' \
  --tool-template '<fresh-tool-template-id>' \
  --node '<cube-node-name>' --control-host "$CLAWBOX_CONTROL_HOST" \
  --count 4 --output results/endpoint-c4.json

.venv/bin/python scripts/validate-cubesandbox-tcp-endpoints.py \
  --runtime-template '<fresh-runtime-template-id>' \
  --tool-template '<fresh-tool-template-id>' \
  --node '<cube-node-name>' --control-host "$CLAWBOX_CONTROL_HOST" \
  --count 8 --output results/endpoint-c8.json

clawbox experiment validate examples/experiments/openclaw-cube.yaml
clawbox experiment plan examples/experiments/openclaw-cube.yaml
```

## Run replay or a real model

Replay does not need a provider credential. After the c1/c4/c8 endpoint gates
pass, run the checked-in native OpenClaw replay arm:

```bash
clawbox --output-root /data/clawbox-results experiment run \
  examples/experiments/openclaw-cube-replay-c40.yaml --run-id openclaw-replay-c40
```

For a real model, copy `examples/experiments/openclaw-cube.yaml` to a
machine-local file and replace its Runtime/Tool template IDs, image digests,
and target node with the records accepted by the endpoint gate. Set the
credential only in the Worker environment; the YAML contains the name of that
environment variable, not its value:

```bash
export OPENCLAW_API_KEY='<provider credential>'
clawbox --output-root /data/clawbox-results experiment run \
  /data/clawbox-openclaw-api.yaml --run-id openclaw-api-c1
```

The API `base_url` is reached by the Worker and must be an OpenAI-compatible
`/v1` endpoint reachable from that machine. Runtime receives only a
session-scoped ModelGateway token, and never receives the provider key. A
successful local stub-gateway test is not real-model evidence; retain the
Worker model-gateway records and the upstream request count with the c1
result.

## Network failure classification

When the semantic endpoint is returned but Runtime cannot connect, classify the
deployment before changing ClawBox:

- verify the endpoint from the Runtime VM, not only from the host;
- verify a cross-node/private-NIC route when the Tool is on another physical
  node; do not infer same-node SandboxIP-to-HostPort hairpin support;
- separately test CubeProxy's normal SandboxIP command path;
- inspect CubeSandbox's documented physical-NIC `from_world` datapath and
  counters, but do not treat a populated BPF map or attached program as a hit
  proof;
- if the endpoint is a Kubernetes Pod IP, stop and move to the standalone or
  deployment-owned physical/private topology.

The source-controlled topology probe performs these routes against one freshly
created Runtime/Tool pair while keeping strict host-key and Tool-marker checks:

```bash
python scripts/probe-cubesandbox-network-topology.py \
  --runtime-template tpl-67569219b64f4a80836a1f35 \
  --tool-template tpl-06b699a92c694c7ba3e6465b \
  --node hostname-txyuq.foreman.pxe \
  --cube-master-url http://10.103.189.111 \
  --physical-host 193.124.7.2 \
  --output /data/clawbox-topology.json
```

`DIAGNOSTIC_COMPLETE` means the bounded comparison and cleanup completed; it
does not mean a route passed. Inspect `route_identity_results` and
`reachable_identity_routes`. The 2026-09-05 current-template run returned
`false` for semantic HostPort, physical HostPort, and SandboxIP, with TCP
connection refusal on all three and `zero_leaks=true`. This is evidence that
same-node CubeVS forwarding must be corrected before the native c1 gate. It is
not permission to select a diagnostic route in the Worker.

Restore temporary diagnostic changes after every probe. Do not add a ClawBox
proxy, NodePort, Redis lookup, direct guest-IP fallback, or second allocator.

## Run an already validated machine

Once the endpoint gate is green, export the same variables in the Worker
environment and run the standalone experiment worker:

```bash
export CUBE_API_URL='http://<cube-control-host>:3000'
export CUBE_PROXY_NODE_IP='<CubeProxy transport address>'
export CUBE_PROXY_PORT_HTTP='<CubeProxy HTTP port>'
export CLAWBOX_CONTROL_HOST='<address reachable from Runtime VMs>'
export CLAWBOX_MODEL_GATEWAY_HOST="$CLAWBOX_CONTROL_HOST"

clawbox --output-root /data/clawbox-results experiment run \
  examples/experiments/openclaw-cube.yaml --run-id openclaw-run
clawbox --output-root /data/clawbox-results experiment status openclaw-run
clawbox --output-root /data/clawbox-results experiment collect openclaw-run
```

Keep the endpoint-gate JSON, Worker result bundle, exact template records,
and the final zero-leak audit together. If a machine reboots, rerun the health,
template, semantic endpoint, and c1 gates before resuming the experiment.
