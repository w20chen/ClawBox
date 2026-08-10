# Two-sandbox tenant cells

This mode adds one Runtime Pod and one Tool Pod per tenant without changing the
existing `deploy/kata-firecracker` Indexed Job mode.

## Boundary and supported tools

The Runtime Pod contains OpenClaw `2026.7.1-2`, the ClawTune plugin, and the
local ClawTune scheduler sidecar. OpenClaw uses its built-in SSH sandbox backend
to connect to the tenant's Tool Service. The Tool Pod has its own PID namespace,
root filesystem, and ephemeral workspace. Neither Pod mounts a Docker socket or
a Kubernetes service-account token.

The installed OpenClaw schema and implementation route `exec` and `process`
through SSH, and `read`, `write`, `edit`, `apply_patch`, and sandbox media reads
through its remote filesystem bridge.

The first version intentionally denies `browser`: the installed OpenClaw SSH
backend reports that sandbox browser containers are unsupported. `canvas`,
`nodes`, `cron`, and `gateway` are also denied because they are host/control
capabilities rather than Tool filesystem commands. Plugin/MCP tools are not
automatically isolated; each must be reviewed separately before being allowed.
Provider-backed `web_search`/`web_fetch`, messaging tools, session/subagent
control, memory indexing, and model/media inference remain Runtime/Gateway or
external-service operations; this mode does not claim that they execute in the
Tool Pod. Only access to a sandbox-relative media file uses the remote filesystem
bridge.

## Build

Keep `ClawTune` and `claw-k8s` as sibling directories. From the `claw-k8s`
directory, pass ClawTune as a BuildKit named context; do not use a symlink that
points outside the main build context:

```bash
test -f ../ClawTune/packages/openclaw-plugin/package.json
docker build -f docker/Dockerfile.runtime \
  --build-context clawtune=../ClawTune \
  -t registry.example/claw-runtime:two-sandbox .
docker build -f docker/Dockerfile.tool-sandbox \
  -t registry.example/claw-tool:two-sandbox .
docker push registry.example/claw-runtime:two-sandbox
docker push registry.example/claw-tool:two-sandbox
```

Replace the `registry.example` image names with a real registry before building
and pushing.

## Secrets

Create an LLM Secret once. The script references it and never prints its data:

```bash
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
printf '%s' 'https://llm.example/v1' >"$tmp_dir/openai-base-url"
printf '%s' "$OPENAI_API_KEY" >"$tmp_dir/openai-api-key"
printf '%s' 'model-name' >"$tmp_dir/openclaw-model"
kubectl -n agents create secret generic tenant-a-llm \
  --from-file=openai-base-url="$tmp_dir/openai-base-url" \
  --from-file=openai-api-key="$tmp_dir/openai-api-key" \
  --from-file=openclaw-model="$tmp_dir/openclaw-model"
```

Production should create a tenant-specific SSH Secret out of band:

```bash
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
ssh-keygen -q -t ed25519 -N '' -f "$tmp_dir/id_ed25519"
ssh-keygen -q -t ed25519 -N '' -f "$tmp_dir/ssh_host_ed25519_key"
kubectl -n agents create secret generic tenant-a-tool-ssh \
  --from-file=id_ed25519="$tmp_dir/id_ed25519" \
  --from-file=id_ed25519.pub="$tmp_dir/id_ed25519.pub" \
  --from-file=ssh_host_ed25519_key="$tmp_dir/ssh_host_ed25519_key" \
  --from-file=ssh_host_ed25519_key.pub="$tmp_dir/ssh_host_ed25519_key.pub"
```

If `--ssh-secret` is omitted, `cell.sh deploy` creates a demo key in a temporary
directory, uploads `claw-<tenant>-ssh`, and deletes the local copy immediately.
`cell.sh delete` removes only matching, labeled demo keys. It never deletes a
referenced production SSH Secret or the LLM Secret.

## Deploy and delete

Tenant IDs must be lowercase Kubernetes DNS labels of at most 40 characters;
invalid IDs are rejected and never evaluated as shell text.

Standard NetworkPolicy cannot allow an FQDN. Resolve the LLM endpoint to a stable
and narrow CIDR, or use a controlled egress proxy, and pass the CIDR explicitly.
The deployment entry rejects the IPv4 and IPv6 default routes:

```bash
bash deploy/two-sandbox/cell.sh deploy \
  --namespace agents \
  --tenant tenant-a \
  --runtime-image registry.example/claw-runtime:two-sandbox \
  --tool-image registry.example/claw-tool:two-sandbox \
  --llm-secret tenant-a-llm \
  --ssh-secret tenant-a-tool-ssh \
  --llm-egress-cidr 203.0.113.10/32 \
  --llm-egress-port 443
```

Omit RuntimeClass options for the cluster default. They are independent, so
runc, gVisor, Kata, or the existing `kata-fc` can be selected without redesign:

```bash
bash deploy/two-sandbox/cell.sh deploy ... \
  --runtime-class kata-fc \
  --tool-runtime-class kata-fc
```

Tool egress is denied by default. A reviewed workload can receive one explicit
CIDR/port through `--tool-egress-cidr` and `--tool-egress-port`.

```bash
bash deploy/two-sandbox/cell.sh delete --namespace agents --tenant tenant-a
```

## Validation

```bash
python deploy/two-sandbox/test_render.py  # requires PyYAML
bash deploy/two-sandbox/smoke-test.sh \
  --namespace agents --tenant tenant-a --other-tenant tenant-b
```

The smoke test asks OpenClaw itself to execute shell and file tools. It verifies
distinct Pods, hostnames, PID namespaces and filesystems; remote `exec` and
`read/write/edit/apply_patch`; no Docker socket; no long-term credential in Tool;
cross-tenant SSH denial; RuntimeClass reporting; and optional cleanup. Add
`--verify-delete` to remove the tested cell and verify managed-resource cleanup.

## ClawTune degradation and next telemetry interface

The sidecar remains loopback-local in Runtime and retains lifecycle hooks, trace
correlation, the LLM proxy, and advisory prediction. The plugin uses
`executionBackend: hook-only`; cgroup, affinity, NUMA, and stage-2 eBPF collection
are disabled. The Runtime sidecar has no trustworthy Tool PID/cgroup scope, so
existing behavior records absent/unattributed scope and unavailable stage-2
telemetry. No measurements are fabricated and no public schema change is needed.

A later optional Tool telemetry agent should accept a one-time execution token,
`execution_id`, tenant/runtime identity, and command digest; return a
JSON-Schema-defined Tool Pod UID plus cgroup-v2 path or root PID/PID namespace
inode and explicit availability reason; then return bounded CPU, RSS/peak memory,
I/O, exit status, and monotonic timestamps keyed by `execution_id`. Public fields
must be added to ClawTune JSON Schema first, followed by producer, consumer, and
compatibility tests. This is deliberately outside the MVP.
