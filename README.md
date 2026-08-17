# ClawBox: Firecracker-first ARM64 benchmark runtime

ClawBox runs a dual-VM Cell per `SandboxTask` on Kunpeng/openEuler ARM64 hosts. The Tool Pod hosts the immutable SWE-ReBench task image and a static SSH Tool Bridge; the Runtime Pod hosts OpenClaw and runs ClawTune as a native Kubernetes sidecar. Both Pods are pinned to `kata-fc-arm64`, with no fallback path to other architectures or VMMs.

```text
SandboxTask
  ├─ Tool Pod (Kata + Firecracker) ── SSH/2222 ──┐
  └─ Runtime Job/Pod (Kata + Firecracker)        │
       ├─ runtime: OpenClaw ─────────────────────┘
       └─ native sidecar: ClawTune observe-only
              └─ task token → central trace/result ingester
```

This repository contains the full software implementation, fail-closed host gates, and automated tests. Real `/dev/kvm`, two dedicated disks, and Firecracker microVMs can only be validated on a target Kunpeng host; a deployment should not be marked production-passing unless the target-machine commands have been run.

## Fixed boundaries

- Kata Containers `3.31.0`, Firecracker `1.12.1`, Linux `arm64` guest/host.
- containerd 2.x `devmapper` snapshotter; loop devices are forbidden in production.
- The RuntimeClass name and handler are both `kata-fc-arm64`, and per-VM Pod overhead is declared.
- Every task must map to a `linux/arm64` registry digest; `unsupported-arm64` blocks submission.
- The Tool Pod only receives a task-specific SSH public key/host key, never LLM or upload credentials.
- Runtime and Tool share no hostPath, PVC, or RWX; results and traces are uploaded via a short-lived, task-scoped token.
- ClawTune is only enabled with `observe-only` and `hook-only`; cgroup, affinity, and NUMA control are disabled.

## 1. Host and Firecracker

Start with a read-only plan on the target machine; `apply` installs Kubernetes/containerd/Kata and only initializes the two dedicated disks when given a confirmation string that exactly matches the canonical device names:

```bash
bash scripts/bootstrap-openeuler-arm64.sh plan \
  --devmapper-data-device /dev/DATA_DISK \
  --devmapper-meta-device /dev/META_DISK

sudo bash scripts/bootstrap-openeuler-arm64.sh apply \
  --devmapper-data-device /dev/DATA_DISK \
  --devmapper-meta-device /dev/META_DISK \
  --confirm-erase /dev/DATA_DISK,/dev/META_DISK
```

If you prefer to run the steps separately, use:

```bash
bash scripts/audit-kata-firecracker-arm64.sh --root /opt/kata
bash scripts/build-kata-firecracker-arm64.sh plan
sudo bash scripts/setup-devmapper-openeuler-arm64.sh status
bash deploy/check-host.sh --runtime-class kata-fc-arm64
bash scripts/arm64-kata-smoke.sh --runtime-class kata-fc-arm64
```

Only after the last two checks pass should a node be labeled `clawbox.openai.com/firecracker-ready=true` (the bootstrap labels it automatically; for a manual run):

```bash
kubectl label node "$(hostname)" clawbox.openai.com/firecracker-ready=true
```

The live smoke gate (`arm64-kata-smoke.sh`) is the authoritative FC-3/FC-4 check: it launches two `kata-fc-arm64` Pods (tool + runtime) and one runc attacker Pod in a throwaway namespace, then verifies arm64 guest boot IDs, Runtime→Tool networking through the Service, attacker isolation via NetworkPolicies, Firecracker-only host processes, and devmapper snapshot reclamation. Both `check-host.sh` and the smoke script are safe to run as an unprivileged user (privileged probes fall back to passwordless `sudo`). The smoke image contract is `busybox:1.36` (default) — it must provide `/bin/sh`, `uname`, busybox `httpd` and `wget`; do **not** switch to `alpine:3.22`, whose busybox build drops the `httpd` applet (tool container exits 127 with `httpd: applet not found`).

See [docs/OPENEUER_ARM64.md](docs/OPENEUER_ARM64.md) for detailed steps and rollback boundaries, and [docs/AGENT_HANDOFF_2026-08-17.md](docs/AGENT_HANDOFF_2026-08-17.md) for the validated target-machine record (FC-0 18/18, host gate 19/19, live smoke pass) plus the operational incidents and fixes.

### 1.1 Local image registry and Buildx (single-node setup)

A single-node cluster can build and pull images through a registry on its own loopback. Do this once per host, before building images:

```bash
# 1) Install Buildx — the image Dockerfiles use BuildKit-only features
#    (`# syntax=docker/dockerfile:1`, `--platform=$BUILDPLATFORM`); the legacy
#    builder fails with `invalid platform ""`. openEuler has no
#    docker-buildx-plugin package, so install the official arm64 binary:
docker buildx version >/dev/null 2>&1 || {
  mkdir -p ~/.docker/cli-plugins
  curl -fsSL https://github.com/docker/buildx/releases/download/v0.15.1/buildx-v0.15.1.linux-arm64 \
    -o ~/.docker/cli-plugins/docker-buildx
  chmod +x ~/.docker/cli-plugins/docker-buildx
}
docker buildx version

# 2) Run a loopback registry (Docker must be able to run containers)
sudo docker rm -f clawbox-registry 2>/dev/null || true
sudo docker run -d --name clawbox-registry --restart=always \
  -p 127.0.0.1:5000:5000 registry:2
curl -s http://127.0.0.1:5000/v2/ && echo " registry v2 ok"

# 3) containerd trust for 127.0.0.1:5000 is ALREADY in the bootstrap-generated
#    config. Verify it — do NOT append it again: appending a table that already
#    exists crashes containerd with `toml: table ... already exists`.
sudo grep -n -A3 '127.0.0.1:5000' /etc/containerd/config.toml

# 4) End-to-end proof: docker push → kubelet-side (containerd/crictl) pull
docker tag busybox:1.36 127.0.0.1:5000/clawbox/test:busybox
docker push 127.0.0.1:5000/clawbox/test:busybox
sudo crictl pull 127.0.0.1:5000/clawbox/test:busybox && echo "crictl pull OK"
sudo crictl rmi 127.0.0.1:5000/clawbox/test:busybox
```

After this, build and push with `REGISTRY=127.0.0.1:5000/clawbox`.

## 2. ARM64 images

All builds must run on a native ARM64 Docker daemon; detection of a foreign-architecture binfmt handler fails the build outright. The builds need Go, npm and PyPI reachable from inside the build containers; on restricted networks export mirrors first (`build-kubernetes-images.sh` forwards `GOPROXY`, `NPM_REGISTRY` and `PIP_INDEX_URL` as build args, defaulting to the official upstreams):

```bash
export GOPROXY=https://goproxy.cn,direct            # proxy.golang.org times out from CN hosts
export NPM_REGISTRY=https://registry.npmmirror.com
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
export HF_ENDPOINT=https://hf-mirror.com            # for the SWE-ReBench dataset download

REGISTRY=127.0.0.1:5000/clawbox PUSH=1 \
  CLAWTUNE_ROOT=~/ClawTune \
  bash scripts/build-kubernetes-images.sh
```

`validate_clawtune_integration.py` must pass first; it validates the ClawTune checkout layout, the ClawTune plugin, the sidecar package, and required assets.

The 128 SWE-ReBench images are built from the install recipes in the full dataset and a pinned SWE-bench fork. First pin the fork to the factory default commit:

```bash
git clone https://github.com/SWE-rebench/SWE-bench-fork.git /src/SWE-bench-fork
git -C /src/SWE-bench-fork checkout 980d0cca8aa4e73f1d9f894e906370bef8c4de8a
```

`--selection` refers to the 128 tasks selected by ClawTune. You can use a full local export that includes `install_config`:

```bash
python3 scripts/build-swe-rebench-arm64.py \
  --dataset /data/swe-rebench-full.parquet \
  --selection /src/ClawTune/swe_rebench/tasks.json \
  --swebench-root /src/SWE-bench-fork \
  --registry registry.example.com/clawbox \
  --mapping /data/swe-rebench-arm64-map.json \
  --tool-bridge-binary .artifacts/tool-bridge-arm64/tool-bridge \
  --push --fail-fast
```

Alternatively, you can have the factory read the official dataset at a pinned revision:

```bash
python3 scripts/build-swe-rebench-arm64.py \
  --dataset-id nebius/SWE-rebench \
  --dataset-revision 4ece23ba02fe8b68858e430134adddfd64d6f0f4 \
  --selection /src/ClawTune/swe_rebench/tasks.json \
  --swebench-root /src/SWE-bench-fork \
  --registry registry.example.com/clawbox \
  --mapping /data/swe-rebench-arm64-map.json \
  --tool-bridge-binary .artifacts/tool-bridge-arm64/tool-bridge \
  --push --fail-fast
```

The factory validates `/testbed`, the shell, git, patches, dependency/test commands, and the ARM64 Tool Bridge before writing a mapping that records `platform`, `recipe_revision`, and the registry digest. Any entry that cannot be built natively is recorded as `unsupported-arm64`, and the launcher does not allow fallback.

Before reading Parquet or remote datasets from Hugging Face, install the optional image-factory dependencies: `python3 -m pip install -e '.[images]'`.

## 3. Control plane and tasks

> For the exact, validated single-task run (real LLM, local registry, all
> gotchas), follow **§6 "Run one agent task end-to-end"** first.

Replace the example image names with real digests/tags and create the `clawbox-control-plane` and `clawbox-llm` Secrets, then deploy:

```bash
kubectl apply -f deploy/sandboxtask-crd.yaml
kubectl apply -f deploy/control-plane-rbac.yaml
kubectl apply -f deploy/trace-ingester.yaml
python3 scripts/collect-node-capacity.py --configmap | kubectl apply -f -
kubectl apply -f deploy/cell-controller.yaml
```

A template for `clawbox-control-plane` is in `deploy/control-plane-secret.example.yaml`; the keys for `clawbox-llm` are in `deploy/swe-rebench-secret.example.yaml`. The placeholder values in both templates must be replaced, and a production ingester should use persistent PostgreSQL.

Submit a single template:

```bash
TASK_ID=demo TOOL_IMAGE='registry.example.com/task@sha256:…' \
LLM_EGRESS_CIDR=203.0.113.10/32 bash deploy/cell.sh render | kubectl apply -f -
kubectl get sandboxtasks -n clawbox-benchmarks -w
```

Run the dataset:

```bash
bash scripts/run-swe-rebench.sh \
  --tasks /src/ClawTune/swe_rebench/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-cidr 203.0.113.10/32 \
  --parallelism 8
```

The controller drives each task strictly through `Queued → Admitted → ToolStarting → ToolReady → RuntimeRunning → Collecting → Succeeded/Failed/TimedOut → Cleaned`. If the full dual-VM budget is not available, the task stays `Queued` and never creates a half-built Cell.

## 4. Capacity and incremental load testing

The `small`, `medium`, and `large` profiles account for the Runtime, Tool, ClawTune sidecar, both RuntimeClass overheads, and a 10% safety margin. Node capacity is derived from the allocatable resources of ready ARM64 nodes, minus non-Cell Pod requests, and is bounded by the devmapper baseline budget.

```bash
python3 scripts/collect-node-capacity.py
bash scripts/scale-swe-rebench.sh \
  --tasks /src/ClawTune/swe_rebench/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json \
  --llm-egress-cidr 203.0.113.10/32
```

Load testing ramps in fixed steps of `1, 2, 4, 8, 16, 32` and stops on the first task failure or a thin-pool pressure gate failure. See [docs/IMPLEMENTATION_MAPPING.md](docs/IMPLEMENTATION_MAPPING.md) for an index of architecture and implementation files.

## 5. Target-machine operations and troubleshooting

These are the incidents that actually occur on a Kunpeng/openEuler single node, ordered by likelihood. Full evidence and fixes are in [docs/AGENT_HANDOFF_2026-08-17.md](docs/AGENT_HANDOFF_2026-08-17.md).

### 5.1 Restarting containerd takes down the control plane and orphans VMs

`containerd.service` runs with systemd `KillMode=control-group`, so `systemctl restart containerd` kills every runc container in the service cgroup — including the static control-plane Pods (etcd, kube-apiserver, ...). Kata shims and Firecracker VMs escape the cgroup kill and survive as orphans.

1. kubelet re-creates the static Pods automatically within ~60-90 s; do not panic about `CrashLoopBackOff` immediately after a restart.
2. If `kube-apiserver` stays down and etcd reports `listen ...:2380: bind: address already in use`, an **orphaned control-plane process** from before the restart is still holding the port:

```bash
# pkill matches process NAMES, which are truncated to 15 chars — use -f for long names
sudo pkill -9 -f 'kube-controller-manager' || true
sudo pkill -9 etcd || true
sudo pkill -9 kube-apiserver || true
sudo pkill -9 kube-scheduler || true
sudo ss -ltnp | grep -E ':(2379|2380|6443|10257|10259)' || echo "ports free"
sudo systemctl restart kubelet      # clears the 5m CrashLoopBackOff backoff
```

### 5.2 Leaked Kata VMs from failed smoke runs

Failed smoke runs can leave sandboxes behind; because shims escape the cgroup kill they survive containerd restarts and accumulate (observed: ~490 kata shims + 244 Firecrackers). Symptoms: `docker run` fails with shim `connection refused`, hundreds of `containerd-shim-kata-v2` / `firecracker` processes in `ps`. Cleanup:

```bash
# crictl -o json returns {"items":[...]}, NOT a bare list
sudo crictl pods -o json | python3 -c '
import json,sys
for p in json.load(sys.stdin)["items"]:
    if p.get("metadata",{}).get("namespace","") != "kube-system":
        print(p["id"])' > /tmp/leak-pods
sudo crictl rmp -f $(cat /tmp/leak-pods) 2>/dev/null || true
sleep 10
sudo pkill -9 -f 'containerd-shim-kata-v2' || true
sudo pkill -9 -f jailer || true
sudo pkill -9 -f firecracker || true
ps -eo args | grep -c '[f]irecracker'   # expect 0
```

### 5.3 Editing the containerd config

The bootstrap-generated config (v4) already contains `[plugins."io.containerd.cri.v1".registry]` (including the loopback trust) and many other tables. **Never append a table that already exists** — TOML rejects it and containerd will not start. Before any restart, validate that the config parses:

```bash
sudo grep -n -A3 '127.0.0.1:5000' /etc/containerd/config.toml   # inspect before adding
sudo containerd config dump >/dev/null && echo "config parse OK"  # preflight
sudo systemctl restart containerd
```

### 5.4 Image build failures on restricted networks

- `go mod download` timeout → export `GOPROXY` (see §2).
- `invalid platform ""` from the legacy builder → install Buildx (see §1.1).
- `npm ci` / `pip install` timeouts → export `NPM_REGISTRY` / `PIP_INDEX_URL` (see §2).

### 5.5 Pre-flight before touching anything

```bash
sudo systemctl is-active containerd kubelet docker
kubectl get nodes
ps -eo args | grep -c '[f]irecracker'     # 0 with no running Cells
kubectl get pods -A                       # control plane 1/1 Running
```

## 6. Run one agent task end-to-end (validated 2026-08-17)

This is the exact, validated sequence that got a single agent task to run the
full chain (Tool Bridge → runtime sidecar → OpenClaw → real LLM → agent SSH into
`/testbed` → patch + trace upload). Run these on the target Kunpeng host. A
real task (`--timeout-seconds 1800`) takes ~21 min from `single-image-scale.sh`
start to `Succeeded` (validated `real001-001`); a debug run (`--timeout-seconds
480`) finishes in ~13 min but usually produces no patch.

### 6.0 Prerequisites (already in place on the validated host)

- Cluster ready, node labeled `clawbox.openai.com/firecracker-ready=true`,
  FC gates passed (§1), loopback registry `127.0.0.1:5000` up (§1.1).
- `~/ClawBox` and `~/ClawTune` checkouts on the target.
- Shell proxy note: any `kubectl`/k8s-client command needs the proxy env unset
  (`export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12; unset
  http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY`), or
  kubectl routes `193.124.7.2:6443` through the SOCKS proxy and times out.
- Non-interactive SSH has **no passwordless sudo** on the validated host: the
  script's `ctr` pre-pull and devmapper status print sudo errors. These are
  best-effort and non-fatal; the controller's serialized admission covers the
  same-image unpack race for low cell counts.

### 6.1 Sync the code

```bash
cd ~/ClawBox
# The target's global git config may point at an offline SOCKS proxy; bypass it
# (a plain `git pull` will hang or fail). If local CRLF edits exist, a clean
# reset to origin/main is safest (CRLF in *.sh shebangs breaks scripts):
git -c http.proxy= -c https.proxy= fetch origin main
git reset --hard origin/main
git log --oneline -1   # expect the newest commit

# Verify the two load-bearing fixes are present in the source:
grep -c 'clawtune-sidecar-entrypoint' scripts/runtime-entrypoint.sh   # expect ≥1
grep -c 'pre-creating sandbox runtime root' scripts/runtime-entrypoint.sh  # expect ≥1 (openclaw marker)
grep -c 'remoteWorkspaceDir: workspaceRoot' docker/Dockerfile.runtime  # expect ≥1 (openclaw patch)
grep -n 'KUBERNETES_IMAGE_PULL_POLICY' deploy/cell-controller.yaml    # expect Always
```

Two fixes are load-bearing — do not regress them:

1. **Runtime Job is a single container** (Kata cannot share volumes across
   containers on this host), so the clawtune sidecar runs **in-process**:
   `scripts/runtime-entrypoint.sh` starts
   `/usr/local/bin/clawtune-sidecar-entrypoint` in the background before the
   `127.0.0.1:8765/health/ready` wait. Without this the runtime exits ~60 s in
   with `curl: Failed to connect to 127.0.0.1 port 8765` → `RuntimeFailed`.
2. **Cell containers must re-pull `:dev` images**: `deploy/cell-controller.yaml`
   sets `KUBERNETES_IMAGE_PULL_POLICY=Always` (default is `IfNotPresent`, which
   silently reuses a stale locally-cached `:dev` image — observed as the runtime
   pod running the pre-fix digest). Verify the live deployment too:
   `kubectl -n clawbox-system set env deployment/clawbox-cell-controller KUBERNETES_IMAGE_PULL_POLICY=Always`
   then `kubectl -n clawbox-system rollout restart deployment/clawbox-cell-controller`.

### 6.2 Build and push the three images (native arm64 daemon)

```bash
cd ~/ClawBox
export GOPROXY=https://goproxy.cn,direct
export NPM_REGISTRY=https://registry.npmmirror.com
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
export APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn
REGISTRY=127.0.0.1:5000/clawbox PUSH=1 CLAWTUNE_ROOT=~/ClawTune \
  bash scripts/build-kubernetes-images.sh
```

Only `runtime-entrypoint.sh` / `Dockerfile.runtime` changed? Rebuild just the
runtime image (cached layers make it fast). The Dockerfile bakes in the
**openclaw sandbox-root patch** (heredoc `RUN` that rewrites the installed
`openclaw` bundle so the SSH sandbox root is `/testbed` — required for the
agent's read/write/edit tools to touch the task source; see §6.8). If the
patch step fails the build fails (`process.exit(1)`), which is intended.

```bash
cd ~/ClawBox
docker build --platform linux/arm64 --pull --build-context clawtune=~/ClawTune \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn \
  -f docker/Dockerfile.runtime -t 127.0.0.1:5000/clawbox/runtime-arm64:dev .
# verify both fixes are inside the image
# (entrypoint path has NO .sh suffix in the image)
docker run --rm --entrypoint /bin/sh 127.0.0.1:5000/clawbox/runtime-arm64:dev -c '
  grep -c clawtune-sidecar-entrypoint /usr/local/bin/runtime-entrypoint
  D="$(npm root -g)/openclaw/dist"
  grep -l "remoteWorkspaceDir: workspaceRoot" "$D"/*.js    # expect one file
  grep -c pre-creating /usr/local/bin/runtime-entrypoint'
docker push 127.0.0.1:5000/clawbox/runtime-arm64:dev
```

### 6.3 Deploy / update the control plane

```bash
cd ~/ClawBox
kubectl apply -f deploy/sandboxtask-crd.yaml
kubectl apply -f deploy/control-plane-rbac.yaml
kubectl apply -f deploy/trace-ingester.yaml
python3 scripts/collect-node-capacity.py --configmap | kubectl apply -f -
kubectl apply -f deploy/cell-controller.yaml
kubectl -n clawbox-system rollout status deployment/clawbox-cell-controller --timeout=120s
```

### 6.4 Configure the LLM secret (`clawbox-llm`) with a real endpoint

The default secret is placeholder-only (`https://api.example.com/v1`,
`placeholder-model`) — the agent fails at the first LLM call. Set real values
(DeepSeek is reachable directly from the host; OpenAI/Anthropic are not):

```bash
kubectl -n clawbox-benchmarks patch secret clawbox-llm --type=merge -p '{
  "stringData": {
    "llm-upstream-base-url": "https://api.deepseek.com",
    "llm-model": "deepseek-v4-flash",
    "openclaw-model-ref": "vllm/deepseek-v4-flash",
    "llm-api-key": "PLACEHOLDER"
  }
}'
# inject the real key — read server-side from the key file, never through chat:
kubectl -n clawbox-benchmarks patch secret clawbox-llm --type=merge \
  -p "{\"stringData\":{\"llm-api-key\":\"$(cat ~/ClawTune/swe_rebench/llm_api_key.txt)\"}}"
# verify (key masked)
kubectl -n clawbox-benchmarks get secret clawbox-llm -o jsonpath='{.data.llm-upstream-base-url}' | base64 -d; echo
```

The runtime opens the model as `deepseek-v4-flash` (sidecar rewrites
`/v1/models`), upstreams to DeepSeek. The runtime pod reads this secret at
creation — a cell started before the patch keeps the old values.

### 6.5 Run one cell

**Always use a UNIQUE `--prefix`** per run (see the 409 gotcha in §6.7). The
default prefix is `single`, which collides with any earlier `single-001` result
already stored in the ingester and makes the final result upload fail with 409.

```bash
cd ~/ClawBox
export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
PREFIX="run$(date +%s)"          # unique per run — do not reuse task names
bash scripts/single-image-scale.sh \
  --tool-image 127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:bdf4637498a4b765f0e91333ff292c226bd011a31c220d76609daff43e2c39fd \
  --problem-file scripts/problem-scim2-13.txt \
  --prefix "${PREFIX}" \
  --llm-egress-cidr 0.0.0.0/0 \
  --count 1 --timeout-seconds 1800 --wait-seconds 3900
```

- `--llm-egress-cidr 0.0.0.0/0` opens TCP 443 egress for the DeepSeek API on the
  runtime NetworkPolicy. Fine for a single-task validation; pin to the provider's
  CIDR for production.
- **Budget (`--timeout-seconds`)**:
  - Real task (needs a non-empty patch): use `1800`+ — a real run takes ~21 min
    to `Succeeded` (validated `real001-001`). `480` is too short for a real task
    (the agent is still exploring when the LLM request gets cut off).
  - Debug/pipeline check only: `480` makes the whole cell fail/succeed fast.
  - The cell `timeoutSeconds` bounds the runtime Job (`activeDeadlineSeconds`)
    and the agent's `--timeout`; the entrypoint reaps a lingering OpenClaw
    process once `final-answer.json` is written (grace 30 s).
- **Problem statement matters a lot**: a vague prompt ("Fix the reported
  issue…") makes the agent spend the whole budget exploring. Use the real
  SWE-ReBench issue text (repo copy: `scripts/problem-scim2-13.txt`). With the
  real statement the agent locates and fixes the bug in ~4 min.
- The task image digest above is the validated SWE-ReBench arm64 image with the
  Tool Bridge baked in (`15five__scim2-filter-parser-13`, `/testbed` chowned to
  10001). Do not use the older `ef4a5559…` digest (no bridge).
- **Expected startup log noise** (harmless): the script's `ctr pre-pull` and
  devmapper status print `sudo: a password is required` (no passwordless sudo);
  the runtime pod logs `pre-creating sandbox runtime root …` then
  `runtime-root-ok` **and** `WARN: could not pre-create sandbox runtime root`
  (the intermittent runtime→tool SSH 255 — the marker dir is still created, so
  this is benign).

### 6.6 Success criteria

1. `kubectl get sandboxtasks -n clawbox-benchmarks | grep <prefix>` →
   `Succeeded`/`Cleaned` with `outcome=Succeeded`.
2. Runtime pod (`kubectl logs <runtime-pod> -n clawbox-benchmarks`) shows the
   milestones in order:
   `starting clawtune sidecar in-process (port 8765)` → `ready=true` →
   `pre-creating sandbox runtime root …` → `runtime-root-ok` → agent turns with
   `model-fetch … status=200` and `tool exec/edit: ok` (agent is really SSHing
   into `/testbed`).
3. Inside the runtime pod: `final-answer.json` + `result.json` written,
   `logs/agent.log` shows the working session, `traces/session-*.jsonl` growing.
4. The runtime Job exits 0 after the sidecar uploads result+trace to the ingester
   (`.upload-complete`), and the controller transitions
   `RuntimeRunning → Collecting → Succeeded (ArtifactsDurable)`.
5. **Real-task success** — verify the ingester stored a non-empty patch:
   `bash /tmp/verify-read-fix.sh <task_id>` → expect `status: succeeded`,
   `patch_status: present`, `patch len > 0`, `escapes count: 0`.

Logs live under `/state/<task>/logs/` (agent, sidecar, onboard, plugin) inside
the runtime pod. The controller cleans child pods ~10–16 s after a failure, so
for debugging either watch live (`kubectl logs -f`) or run a background watcher
(`CLAWBOX_WATCH_SECONDS=N bash /tmp/start-watcher.sh`, snapshots pod logs to
`/tmp` before cleanup).

### 6.7 Quick failure diagnosis

| Symptom | Cause / check |
|---|---|
| Runtime exits ~60 s, `curl … 8765: Couldn't connect` | Sidecar not running — image missing the in-process-sidecar fix (verify image digest §6.2, `imagePullPolicy` §6.1) |
| Runtime pod uses an old image digest | `KUBERNETES_IMAGE_PULL_POLICY` not `Always` (§6.1) |
| Runtime fails at the LLM call (401/404/connection) | `clawbox-llm` values (§6.4), egress CIDR/port (§6.5), API key validity |
| Tool log: `ssh handshake failed … EOF` every 2 s | Benign — that's the kubelet `tcpSocket` readiness probe hitting port 2222 |
| Cell stays `Queued` | Node not labeled `firecracker-ready`, or cell budget unavailable |
| `ctr pre-pull` / devmapper sudo errors in the script | Expected without passwordless sudo; non-fatal for low counts (§6.0) |
| **Final upload fails, cell `Failed` with `.upload-failed`** | **Task name reused.** The ingester result is immutable (`task_id` primary key, first write wins); rerunning the same name with different content → `POST /v1/tasks/<id>/result` → 409. **Always use a unique `--prefix`** (§6.5) |
| agent read/write/edit fails: `Sandbox path escapes allowed mounts` | openclaw sandbox-root patch missing — rebuild runtime image (§6.2) and verify `remoteWorkspaceDir: workspaceRoot` is inside it |
| runtime pod logs `WARN: could not pre-create sandbox runtime root` right after `runtime-root-ok` | Benign — the intermittent runtime→tool SSH 255; the marker dir is already created |
| Cell `Succeeded` but `patch_status: empty` | The agent ran out of budget before editing, or the problem statement was too vague. Give `--timeout-seconds 1800+` and use the real issue text (§6.5) |
| Agent spends the whole run on environment setup | The task image's default python lacks test deps (`sly`/`django`); the real env is `/opt/miniconda3/envs/testbed/bin/python`. Consider preinstalling, or hint it in the problem text |

### 6.8 The openclaw read/write tool fix (validated `real001-001`)

**Why it was needed**: openclaw's SSH sandbox exposes only one container mount,
`remoteWorkspaceDir = <workspaceRoot>/openclaw-ssh-<scope>-<hash>/workspace`
(`/testbed/openclaw-ssh-shared-8198076c/workspace`), and its path check is a
*string* prefix test — so the agent's `read`/`write`/`edit` on absolute
`/testbed/src/...` paths failed with `Sandbox path escapes allowed mounts`
(exec always worked). A symlink cannot fix it; only making the mount root
itself `/testbed` works.

**Fix (already baked into the runtime image)**:
1. `docker/Dockerfile.runtime` patches the installed openclaw bundle so
   `remoteWorkspaceDir = workspaceRoot` (i.e. `/testbed`).
2. `scripts/runtime-entrypoint.sh` pre-creates the per-scope marker dir
   `/testbed/openclaw-ssh-shared-8198076c` on the tool VM before the agent
   starts — this makes openclaw's `ensureRuntime` guard skip its destructive
   "replace remote workspace from local" copy (which would otherwise wipe
   `/testbed`). See §6.2 for rebuild/verify.

**Validated result (2026-08-17)**: cell `real001-001` (real issue text, 1800 s
budget) reached `Cleaned/Succeeded` in 21 min; the agent used the **read** tool
on `/testbed/src/scim2_filter_parser/transpilers/sql.py` and the **edit** tool
(`edit: ok in 1650ms`); the uploaded result has `status: succeeded`,
`patch_status: present`, `patch len = 1175` (semantically identical to the gold
PR #13 `AttrPath` namedtuple fix), `final_answer` 13329 B, `escapes count = 0`.

### 6.9 Driving the target from a Windows laptop (PowerShell)

The validated host is reached over SSH from PowerShell. Keep these rules to
avoid the quoting failures that waste the most time:

- **Never inline complex commands with nested quotes** (`ssh host "…"` plus
  inner `"`/`'`/`$()` breaks). Write a small script file, `scp` it, then run:
  ```powershell
  scp .\scripts\check-prefix.sh weitianc@193.124.7.2:/tmp/check-prefix.sh
  ssh weitianc@193.124.7.2 "bash /tmp/check-prefix.sh run123"
  ```
- PowerShell expands `$(...)` before SSH sees it — pass unique prefixes as
  literals (`--prefix run123`) or compute them on the target.
- Remote `git`/`kubectl` need the proxy env unset
  (`export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12; unset
  http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY`).
- Helper scripts (all in `scripts/`; scp to `/tmp` on the target then
  `bash /tmp/<name>`): `check-prefix.sh <prefix>` (cell/pod/log snapshot),
  `check-agent-log.sh <prefix>` (agent tool activity),
  `verify-read-fix.sh <task_id>` (ingester result: status/patch/escapes),
  `dump-result.sh <task_id>`, `dump-answer.sh <task_id>`,
  `diag-ingester.sh` (all ingester rows), `start-watcher.sh` (background log
  snapshot, copy from `.tmp-remote/` on the target or recreate).
- The laptop's own SSH connection can drop intermittently; commands that
  finished on the target keep running — re-ssh and re-check, don't restart the
  run.

## Local verification

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest
python3 -m py_compile $(find clawbox scripts -name '*.py')
```

Local tests are not a substitute for target-machine acceptance; production-passing evidence must include the host gates, a dual-Pod smoke test, Firecracker host processes/journal, and devmapper cleanup results.
