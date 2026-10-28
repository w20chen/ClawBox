# Prepare workloads and Tool images

Use this page when importing recordings or preparing task-specific environments.
Return to the [experiment walkthrough](guide.md) to configure and launch them.
Commands run from the checkout with the installed Python environment active.
Paths and identifiers in capitals must be replaced with your actual inputs.

## Import replay traces

Replay preserves recorded model responses and waits while executing real tools.
Recorded tool output is comparison evidence; it is not substituted for execution.

```bash
bash scripts/clawbox experiment import-trace SOURCE.jsonl --output REPLAY.jsonl \
  --source-workspace /testbed --target-workspace /workspace
bash scripts/clawbox experiment trace REPLAY.jsonl
```

Use workspace paths matching the recording and destination Tool image. The image
must contain the recorded repository revision and dependencies. A recorded
nonzero shell result is accepted only when call identity, command, exit code,
and error match the recording. It remains a Tool failure and does not establish
task correctness. New errors, timeouts, missing comparison evidence, and failed
final validation fail the run.

For a corpus, choose the number of compatible unique traces required:

```bash
bash scripts/clawbox experiment import-corpus /absolute/path/recordings \
  --output /absolute/path/replay-corpus --count 1280
```

The importer writes relocatable paths in `workload.json`, preserves source
identities and SHA-256 hashes, and records skipped sources and reasons. It
alternates PennyLane and SQLGlot traces before appending the remaining projects
lexically. It fails if it cannot fill `--count` with compatible unique traces.
Copy the whole output directory when moving the corpus to another host.

Configure it with `--workload-manifest /absolute/path/replay-corpus/workload.json`.
This sets `workload.source: swe_rebench`, selects unique assignment, and removes
inline cases inherited from the base specification. Offered concurrency cannot
exceed the number of cases when assignment is unique.

The importer does not choose templates. Either use a Tool template containing
every selected task environment, or supply a complete `sandbox` object for each
manifest task. The object has the same fields as `environment.sandbox`: template
ID, image identity, architecture, vCPU, memory, and workspace. Disk size is fixed
when registering the template. A case-level
object replaces the entire common Tool description; it is not a partial patch.
Qualification executes a real tool call from every case and checks distinct
Tool templates on the selected compute nodes.

## Build ARM64 task images

This builder reuses the prepared `/testbed` checkout from original SWE-ReBench
images and rebuilds architecture-specific dependencies. It requires native
ARM64 Docker, registry access, a pinned SWE-bench checkout and its dependencies,
a compiled Tool Bridge, and an immutable Cube Tool base image. Use the base image
recorded in the matching host profile's `sandbox.source_image_reference`.
Build the bridge from this checkout with Go:

```bash
mkdir -p "$HOME/clawbox-build"
(cd toolbridge && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 \
  go build -mod=readonly -trimpath -o "$HOME/clawbox-build/tool-bridge" .)
```

Create a selection JSON containing the actual dataset IDs and original images:

```json
[
  {"instance_id": "DATASET_TASK_ID", "docker_image": "REGISTRY/ORIGINAL_IMAGE:TAG"}
]
```

Install the image-builder dependencies and build the selection:

```bash
python -m pip install -e '.[images]'
python scripts/build-swe-rebench-arm64.py \
  --dataset /absolute/path/filtered.parquet \
  --selection /absolute/path/selection.json --expected-count 1 \
  --swebench-root /absolute/path/SWE-bench \
  --registry REGISTRY/clawbox \
  --mapping /absolute/path/task-image-mapping.json \
  --tool-bridge-binary "$HOME/clawbox-build/tool-bridge" \
  --cube-tool-base-image 'REGISTRY/tool-base@sha256:DIGEST' \
  --workspace-root /absolute/path/build-workspaces \
  --workers 4 --min-free-gib 150 --push
```

Set `--expected-count` to the selection size. `--dataset-id` and
`--dataset-revision` can replace a local dataset. The helper checks the pinned
SWE-bench revision; use its `--help` for the supported revision and overrides.
Use `--registry-proxy URL` when registry access needs a proxy.

The builder resolves immutable source digests, verifies downloaded layers,
copies the existing checkout, and builds the shared ARM64 environment and task
layers. It does not clone or reset the task repository. The current ARM64 recipe
substitutes `rasterio 1.3.11` for `1.4.3` to match the base system's GDAL.

`--workers` bounds build pipelines; `--min-free-gib` is the free-space floor
before downloads. Temporary task workspaces are removed unless
`--keep-workspaces` is set. Repeat execution reuses completed mappings and
shared Docker layers and retries failed tasks. Changes to source images,
dependencies, the shared recipe, Tool base, bridge, or entrypoint invalidate
affected mappings. `--common-base-dockerfile` selects another shared recipe.

Register each completed immutable image using the intended Tool shape from the
host profile:

```bash
python scripts/register-task-image.py \
  --image 'REGISTRY/TASK_IMAGE@sha256:DIGEST' --alias TASK_ALIAS \
  --profile "$HOME/.config/clawbox/host.json" \
  --output /absolute/path/task-profile.json
```

Use a new output path for each registration. The command preserves the original
profile and writes the registered template under `sandbox` in the new profile.
Copy that complete object into the matching task in `workload.json`, setting
`workspace: /testbed` for these images. Repeat for each distinct task image,
then configure and validate the workload with `validate SPEC --inputs`.

## Refresh Tool collectors

When only the guest collector changes, retain task workspaces and dependency
layers. First build the shared Tool collector with the selected `CLAWTUNE_ROOT`:

```bash
bash scripts/clawbox experiment images --role sandbox \
  --profile "$HOME/.config/clawbox/host.json" --tool-prewarm enabled
```

The first build also needs `--kernel-source` and `--kernel-build` for the actual
guest kernel; later builds reuse saved settings. Prewarming compiles and attaches
BPF before template readiness without opening a command measurement window.
Validate the source collector after restore on a real VM before applying it to
the corpus. Then use its immutable image and recorded kernel identity:

```bash
python scripts/refresh-swe-collector-images.py \
  --workload /absolute/path/workload-ready.json \
  --collector-image 'REGISTRY/VALIDATED_TOOL@sha256:DIGEST' \
  --clawtune-revision GIT_SHA --node CUBE_NODE \
  --expected-kernel-version KERNEL_ID --expected-count 503 \
  --registry REGISTRY/clawbox/collector-refresh \
  --output-dir /absolute/path/collector-refresh \
  --workers 8 --batch-size 16 --minimum-free-gib 64 \
  --storage-check-path /absolute/path/cube-storage
```

Set `--expected-count` to the actual task count. Repeat `--storage-check-path`
for separate registry/artifact filesystems; each path must exist. The helper
adds collector-only layers, carries over prewarm settings, registers templates,
and writes `workload-collector-refreshed.json` and `catalog.json`. Old images and
templates are preserved. Use the generated workload in a new qualified run.

`--workers` bounds concurrent work; `--batch-size` bounds tasks retaining
intermediate build state (`0` means one batch). `--minimum-free-gib` is checked
on every selected filesystem. Optional `--prune-build-cache-between-batches`
deletes unused Docker build cache between non-final batches; use it only on a
builder whose cache may be discarded. It does not prune volumes or images.

Repeat the same command to reuse completed entries and retry failures. To replace
an unusable registered template while retaining its valid immutable image, add
`--reregister-case CASE_ID --reregister-generation GENERATION` to that command.
Repeat the case option for multiple templates and keep the generation unchanged
when resuming. A new generation registers another template. This updates the
generated workload and requires new sharing evidence and qualification.

A predictor/KB-only update uses [one Runtime rebuild](installation.md#refresh-the-runtime-predictor-and-kb)
and leaves all these Tool templates reusable.

## Prove template sharing

Template-aware placement requires physical RAM-base sharing evidence for the
exact Tool templates. Create it once and retain the raw artifacts:

```bash
bash scripts/clawbox --sudo experiment prove-template-sharing eval.yaml \
  --output /data/clawbox-evidence/template-sharing.json \
  --artifact-root /data/clawbox-evidence/template-sharing-raw
bash scripts/clawbox experiment configure eval.yaml eval-with-proof.yaml \
  --template-base-sharing-artifact /data/clawbox-evidence/template-sharing.json
```

The evidence binds template IDs, image digests, VM sizes, architecture, target
node, and raw proof hashes. New Tool templates, a changed template-RAM mapping
mechanism, or missing raw evidence require a new proof. Runtime-only and ordinary
host-predictor updates do not change Tool RAM-base sharing.

After an interrupted proof, use a new artifact directory and add
`--resume-result /absolute/path/previous-raw/result.json`. Repeat this option for
multiple retained attempts. Valid evidence and cleanup are rechecked; only
missing templates are probed again. Reusable proof skips repeated sharing probes,
not the experiment's own creation and full-load measurements.

## Cache dependency downloads

For repeated APT or pip installs, start host-local caches on an address reachable
from Tool VMs. Docker is required. The services retain data across stops:

```bash
read -r -p 'Host address reachable from Tool VMs: ' CACHE_HOST
sudo bash scripts/clawbox-package-cache start --bind-address "$CACHE_HOST" \
  --port 3142 --data-root /var/lib/clawbox/apt-cache
sudo bash scripts/clawbox-pypi-cache start --bind-address "$CACHE_HOST" \
  --port 3141 --data-root /var/lib/clawbox/pypi-cache
sudo bash scripts/clawbox-package-cache status
sudo bash scripts/clawbox-pypi-cache status
```

Replace `HOST_ADDRESS` below with that address in the experiment YAML:

```yaml
execution:
  apt_cache_proxy_url: http://HOST_ADDRESS:3142
  pypi_cache_index_url: http://HOST_ADDRESS:3141/root/pypi/+simple/
  dependency_install_concurrency: 8
```

Both URLs default to unset. APT configuration and pip index/trusted-host settings
apply only to Tool VMs; recorded commands stay unchanged. The optional install
limit counts active apt/apt-get/pip commands across the arm; it defaults to
unlimited. Other commands are unaffected. Choose the limit for your download
capacity rather than treating this example's value as a measured optimum.

Start the caches before launching. Changing these settings requires a new run
ID and qualification, without host apply, image rebuild, or retraining. Stop
them after use with `sudo bash scripts/clawbox-package-cache stop` and
`sudo bash scripts/clawbox-pypi-cache stop`; cached data and containers remain.
To restart an existing stopped cache, use `sudo docker start clawbox-apt-cache`
or `sudo docker start clawbox-pypi-cache`, then check its status. The helper's
`start` command creates a new container or accepts one already running; it does
not replace an existing stopped container or its configuration.
