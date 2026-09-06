# ClawBox experiment operations

This runbook covers the supported standalone CubeSandbox path. It is intended
for provisioning a new ARM64 machine and for running another experiment on an
already validated machine. Historical Kubernetes files remain diagnostics;
they are not the ClawBox execution substrate.

## 1. Plan storage before installation

ClawBox uses three distinct kinds of disk space:

1. **CubeSandbox backing storage** holds VM writable layers, images, and pause
   snapshots. Put it on a large, reflink-capable XFS filesystem. The checked-in
   Kunpeng profile uses `/data/cubelet` and
   `/data/cubelet/snapshot_pack`.
2. **Per-VM writable disk** is fixed when a Runtime or Tool template is built.
   `scripts/register-cube-template.py --writable-layer-size 20G` controls it.
   Tool `/workspace` is inside this disk; ClawBox does not add a hostPath, PVC,
   NFS mount, or shared workspace volume.
3. **Experiment results** are host files selected by `--output-root`, for
   example `/data/clawbox-results`. This includes event JSONL, gateway and
   policy records, Runtime traces, Tool telemetry, lifecycle timings,
   validation, and summaries.

Nominal writable-disk offer is `concurrency * (Runtime disk + Tool disk)`.
At c60 with 20 GiB for each VM this is 2.4 TiB. CubeSandbox may store sparse or
reflink data efficiently, but do not depend on that for correctness: reserve
room for simultaneous snapshots, image layers, telemetry, and cleanup.

```bash
df -hT /data/cubelet /data/clawbox-results
findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS -T /data/cubelet
xfs_info /data/cubelet | grep 'reflink=1'
```

The historical Kunpeng installer can create a 200 GiB XFS loopback image via
`bootstrap.nodeInit.dataCubelet.loopback.size` in
`deploy/cubesandbox/runtime-values-kunpeng920.yaml`. For c60 or large coding
repositories, use a larger dedicated XFS filesystem and disable the loopback
path rather than filling 200 GiB. `output.directory` does not relocate VM or
snapshot storage.

## 2. Prepare a new machine

Use ARM64 with `/dev/kvm`, cgroup v2, an eBPF/kprobe-capable guest kernel,
Docker/BuildKit, Git, Python 3.12+, and sufficient XFS storage. Keep ClawBox
and ClawTune beside each other:

```bash
mkdir -p "$HOME/src"
cd "$HOME/src"
git clone <ClawBox repository URL> ClawBox
git clone <ClawTune repository URL> ClawTune
cd ClawBox
git checkout <ClawBox commit used by the experiment>
git -C ../ClawTune checkout <ClawTune commit used by the experiment>
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev,postgres]'
```

Record both commits. Prefer `.venv/bin/python -m clawbox.cli`; a globally
installed `clawbox` command may resolve a different checkout.

Prepare the semantic CubeSandbox source and install its matching Python SDK as
described in [cubesandbox-setup.md](cubesandbox-setup.md):

```bash
export CUBE_SOURCE_DIR="$PWD/.cubesandbox"
bash deploy/cubesandbox/prepare-semantic-source.sh
.venv/bin/python -m pip install -e "$CUBE_SOURCE_DIR/sdk/python"
```

The required additions are the `(sandbox_id, container_port)` TCP endpoint,
same-node HostPort hairpin support, and immutable template-image provenance.
For the Kubernetes Kunpeng diagnostic profile only, storage, guest-kernel, and
S3lvol procedures are in
[kunpeng920-reproduction-runbook.md](kunpeng920-reproduction-runbook.md).

## 3. Build images and immutable templates

Runtime contains OpenClaw, ClawTune, and the SSH admission hook. Tool contains
the mutable workspace, native SSH server, cgroup collector, and ClawTune
eBPF/kprobe collector. Build on native ARM64 and push to a registry reachable
by CubeSandbox. The base-image construction is described in the Dockerfiles
and the Kunpeng reproduction runbook; the final Cube overlays are:

```bash
export CLAWTUNE_ROOT="$HOME/src/ClawTune"
export REGISTRY='<registry>/clawbox'
export TAG="$(git rev-parse --short HEAD)"
export KERNEL_SHA='<guest-kernel sha256 without the sha256: prefix>'

docker build --network host \
  --build-arg CUBE_GUEST_KERNEL_DIGEST="$KERNEL_SHA" \
  -f docker/Dockerfile.runtime-cube \
  -t "$REGISTRY/runtime-cube-arm64:$TAG" .
docker build --network host \
  --build-arg CUBE_GUEST_KERNEL_DIGEST="$KERNEL_SHA" \
  -f docker/Dockerfile.tool-cube \
  -t "$REGISTRY/tool-cube-arm64:$TAG" .
docker push "$REGISTRY/runtime-cube-arm64:$TAG"
docker push "$REGISTRY/tool-cube-arm64:$TAG"
docker inspect --format '{{index .RepoDigests 0}}' \
  "$REGISTRY/runtime-cube-arm64:$TAG" "$REGISTRY/tool-cube-arm64:$TAG"
```

Register fresh templates using exact image digests and the exact guest-kernel
component ID reported by CubeSandbox:

```bash
export CUBE_API_URL='http://<cube-control-host>:3000'
export CUBE_NODE='<CubeSandbox compute-node name>'
export GUEST_KERNEL_COMPONENT='<exact Cube component version>'

.venv/bin/python scripts/register-cube-template.py \
  '<runtime-image>@sha256:<digest>' \
  --alias "clawbox-runtime-$TAG" --node "$CUBE_NODE" \
  --cpu-millicores 2000 --memory-mib 2048 \
  --writable-layer-size 20G \
  --exposed-port 49983 --probe-port 49983 \
  --expected-kernel-version "$GUEST_KERNEL_COMPONENT"

.venv/bin/python scripts/register-cube-template.py \
  '<tool-image>@sha256:<digest>' \
  --alias "clawbox-tool-$TAG" --node "$CUBE_NODE" \
  --cpu-millicores 2000 --memory-mib 4096 \
  --writable-layer-size 40G \
  --exposed-port 49983 --exposed-port 2222 --probe-port 49983 \
  --expected-kernel-version "$GUEST_KERNEL_COMPONENT"
```

`runtime.memory_mib` and `sandbox.memory_mib` in the experiment describe VM
memory. Disk size is a template-time setting, so comparing disk sizes requires
different immutable template IDs; it is not an experiment YAML parameter.

## 4. Configure an installed machine

Create a machine-local environment file outside Git. It contains addresses and
paths, not policy or provider credentials:

```bash
umask 077
mkdir -p "$HOME/.config/clawbox"
cp examples/clawbox-machine.env.example "$HOME/.config/clawbox/kunpeng.env"
${EDITOR:-vi} "$HOME/.config/clawbox/kunpeng.env"
set -a
. "$HOME/.config/clawbox/kunpeng.env"
set +a
```

Run the read-only preflight:

```bash
test -c /dev/kvm
test "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs
curl -fsS "$CUBE_API_URL/health"
df -hT "$CLAWBOX_OUTPUT_ROOT"
.venv/bin/python scripts/audit-cube-sandboxes.py --json
git rev-parse HEAD
git -C "$CLAWTUNE_ROOT" rev-parse HEAD
```

The audit prints active sandboxes first and templates afterward. READY template
rows are normal. Attribute and clean any sandbox rows before a formal run.
After a reboot, revalidate control/compute health, template status/digests,
guest kernel, endpoint identity, pause/restore, and c1 before c60.

```bash
for count in 1 4 8; do
  .venv/bin/python scripts/validate-cubesandbox-tcp-endpoints.py \
    --runtime-template "$CLAWBOX_RUNTIME_TEMPLATE" \
    --tool-template "$CLAWBOX_TOOL_TEMPLATE" \
    --node "$CUBE_NODE" --control-host "$CLAWBOX_CONTROL_HOST" \
    --count "$count" \
    --output "$CLAWBOX_OUTPUT_ROOT/endpoint-c${count}.json"
done
```

Require Tool identity, stale-endpoint rejection, epoch advancement after
restore, post-restore SSH, complete cgroup/eBPF telemetry, and zero sandboxes;
an open TCP port is not sufficient.

## 5. YAML parameters and memory overcommit

Copy a checked-in experiment to a machine-local path and replace template IDs,
digests, node, traces, KB path, and memory budget. The schema rejects unknown
fields.

| Field | Meaning and guidance |
| --- | --- |
| `workload.repetitions` | Repetitions of each arm; keep equal across policies. |
| `workload.session_assignment` | `single_case` or deterministic `round_robin`; use the latter for A/B/C/A heterogeneous replay. |
| `execution.concurrency_levels` | Offered Agents, such as `[1, 5, 60]`; not a residency semaphore. |
| `arrival_schedule` | `burst` or `fixed_stagger`; prefer fixed stagger for primary results and label bursts. |
| `stagger_interval_seconds` | Positive only with `fixed_stagger`. |
| `random_seed` | Arm ordering and reproducibility seed. |
| `arm_timeout_seconds` | Whole-arm deadline. |
| `command_timeout_seconds` | Native SSH command deadline. |
| `memory_sample_interval_seconds` | Host-memory sample period; validated default is 0.2 s. |
| `stabilization_seconds` | Short pre-workload stabilization, identical across arms. |
| `runtime/sandbox.vcpu` | VM virtual CPUs. |
| `runtime/sandbox.memory_mib` | Configured VM memory and full-reservation input. |
| `pool_memory_budget_mib` | Experimental physical-memory pool; primary admission budget. |
| `emergency_free_memory_mib` | Identical host `MemAvailable` safety floor across policies. |
| `checkpoint_restore_headroom_mib` | Pool capacity retained so restore can progress. |
| `static_tool_memory_mib` | Increment reserved by `tool_static`. |
| `full_tool_memory_mib` | Tool reservation for `tool_full`, normally Tool configured memory. |
| `p90_predictions` | Immutable calibrated ClawTune KB JSON, identical hash across P90 arms. |
| `oracle_measurements` | Held-out actual demand; evaluation-only. |
| `time_scale` | Replay model-wait multiplier; use 1.0 for primary snapshot results. |
| `openclaw_exec_yield_ms` | OpenClaw foreground window; preserve capture value, range 10--120000 ms. |
| `fixed_delay_seconds` | Snapshot delay, valid only with `eviction: fixed_delay`. |
| `model_wait_prediction_seconds` | Request-time wait estimate; formal runs also need a provenance source. |
| `prefetch_lead_seconds` | Proactive Runtime-restore lead, valid only with proactive restore. |

Controlled memory overcommit is:

```text
concurrency * (runtime.memory_mib + sandbox.memory_mib)
------------------------------------------------------
               pool_memory_budget_mib
```

At c60 with 2 GiB Runtime + 4 GiB Tool and a 64 GiB pool this is 5.625x.
This is overcommit against the experimental memory scope, not necessarily
exhaustion of the entire host. Keep one hard host safety floor across policies
and never hide admission behavior behind a session cap.

## 6. Run every baseline

One YAML can list all policies. ClawBox expands each policy, concurrency, case,
and repetition into a distinct arm with the same hashed inputs:

```yaml
policies:
  - {name: lifetime-full, admission: lifetime_full, reclamation: resident, eviction: none, restore: none}
  - {name: tool-full, admission: tool_full, reclamation: resident, eviction: none, restore: none}
  - {name: tool-static, admission: tool_static, reclamation: resident, eviction: none, restore: none}
  - {name: tool-p90, admission: tool_p90, reclamation: resident, eviction: none, restore: none}
  - {name: static-snapshot, admission: tool_static, reclamation: snapshot_pause, eviction: eager, restore: reactive}
  - {name: p90-snapshot, admission: tool_p90, reclamation: snapshot_pause, eviction: eager, restore: reactive}
  - {name: p90-fixed, admission: tool_p90, reclamation: snapshot_pause, eviction: fixed_delay, restore: reactive, fixed_delay_seconds: 0.5}
  - {name: proposed, admission: tool_p90, reclamation: snapshot_pause, eviction: wait_aware_pressure, restore: proactive, prefetch_lead_seconds: 0.5}
  - {name: oracle, admission: tool_oracle, reclamation: resident, eviction: none, restore: none}
```

Their purposes, in order, are lifetime conservative, full Tool reservation,
static Tool admission, command-specific P90 admission, reclamation-only,
combined eager P90, fixed-delay decision, proposed wait-aware/proactive, and an
explicit oracle upper bound. Required resources are:

```yaml
resources:
  target_node: <compute-node>
  pool_memory_budget_mib: 65536
  emergency_free_memory_mib: 200000
  checkpoint_restore_headroom_mib: 2048
  static_tool_memory_mib: 256
  full_tool_memory_mib: 4096
  p90_predictions: /data/clawbox-kb/frozen-p90.json
  oracle_measurements: /data/clawbox-kb/held-out-oracle.json
```

Remove oracle from deployable comparisons. Use
`openclaw-cube-replay-c60-overcommit.yaml` for the validated static c60 shape;
`spatial.yaml`, `reclamation.yaml`, and `decision.yaml` are concise catalogs.
Replace their historical aliases with immutable IDs/digests for formal
OpenClaw results.

```bash
export SPEC=/data/clawbox-specs/formal.yaml
export RUN_ID="formal-$(git rev-parse --short HEAD)-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$CLAWBOX_OUTPUT_ROOT"

.venv/bin/python -m clawbox.cli experiment validate "$SPEC"
.venv/bin/python -m clawbox.cli experiment plan "$SPEC" \
  > "$CLAWBOX_OUTPUT_ROOT/$RUN_ID.plan.json"
.venv/bin/python -m clawbox.cli --output-root "$CLAWBOX_OUTPUT_ROOT" \
  experiment run "$SPEC" --run-id "$RUN_ID"
.venv/bin/python -m clawbox.cli --output-root "$CLAWBOX_OUTPUT_ROOT" \
  experiment status "$RUN_ID"
.venv/bin/python -m clawbox.cli --output-root "$CLAWBOX_OUTPUT_ROOT" \
  experiment collect "$RUN_ID" > "$CLAWBOX_OUTPUT_ROOT/$RUN_ID.collect.json"
sha256sum "$CLAWBOX_OUTPUT_ROOT/$RUN_ID/summary.json"
```

`run` is synchronous. A nonzero exit means at least one arm failed; retain the
raw bundle and use a new run ID for a rerun.

## 7. Real LLM capture and replay

For DeepSeek or another OpenAI-compatible provider, derive a c1/c2 API spec
from `examples/experiments/openclaw-cube.yaml`:

```yaml
inference:
  backend: api
  configuration:
    base_url: https://api.deepseek.com/v1
    model: deepseek-v4-flash
    api_key_env: OPENCLAW_API_KEY
    repo_fingerprint: <stable repository key>
```

Load credentials only into the Worker environment. To reuse the existing
ClawTune operator file without copying it into the repository:

```bash
IFS= read -r OPENCLAW_API_KEY < "$CLAWTUNE_ROOT/swe_rebench/llm_api_key.txt"
export OPENCLAW_API_KEY
.venv/bin/python -m clawbox.cli --output-root "$CLAWBOX_OUTPUT_ROOT" \
  experiment run /data/clawbox-specs/deepseek-c1.yaml --run-id deepseek-c1
unset OPENCLAW_API_KEY
```

Successful API sessions export exact replay files under
`$CLAWBOX_OUTPUT_ROOT/<run-id>/model-traces/<session-id>.jsonl`. Freeze
representative successful traces, record hashes, and reference them from
`workload.cases[*].replay_trace_reference`. Replay keeps Runtime/OpenClaw,
Tool commands, SSH, workspaces, admission, snapshot, memory pressure, and
telemetry real; only model generation is replayed. Use `time_scale: 1.0` for
primary snapshot comparisons. Divergence fails closed.

## 8. Train and freeze ClawTune P90

Training and comparison runs must be disjoint. First record representative
workloads with a non-P90 policy, then join ClawTune cgroup/eBPF observations
with host VM-RSS execution increments:

```bash
mkdir -p /data/clawbox-kb
.venv/bin/python scripts/train-p90-from-runs.py \
  "$CLAWBOX_OUTPUT_ROOT/training-recording" \
  --clawtune-root "$CLAWTUNE_ROOT" \
  --repository '<stable repository key>' \
  --training-set-id representative-training-v1 \
  --idle-tool-vm-rss-mib '<measured idle Tool VM RSS MiB>' \
  --idle-safety-margin-fraction 0.25 \
  --command-headroom-fraction 0.25 \
  --tool-memory-size-class-mib 1024 \
  --tool-memory-size-class-mib 2048 \
  --tool-memory-size-class-mib 4096 \
  --host-calibration-observations \
    "$CLAWBOX_OUTPUT_ROOT/host-calibration/arms/<arm-id>.json" \
  --output /data/clawbox-kb/frozen-p90.json
sha256sum /data/clawbox-kb/frozen-p90.json \
  | tee /data/clawbox-kb/frozen-p90.sha256
chmod a-w /data/clawbox-kb/frozen-p90.json
```

Reuse that exact file across all P90 arms. Report prediction source, fallback
level/rate, P90 error, and reservation accuracy. A global-default-dominated
run is not command-specific P90 evidence.

## 9. Scale, monitoring, and cleanup

Pair creation is bounded separately from Agent execution. Eight is the
validated default control-plane throttle:

```bash
export CLAWBOX_SANDBOX_CREATE_CONCURRENCY=8
```

It is not an Agent concurrency cap. Before c60, check offered memory, pool
budget, disk, safety floor, and arrival schedule. Monitor read-only:

```bash
watch -n 2 'free -h; df -h /data/cubelet /data/clawbox-results'
```

After every run:

```bash
.venv/bin/python scripts/audit-cube-sandboxes.py --json
test -f "$CLAWBOX_OUTPUT_ROOT/$RUN_ID/summary.json"
sha256sum "$CLAWBOX_OUTPUT_ROOT/$RUN_ID/summary.json"
```

Keep failed bundles as rejection evidence. Clean only sandboxes attributed to
the exact ClawBox owner/run. Do not reset a dirty remote checkout or delete
templates, unrelated sandboxes, diagnostic services, `/data/cubelet`, snapshot
storage, or result directories as generic recovery.

An admissible arm requires final validation, exact-ID Tool telemetry join 1.0,
zero telemetry loss, zero wrong/duplicate Tool execution, zero unexpected OOM,
and zero owned-sandbox leaks. Preserve provisioning, workload, cleanup, and
lifecycle timing separately with the raw bundle that regenerates the summary.
