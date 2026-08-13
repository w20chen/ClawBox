# ClawBox

> Phase 1-3 multi-tenant control plane is implemented in `clawbox/`. See
> [implementation mapping](docs/IMPLEMENTATION_MAPPING.md) and the
> [Phase 3 operations/security guide](docs/PHASE3.md). The original Kubernetes/Kata tenant-cell
> delivery below is retained as the Phase 4+ deployment foundation.

Quick local verification:

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python scripts/e2e.py
```

ClawBox delivers [OpenClaw](https://github.com/openai/openclaw) and [ClawTune](https://github.com/w20chen/ClawTune) to Kubernetes. Each tenant is a pair of Pods — a **Runtime** Pod and a **Tool** Pod — both running in isolated Kata Containers + Firecracker microVMs.

This repo ships no models and deploys no LLM. OpenClaw talks to an external OpenAI-compatible LLM endpoint over HTTP.

## Architecture

```text
                         External LLM API
                                ^
                                | HTTPS (fixed egress)
                                |
  +-----------------------------+-----------------------------+
  | Runtime Pod = One Firecracker microVM                     |
  |                                                           |
  |  OpenClaw Gateway/runtime                                 |
  |  ClawTune plugin                                          |
  |  ClawTune scheduler sidecar (127.0.0.1)                   |
  +----------------------------+------------------------------+
                               |
                               | SSH, tenant-specific Service
                               v
  +-----------------------------------------------------------+
  | Tool Pod = One Firecracker microVM                        |
  |                                                           |
  |  Non-root SSH executor                                    |
  |  Independent workspace, PID namespace and root filesystem |
  +-----------------------------------------------------------+
```

OpenClaw tools run in the Tool Pod via the built-in SSH sandbox backend:

- `exec`, `process`
- `read`, `write`, `edit`, `apply_patch`
- sandbox-relative media reads

Not executed in the Tool Pod:

- `browser` (unsupported by the current OpenClaw SSH backend)
- `web_search`, `web_fetch` (Runtime / external provider)
- model inference, messaging, session/subagent control, memory indexing
- plugin/MCP tools (each needs separate review)

Neither pod mounts the Docker socket, uses privileged mode, or auto-mounts the ServiceAccount token.

## Prerequisites

- Linux host with `/dev/kvm`
- Kubernetes cluster (containerd) with Kata Containers + Firecracker and the `kata-fc` runtime handler on the nodes (see `deploy/runtimeclass.yaml`)
- `kubectl` pointing at the target cluster
- Docker (or another OCI build tool)
- `envsubst` (Debian/Ubuntu: `gettext-base`; RHEL/openEuler: `gettext`)
- An image registry reachable from the cluster
- An OpenAI-compatible LLM endpoint
- A `ClawTune` checkout

The cluster CNI must implement Kubernetes `NetworkPolicy`, or tenant network isolation won't actually take effect. Keep both repos as sibling directories; all commands below run from the ClawBox root:

```text
/work/
  ClawTune/
  ClawBox/
```

## Deploy

### 1. Check the host

```bash
bash deploy/check-host.sh
```

Read-only self-check of KVM, Kata/Firecracker, the `kata-fc` handler and RuntimeClass. Fix `FAIL`; `WARN` can usually be ignored.

### 2. Build and push images

Don't create a `clawtune -> ../ClawTune` symlink — Docker doesn't reliably follow symlinks pointing outside the build context. Keep ClawBox as the main build context and pass the ClawTune sources via a BuildKit named context:

```bash
export RUNTIME_IMAGE=registry.example.com/claw/runtime:latest
export TOOL_IMAGE=registry.example.com/claw/tool:latest

docker build -f docker/Dockerfile.runtime \
  --build-context clawtune=../ClawTune \
  -t "$RUNTIME_IMAGE" .
docker build -f docker/Dockerfile.tool-sandbox -t "$TOOL_IMAGE" .
docker push "$RUNTIME_IMAGE"
docker push "$TOOL_IMAGE"
```

Replace `registry.example.com` with a real registry. The runtime image ships OpenClaw, the ClawTune plugin and the scheduler sidecar; the tool image ships only the non-root SSH executor and the base shell/file commands — no LLM key, OpenClaw token or tenant credentials.

### 3. Create the namespace and LLM Secret

Tenant IDs use lowercase letters, digits and `-`, up to 40 characters:

```bash
export NAMESPACE=agents
export TENANT_ID=tenant-a
export LLM_SECRET=tenant-a-llm

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
```

The LLM Secret requires three keys: `openai-base-url`, `openai-api-key`, `openclaw-model`. Don't put the API key in a manifest or commit it:

```bash
export OPENAI_BASE_URL=https://llm.example.com/v1
export OPENAI_API_KEY='replace-me'
export OPENCLAW_MODEL='replace-me'

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
printf '%s' "$OPENAI_BASE_URL" >"$tmp_dir/openai-base-url"
printf '%s' "$OPENAI_API_KEY" >"$tmp_dir/openai-api-key"
printf '%s' "$OPENCLAW_MODEL" >"$tmp_dir/openclaw-model"
kubectl -n "$NAMESPACE" create secret generic "$LLM_SECRET" \
  --from-file=openai-base-url="$tmp_dir/openai-base-url" \
  --from-file=openai-api-key="$tmp_dir/openai-api-key" \
  --from-file=openclaw-model="$tmp_dir/openclaw-model" \
  --dry-run=client -o yaml | kubectl -n "$NAMESPACE" apply -f -
```

### 4. Set the LLM egress CIDR

NetworkPolicy can only allow by IP/CIDR, not by domain. Give the LLM a fixed egress IP, or route through a fixed-address egress proxy:

```bash
export LLM_EGRESS_CIDR=203.0.113.10/32
export LLM_EGRESS_PORT=443
```

The deploy script rejects `0.0.0.0/0` and `::/0`.

### 5. Render and inspect the manifest

`render` doesn't touch the cluster:

```bash
bash deploy/cell.sh render \
  --namespace "$NAMESPACE" \
  --tenant "$TENANT_ID" \
  --runtime-image "$RUNTIME_IMAGE" \
  --tool-image "$TOOL_IMAGE" \
  --llm-secret "$LLM_SECRET" \
  --llm-egress-cidr "$LLM_EGRESS_CIDR" \
  --llm-egress-port "$LLM_EGRESS_PORT" \
  >"${TENANT_ID}.rendered.yaml"

kubectl apply --dry-run=client -f "${TENANT_ID}.rendered.yaml"
```

The rendered file references the Secret by name but never contains its contents.

### 6. Deploy the tenant

`deploy` checks for the RuntimeClass and applies `deploy/runtimeclass.yaml` if missing:

```bash
bash deploy/cell.sh deploy \
  --namespace "$NAMESPACE" \
  --tenant "$TENANT_ID" \
  --runtime-image "$RUNTIME_IMAGE" \
  --tool-image "$TOOL_IMAGE" \
  --llm-secret "$LLM_SECRET" \
  --llm-egress-cidr "$LLM_EGRESS_CIDR" \
  --llm-egress-port "$LLM_EGRESS_PORT"
```

Without `--ssh-secret`, the script generates a tenant-specific demo SSH keypair, creates the `claw-<tenant>-ssh` Secret, then deletes the local files. Re-running the command updates the Deployment and reuses the demo Secret. For production, create the SSH Secret out-of-band and pass `--ssh-secret` (format: [deploy/README.md](deploy/README.md#secrets)).

### 7. Check status and logs

```bash
kubectl -n "$NAMESPACE" get deployment,pod,service,networkpolicy \
  -l "claw.openai.com/tenant-id=$TENANT_ID" -o wide

kubectl -n "$NAMESPACE" rollout status "deployment/claw-${TENANT_ID}-tool" --timeout=180s
kubectl -n "$NAMESPACE" rollout status "deployment/claw-${TENANT_ID}-runtime" --timeout=300s
kubectl -n "$NAMESPACE" logs "deployment/claw-${TENANT_ID}-tool" --tail=100
kubectl -n "$NAMESPACE" logs "deployment/claw-${TENANT_ID}-runtime" --tail=100
```

Expect two pods (`claw-tenant-a-runtime-...` and `claw-tenant-a-tool-...`), both with `runtimeClassName: kata-fc`, and a matching Firecracker process on the host.

### 8. Run the smoke test

The full cross-tenant test needs two cells. Reuse one LLM Secret; each tenant keeps its own SSH Secret, Deployment, Service and workspace:

```bash
bash deploy/cell.sh deploy --namespace "$NAMESPACE" --tenant tenant-b \
  --runtime-image "$RUNTIME_IMAGE" --tool-image "$TOOL_IMAGE" \
  --llm-secret "$LLM_SECRET" \
  --llm-egress-cidr "$LLM_EGRESS_CIDR" --llm-egress-port "$LLM_EGRESS_PORT"

bash deploy/smoke-test.sh --namespace "$NAMESPACE" --tenant tenant-a --other-tenant tenant-b
```

The test checks that both pods run `kata-fc` with isolated hostname/PID namespace/filesystem, shell output and files land in the Tool workspace, the file tools go through the remote backend, neither pod has a Docker socket, the Tool pod holds no credentials, and `tenant-a` can't reach `tenant-b`'s Tool Service. It calls the real LLM, which must be reachable and support reliable tool calls.

## Delete a tenant

```bash
bash deploy/cell.sh delete --namespace "$NAMESPACE" --tenant tenant-a
```

Removes the tenant's Deployment, Service, NetworkPolicy and the script-generated demo SSH Secret. It never deletes the LLM Secret, production SSH Secrets passed via `--ssh-secret`, other tenants' resources, or the `kata-fc` RuntimeClass. Workspaces are `emptyDir`, so files are lost once the Pod/Deployment is removed.

## Tool Pod internet access

Tool egress is fully closed by default. For a reviewed workload, allow a specific CIDR and port:

```bash
bash deploy/cell.sh deploy ... \
  --tool-egress-cidr 198.51.100.20/32 \
  --tool-egress-port 443
```

This isn't a domain allowlist — use a stable egress proxy if the target IP drifts.

## Local validation

Without a cluster:

```bash
python deploy/test_render.py   # requires PyYAML
bash -n deploy/cell.sh deploy/smoke-test.sh deploy/check-host.sh \
  scripts/runtime-entrypoint.sh scripts/tool-entrypoint.sh scripts/tool-command.sh
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Pod stuck `Pending` | Node resources, image pull permissions, `kata-fc` RuntimeClass/containerd handler: `kubectl -n "$NAMESPACE" describe pod <pod>`, `kubectl get runtimeclass`, `bash deploy/check-host.sh` |
| Runtime Pod not Ready | Runtime logs + in-container sidecar logs. Common causes: missing LLM Secret keys, wrong egress CIDR/port, wrong model ID, malformed SSH Secret |
| Tool Pod not Ready | Tool logs + `describe pod -l 'app.kubernetes.io/component=tool,claw.openai.com/tenant-id=tenant-a'`. Verify the SSH Secret holds both client and host Ed25519 keypairs |
| LLM domain has multiple IPs | Don't widen egress to the whole internet. Use a fixed-egress-IP gateway or proxy and allow only its CIDR/port |
| No internet in the Tool | Expected. Only use `--tool-egress-cidr` when needed; prefer `/32` and a single port |

## Directory structure

```text
deploy/
  cell.yaml                     Runtime + Tool Deployments (both kata-fc)
  cell.sh                       render/deploy/delete entrypoint
  runtimeclass.yaml             kata-fc RuntimeClass (auto-applied on deploy)
  check-host.sh                 Kata/Firecracker host self-check
  smoke-test.sh                 real-cluster isolation test
  test_render.py                local YAML/security assertions
  openclaw-sandbox.example.json reference OpenClaw sandbox config
  README.md                     deployment details, Secrets, boundaries
docker/
  Dockerfile.runtime            runtime image
  Dockerfile.tool-sandbox       tool image
scripts/
  runtime-entrypoint.sh         runtime container entrypoint
  tool-entrypoint.sh            tool container entrypoint (sshd)
  tool-command.sh               tool SSH ForceCommand
```

Details on boundaries, production SSH Secrets and ClawTune: [deploy/README.md](deploy/README.md).
