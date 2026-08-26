# ClawBox

ClawBox runs each coding-agent task in two isolated ARM64 Firecracker
microVMs: a Runtime VM for the agent and a Tool VM for repository access and
command execution. Kubernetes admits both VMs as one `SandboxTask` Cell.

## Daily use on an already-provisioned Kunpeng host

You do not need to repeat the installation sections below. From the ClawBox
checkout, use one command to start/reconcile the existing deployment:

```bash
scripts/clawbox up
```

Then submit a task directly. The CLI reads the existing in-cluster token,
opens a temporary API port-forward, and closes it when the command exits:

```bash
scripts/clawbox submit \
  --input-ref my-first-task \
  --problem 'Describe the change the agent should make.' \
  --watch
```

`--input-ref` is the task/instance identifier; it does not select an image.
The configured template supplies the Tool image. Use `--idempotency-key` when
retrying the same submission after a client/network failure; otherwise the CLI
generates a new key and intentionally creates a new run.

`--project`, `--template`, tenant, API URL, token, and idempotency key all have
safe defaults for local host use. To print one short readiness report without
changing anything:

```bash
scripts/clawbox doctor
```

`up` never runs the destructive host bootstrap. A new host still requires the
one-time installation below because storage devices, registry, database, LLM
credentials, task images, and provider egress policy cannot be inferred.

For an older ClawBox deployment, `up` performs the non-destructive upgrade
work automatically: it reuses existing Secrets and the latest SandboxTask
policy, resolves mutable local image tags to registry digests, renders the
deployment manifests, runs migrations, and reconciles all five services.

For a new host, complete sections 1 through 4, then use the recommended
automated path at the start of section 5. It generates random tokens, creates
Secrets, runs migrations, applies the deployment, waits for readiness, and
leaves task submission ready. Do not also run the manual alternatives in
sections 5 and 6.

## Canonical production architecture

ClawBox has one supported production deployment architecture:

```text
native ARM64 Linux
  -> Kubernetes + containerd
  -> Kata Containers + Firecracker
  -> Runtime VM + Tool VM
  -> Tool-VM cgroup v2 + eBPF telemetry
  -> signed tenant/repository-scoped knowledge base
```

`docker-compose.yml` and `scripts/linux-deploy.sh` are developer checks only.
They do not run Firecracker, the production Cell, or native Tool-VM eBPF, and
are not an alternative production deployment.

## Supported scope

- One native ARM64 Kubernetes node.
- Kata Containers 3.31.0 with Firecracker 1.12.1.
- One Runtime VM and one Tool VM per task.
- Immutable ARM64 task and platform images (`image@sha256:...`).
- Tenant-scoped Managed API submission and idempotency.
- Direct `SandboxTask` submission for debugging.
- Per-task credentials and NetworkPolicies.
- Per-execution Tool-VM cgroup-v2 measurements.
- Native ClawTune eBPF clause telemetry inside the Tool guest kernel.
- Signed, immutable telemetry ingestion and atomic KB generations.
- Fixed resource profiles. Predictions are evaluated in shadow mode and do
  not resize tasks.

There is no supported x86, QEMU, runc, alternate-VMM, or multi-node fallback.

## Architecture

```text
Managed API -> Dispatcher -> SandboxTask -> Cell Controller
                                      |-> Tool Firecracker VM
                                      |    -> task image + Tool Bridge
                                      `-> Runtime Firecracker VM
                                           -> OpenClaw + ClawTune

Tool VM telemetry -> signed native artifacts -> Tuning API -> atomic KB
                                                            `-> next run
```

The eBPF collector runs inside the Tool VM. Moving it to the Kubernetes host
or Runtime VM loses command-level process attribution.

## Before you start

You must provide the following values. The repository cannot invent them:

| Input | Required value |
| --- | --- |
| Host | Dedicated ARM64 Linux host with hardware virtualization |
| Fresh-host storage | Two unused whole block devices; both are erased |
| Registry | Registry reachable from Docker and containerd |
| ClawTune | Checkout beside ClawBox at `../ClawTune` |
| Database | Optional PostgreSQL `postgresql+psycopg://...`; persistent SQLite is the single-node default |
| LLM | API key, upstream base URL, provider model, OpenClaw model ref |
| Task image | Native ARM64 `image@sha256:...` containing the Tool Bridge/telemetry overlay |
| Network policy | Exact LLM-provider egress CIDR; do not use a placeholder |

The validated host software is Kubernetes 1.35, containerd 2.3.4, Kata
3.31.0, Firecracker 1.12.1, Docker with Buildx, cgroup v2, and Python 3.12+.
The destructive host bootstrap installs the runtime/Kubernetes stack, but it
does not install Docker or Buildx. Install those from the host's trusted
package source before section 4, then verify `docker info` and
`docker buildx version` both succeed.

## 1. Check out an exact release tree

ClawBox and ClawTune must be siblings:

```text
/home/USER/
|-- ClawBox/
`-- ClawTune/
```

For a new checkout:

```bash
cd /home/USER
git clone https://github.com/w20chen/ClawTune.git
git clone https://github.com/w20chen/ClawBox.git
git -C ClawTune checkout --detach e91e60bc1e5f3209fbcf6091013fde96f217e2a7
cd ClawBox
git status --short --branch
git rev-parse HEAD
```

Do not build from a dirty working tree. Record both repository revisions with
the deployment evidence.

### Existing host or divergent checkout

Do not run `git reset --hard`, do not delete local work, and do not rerun the
destructive host bootstrap. Build from a clean detached worktree instead:

```bash
cd /home/USER/ClawBox
git fetch origin main
git status --short --branch
release="../ClawBox-release-$(git rev-parse --short=12 origin/main)"
git worktree add --detach "$release" origin/main
cd "$release"
git status --porcelain       # must print nothing
git rev-parse HEAD

# Keep the active ClawTune checkout and its generated data untouched too.
clawtune_release="$HOME/ClawTune-release-e91e60bc"
git -C "$HOME/ClawTune" fetch origin
git -C "$HOME/ClawTune" worktree add --detach \
  "$clawtune_release" e91e60bc1e5f3209fbcf6091013fde96f217e2a7
git -C "$clawtune_release" status --porcelain  # must print nothing
export CLAWTUNE_ROOT="$clawtune_release"
```

This leaves both active checkouts, generated data, and local commits untouched.
Keep `CLAWTUNE_ROOT` set to the clean pinned worktree for sections 4 and 5.

## 2. Install Python tools and run tests

```bash
python3 -c 'import sys; assert sys.version_info >= (3, 12), sys.version'
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[dev,postgres]'
python3 -m pytest -q
```

Do not continue if tests fail.
After this install, `scripts/clawbox` automatically uses `.venv/bin/python`,
so daily commands do not require reactivating the virtual environment.

## 3. Prepare or verify the host

Choose exactly one path.

### A. Fresh dedicated host

The following `plan` is read-only. Replace both device placeholders with
canonical whole-device paths and inspect every line:

```bash
bash scripts/bootstrap-openeuler-arm64.sh plan \
  --devmapper-data-device /dev/DATA_DISK \
  --devmapper-meta-device /dev/META_DISK
```

`apply` erases both devices. Run it only on a fresh dedicated host, and only
after verifying that neither device contains the operating system or user
data:

```bash
sudo bash scripts/bootstrap-openeuler-arm64.sh apply \
  --devmapper-data-device /dev/DATA_DISK \
  --devmapper-meta-device /dev/META_DISK \
  --confirm-erase /dev/DATA_DISK,/dev/META_DISK
```

Install the Tool-VM eBPF RuntimeClass after the base host succeeds:

```bash
bash scripts/install-ebpf-kata-runtime.sh apply
```

The bootstrap installs and verifies the shim `nofile` wrapper before its final
host gate. Re-running `sudo bash scripts/install-shim-nofile-wrapper.sh` is an
idempotent repair/verification step, not a separate prerequisite.

### B. Already provisioned host

Never run bootstrap `apply` again. Refresh sudo first because KVM, containerd,
and LVM checks require root access:

```bash
sudo -v
sudo bash scripts/bootstrap-openeuler-arm64.sh status
bash deploy/check-host.sh --runtime-class kata-fc-arm64
bash scripts/arm64-kata-smoke.sh --runtime-class kata-fc-arm64
```

The host is ready only when:

- the FC-0 artifact audit has zero failures;
- the shim soft `nofile` limit is at least 8192;
- the devmapper plugin is `ok` and below its pressure threshold;
- the Kata smoke reports `PASS`;
- both `kata-fc-arm64` and `kata-fc-arm64-ebpf` exist.

Label the node only after those gates pass:

```bash
kubectl label node "$(hostname)" \
  clawbox.openai.com/firecracker-ready=true --overwrite
```

If a non-root status command says it cannot inspect LVM or containerd, rerun
it with `sudo`; permission failure does not mean the thin pool is missing.

## 4. Build and push immutable platform images

Build on the native ARM64 host. Use a registry that both Docker and containerd
can reach. For a self-contained single-node install, start a persistent local
registry once:

```bash
docker run -d --restart unless-stopped --name clawbox-registry \
  -p 127.0.0.1:5000:5000 \
  -v clawbox-registry:/var/lib/registry registry:2
```

Skip that command when using an existing external registry; authenticate with
that registry before building.

```bash
export REGISTRY=127.0.0.1:5000/clawbox
export CLAWTUNE_ROOT="${CLAWTUNE_ROOT:-$PWD/../ClawTune}"
export TAG="$(git rev-parse --short=12 HEAD)"
export PUSH=1

# Set these only when the default upstreams are unreachable:
# export GOPROXY=https://goproxy.cn,direct
# export NPM_REGISTRY=https://registry.npmmirror.com
# export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

bash scripts/build-kubernetes-images.sh
```

The command fails on a non-native builder or incompatible ClawTune checkout.
After a successful push it prints the three immutable references and saves
them to `.artifacts/platform-images.env`, which `scripts/clawbox install`
discovers automatically. The equivalent shell variables are:

```bash
export CONTROL_IMAGE='REGISTRY/control-plane-arm64@sha256:...'
export RUNTIME_IMAGE='REGISTRY/runtime-arm64@sha256:...'
export TOOL_BRIDGE_IMAGE='REGISTRY/tool-bridge-arm64@sha256:...'
```

Tags such as `:dev` and `:latest` are not release identifiers.

Render deployable manifests from the immutable references:

```bash
python3 scripts/render-kubernetes-images.py \
  --control-image "$CONTROL_IMAGE" \
  --runtime-image "$RUNTIME_IMAGE" \
  --tool-bridge-image "$TOOL_BRIDGE_IMAGE"
```

The rendered files are written to `.artifacts/rendered-deploy/`. Apply those
files, not the source manifests containing development tags.

### Build or overlay a task image

ClawBox does not ship a universal repository image. For SWE-ReBench, provide
the dataset, pinned task selection, SWE-bench checkout, and registry:

```bash
python3 -m pip install -e '.[images]'
python3 scripts/build-swe-rebench-arm64.py \
  --dataset /data/swe-rebench.parquet \
  --selection ../ClawTune/swe_rebench/tasks.json \
  --swebench-root /src/SWE-bench-fork \
  --registry REGISTRY/clawbox \
  --mapping /data/swe-rebench-arm64-map.json \
  --tool-bridge-binary .artifacts/tool-bridge-arm64/tool-bridge \
  --push --fail-fast
```

The mapping is accepted only when each task resolves to a native ARM64 digest.
To add the current production telemetry collector to an existing ARM64 task
image:

```bash
export BASE_IMAGE='REGISTRY/task@sha256:BASE_DIGEST'
export REGISTRY='REGISTRY/clawbox'
export TAG="tool-telemetry-$(git rev-parse --short=12 HEAD)"
export CLAWTUNE_ROOT="${CLAWTUNE_ROOT:-$PWD/../ClawTune}"
bash scripts/rebuild-swe-rebench-tool-overlay.sh
```

Use the immutable `TOOL_IMAGE=...@sha256:...` printed by the command in the
task mapping or Managed API template. Never keep a previous digest after
rebuilding Tool Bridge or eBPF code.

## 5. Configure and deploy

### Recommended automated path

Set the six site-specific task/provider values. The database is optional:
single-node installs default to persistent SQLite; set
`CLAWBOX_DATABASE_URL=postgresql+psycopg://...` when PostgreSQL is required.
Platform image digests come from section 4's generated handoff file.

```bash
export CLAWBOX_LLM_API_KEY='PROVIDER_KEY'
export CLAWBOX_LLM_BASE_URL='https://provider.example/v1'
export CLAWBOX_LLM_MODEL='provider-model'
export CLAWBOX_OPENCLAW_MODEL_REF='vllm/provider-model'
export CLAWBOX_TOOL_IMAGE='REGISTRY/task@sha256:DIGEST'
export CLAWBOX_LLM_EGRESS_CIDR='PROVIDER_CIDR'

scripts/clawbox install
```

`install` validates inputs before creating Secrets, generates independent
tokens, renders digest-pinned manifests, runs migrations, deploys all five
services, and waits for readiness. Re-run `scripts/clawbox configure` with a
corrected database or LLM value to rotate it safely without changing generated
tokens. When `install` succeeds, skip the manual remainder of this section and
all of section 6; continue with section 7 or submit a task immediately.

For credentials that should not enter shell history, set the corresponding
`CLAWBOX_*` variables through the host's secret/environment mechanism. Run
`scripts/clawbox install --help` for the equivalent command-line flags.

### Manual alternative

Use this only when an operator needs to manage the YAML and Secrets directly.

Create the namespaces first:

```bash
kubectl create namespace clawbox-system --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace clawbox-benchmarks --dry-run=client -o yaml | kubectl apply -f -
```

Copy the examples outside the repository and replace every placeholder:

```bash
cp deploy/control-plane-secret.example.yaml /tmp/clawbox-control-plane.yaml
cp deploy/managed-secret.example.yaml /tmp/clawbox-managed.yaml
cp deploy/swe-rebench-secret.example.yaml /tmp/clawbox-llm.yaml
${EDITOR:-vi} /tmp/clawbox-control-plane.yaml
${EDITOR:-vi} /tmp/clawbox-managed.yaml
${EDITOR:-vi} /tmp/clawbox-llm.yaml
```

Secret locations and consumers are fixed:

| Secret | Namespace | Required keys |
| --- | --- | --- |
| `clawbox-control-plane` | `clawbox-system` | `database-url`, `service-token`, `ingest-secret` |
| `clawbox-managed` | `clawbox-system` | `database-url`, `service-token`, `templates` |
| `clawbox-llm` | `clawbox-benchmarks` | `llm-api-key`, `llm-upstream-base-url`, `llm-model`, `openclaw-model-ref` |

Use independent random values of at least 32 bytes for the service and ingest
secrets. Do not use the development defaults in `clawbox/common/config.py`.

The `templates` JSON in `clawbox-managed` must contain:

- a Tool task image pinned by digest;
- the same immutable Runtime image rendered above;
- the exact LLM egress CIDR;
- the intended resource profile and Secret name.

Validate the YAML locally before applying it:

```bash
kubectl apply --dry-run=client -f /tmp/clawbox-control-plane.yaml
kubectl apply --dry-run=client -f /tmp/clawbox-managed.yaml
kubectl apply --dry-run=client -f /tmp/clawbox-llm.yaml
```

PostgreSQL is recommended for production and required for multi-node or
multi-replica Managed API deployments. On a single-node installation,
`scripts/clawbox configure` automatically uses a persistent SQLite database at
`/var/lib/clawbox/managed` when the existing control-plane URL is SQLite.
`scripts/clawbox up` runs migrations inside the pinned control-plane image, so
Alembic does not need to be installed separately on the host. For an explicit
PostgreSQL deployment, migrations can also be run manually:

```bash
export DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST/clawbox'
alembic upgrade head
```

The default tuning KB is single-node SQLite persisted at
`/var/lib/clawbox/tune-kb`. Set `TUNING_DATABASE_URL` in `deploy/tune-kb.yaml`
if PostgreSQL-backed KB storage is required. Back up the selected store before
upgrading.

## 6. Deploy in the required order

```bash
kubectl apply -f deploy/runtimeclass-firecracker.yaml
kubectl apply -f deploy/runtimeclass-firecracker-ebpf.yaml
kubectl apply -f deploy/sandboxtask-crd.yaml
kubectl apply -f deploy/control-plane-rbac.yaml
kubectl apply -f deploy/managed-rbac.yaml

kubectl apply -f /tmp/clawbox-control-plane.yaml
kubectl apply -f /tmp/clawbox-managed.yaml
kubectl apply -f /tmp/clawbox-llm.yaml

capacity_file="$(mktemp)"
sudo env KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}" \
  PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  "$PWD/.venv/bin/python" scripts/collect-node-capacity.py --configmap \
  >"$capacity_file"
kubectl apply -f "$capacity_file"
rm -f "$capacity_file"
kubectl apply -f .artifacts/rendered-deploy/trace-ingester.yaml
kubectl apply -f .artifacts/rendered-deploy/tune-kb.yaml
kubectl apply -f .artifacts/rendered-deploy/cell-controller.yaml

# The manual path must migrate the Managed database before starting its API.
DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST/clawbox' \
  .venv/bin/alembic upgrade head
kubectl apply -f .artifacts/rendered-deploy/managed-control-plane.yaml
```

The supplied Dispatcher uses the served `v1alpha1` CRD. Do not switch it to
`v1alpha2` until the controller and conversion webhook support that version
end to end.

Wait for every service:

```bash
kubectl -n clawbox-system rollout status deployment/clawbox-ingester --timeout=300s
kubectl -n clawbox-system rollout status deployment/clawbox-tune-kb --timeout=300s
kubectl -n clawbox-system rollout status deployment/clawbox-cell-controller --timeout=300s
kubectl -n clawbox-system rollout status deployment/clawbox-managed-api --timeout=300s
kubectl -n clawbox-system rollout status deployment/clawbox-managed-dispatcher --timeout=300s
kubectl -n clawbox-system get pods
```

Confirm that running Deployments use the expected digests:

```bash
kubectl -n clawbox-system get deployment \
  -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image
```

Do not continue if a platform Deployment still shows `:dev`, `:latest`, or an
old digest.

## 7. Run mandatory acceptance gates

First run native eBPF/cgroup acceptance using a task image that contains the
production telemetry overlay:

```bash
bash scripts/run-toolbridge-ebpf-integration.sh \
  --image 'REGISTRY/task@sha256:DIGEST' \
  --namespace clawbox-ebpf-acceptance \
  --output .artifacts/tool-ebpf-acceptance.log
```

Success requires `INTEGRATION_RC=0`, distinct concurrent cgroup IDs, non-zero
CPU/RSS, valid native artifacts, zero telemetry loss, and successful cleanup.
Command exit code zero by itself is not telemetry success.

Then validate KB signature scope and persistence:

```bash
kubectl -n clawbox-system port-forward service/clawbox-tune-kb 8086:8086 \
  >.artifacts/kb-port-forward.log 2>&1 &
kb_forward_pid=$!
for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8086/healthz >/dev/null && break
  sleep 1
done
curl -fsS http://127.0.0.1:8086/healthz >/dev/null
export CLAWBOX_SERVICE_TOKEN='CONTROL_PLANE_TOKEN'
export CLAWBOX_KB_INGEST_SECRET='CONTROL_PLANE_INGEST_SECRET'
python3 scripts/live-kb-smoke.py
kill "$kb_forward_pid"
kubectl -n clawbox-system rollout restart deployment/clawbox-tune-kb
kubectl -n clawbox-system rollout status deployment/clawbox-tune-kb --timeout=300s
kubectl -n clawbox-system port-forward service/clawbox-tune-kb 8086:8086 \
  >.artifacts/kb-port-forward.log 2>&1 &
kb_forward_pid=$!
for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8086/healthz >/dev/null && break
  sleep 1
done
curl -fsS http://127.0.0.1:8086/healthz >/dev/null
python3 scripts/live-kb-smoke.py --verify-only
kill "$kb_forward_pid"
```

Finally validate the Managed API and cancellation path:

```bash
kubectl -n clawbox-system port-forward service/clawbox-managed-api 8085:8085 \
  >.artifacts/managed-port-forward.log 2>&1 &
managed_forward_pid=$!
for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8085/healthz >/dev/null && break
  sleep 1
done
curl -fsS http://127.0.0.1:8085/healthz >/dev/null
export CLAWBOX_API_URL=http://127.0.0.1:8085
CLAWBOX_SERVICE_TOKEN='MANAGED_SERVICE_TOKEN' python3 scripts/live-managed-cancel-smoke.py
kill "$managed_forward_pid"
```

## 8. Submit a real run

```bash
unset CLAWBOX_API_URL CLAWBOX_TOKEN
export CLAWBOX_TENANT='team-a'

scripts/clawbox submit \
  --project demo \
  --template swe-rebench-arm64 \
  --template-revision 1 \
  --input-ref 15five__scim2-filter-parser-13 \
  --problem 'Fix the issue described for this configured task image.' \
  --deadline-seconds 1800 \
  --idempotency-key "demo-$(date +%s)" \
  --watch
```

Use the returned run ID with:

```bash
scripts/clawbox status RUN_ID
scripts/clawbox attempts RUN_ID
scripts/clawbox events RUN_ID
scripts/clawbox watch RUN_ID --timeout 2700
scripts/clawbox cancel RUN_ID
scripts/clawbox retry RUN_ID
```

For direct cluster debugging without the Managed API:

```bash
bash deploy/cell.sh deploy \
  --task demo-001 \
  --tool-image 'REGISTRY/task@sha256:DIGEST' \
  --problem 'Fix the issue described for this task image.' \
  --llm-egress-cidr PROVIDER_CIDR \
  --profile small \
  --timeout-seconds 1800

# Watch until terminal, then press Ctrl-C before deleting the parent object.
kubectl -n clawbox-benchmarks get sandboxtask demo-001 -w
bash deploy/cell.sh delete --task demo-001
```

## Successful deployment checklist

A deployment is complete only when all of the following are true:

- Git working tree is clean and its exact revision is recorded.
- Platform and task images are ARM64 immutable digests.
- Host audit, Kata smoke, and eBPF/cgroup integration pass.
- Five ClawBox Deployments are Available: ingester, tuning KB, Cell
  Controller, Managed API, and Dispatcher.
- A real task reaches a correct terminal state and owned resources are cleaned.
- Cancellation remains terminal and cannot be overwritten by late success.
- KB accepts valid scoped telemetry, rejects tampering/cross-tenant replay,
  and retains its generation after restart.
- No new ClawBox `Failed` Pods remain after acceptance.

## Upgrading an existing deployment

1. Create a clean release worktree from `origin/main`; never build from a
   divergent or dirty checkout.
2. Run tests and record the ClawBox and ClawTune revisions.
3. Back up PostgreSQL, `/var/lib/clawbox/tune-kb`, and trace data.
4. Build and push new revision-tagged images.
5. Render new digest-pinned manifests.
6. Run `alembic upgrade head` before rolling out the matching API image.
7. Apply manifests and wait for rollouts.
8. Rerun all mandatory acceptance gates.

Do not rerun host bootstrap `apply` during a software upgrade.

## Troubleshooting

### Host gate reports KVM, containerd, or LVM failures

Run `sudo -v`, then rerun the gate with `sudo`. Non-interactive SSH sessions
do not inherit an interactive sudo credential.

### Existing checkout is ahead and behind `origin/main`

Do not pull or reset it. Use the detached release-worktree procedure in
section 1.

### A Pod uses an old image after rebuild

Tags are mutable and node caches can be stale. Render and deploy a new digest;
do not solve this by repeatedly restarting a `:dev` Deployment.

### KB generation does not advance

Check all four values together: `CLAWBOX_KB_ENDPOINT`, `CLAWBOX_KB_TOKEN`,
`CLAWBOX_KB_INGEST_SECRET`, and the exact repository fingerprint. Invalid,
incomplete, lossy, or identity-mismatched telemetry is rejected by design.

### Thousands of historical Failed Pods exist

Inspect first, then delete only terminal objects in an explicitly named
namespace:

```bash
python3 scripts/cleanup-failed-pods.py
python3 scripts/cleanup-failed-pods.py --namespace clawbox-system --apply
```

The script never selects Running, Pending, or Succeeded Pods. Cleaning a
non-ClawBox namespace requires separate operator review and explicit scope.

## Developer-only checks

The following Docker harness checks control-plane logic on Linux. It is not a
ClawBox deployment and does not validate Firecracker or native eBPF:

```bash
bash scripts/linux-deploy.sh all
bash scripts/linux-deploy.sh status
bash scripts/linux-deploy.sh down
```

Repository checks:

```bash
python3 -m pytest -q
python3 -m py_compile $(find clawbox scripts -name '*.py')
cd toolbridge && go test -race ./...
```

Host, Firecracker, cgroup, or eBPF changes always require the real ARM64 gates
above. Maintainers should also read [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md).

## Advanced workflows

- Kubernetes manifest details: [deploy/README.md](deploy/README.md)
- Execution and telemetry invariants: [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)
- Benchmark scripts: `scripts/run-swe-rebench.sh`,
  `scripts/scale-swe-rebench.sh`, and `scripts/load-test.sh`
