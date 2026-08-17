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

## Local verification

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest
python3 -m py_compile $(find clawbox scripts -name '*.py')
```

Local tests are not a substitute for target-machine acceptance; production-passing evidence must include the host gates, a dual-Pod smoke test, Firecracker host processes/journal, and devmapper cleanup results.
