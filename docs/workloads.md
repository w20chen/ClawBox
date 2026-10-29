# Prepare workloads and Tool images

Use this page when importing recordings or preparing task-specific environments.
Return to the [experiment walkthrough](guide.md) to configure and launch them.
Commands run from the checkout with the installed Python environment active.
Paths and identifiers in capitals must be replaced with your actual inputs.

## Import replay traces

Replay preserves recorded model responses and waits while executing real tools.
Recorded tool output is comparison evidence; it is not substituted for execution.
Recordings with synchronous tool results run `exec` synchronously, so dependent
commands wait for installations and writes to finish. Recordings using `process`,
explicit `background`/`yieldMs`, or a recorded running-session result retain
OpenClaw background execution and the configured `openclaw_exec_yield_ms`.
Live-model execution retains its normal background behavior. This is selected
from the trace automatically; changing the trace requires a new run ID.
Replay also disables automatic transcript compaction, because generating a
summary would require an unrecorded model response. Rebuild the Runtime image
with the current image scripts to enable this behavior; use a new run ID.

Tool commands use `inference.configuration.openclaw_tool_home` (default `/root`)
to read the preserved Git and Conda configuration. Set it to the actual user's
home when using a custom Tool image; the workspace remains the recorded path.

```bash
bash scripts/clawbox experiment import-trace SOURCE.jsonl --output REPLAY.jsonl \
  --source-workspace /testbed --target-workspace /workspace
bash scripts/clawbox experiment trace REPLAY.jsonl
```

Use workspace paths matching the recording and destination Tool image. The image
must contain the recorded repository revision and dependencies. A recorded
nonzero shell result is accepted only when call identity, command, exit code,
and error match the recording. Comparison ignores CRLF versus LF, test-run
elapsed times, pytest/pluggy banner versions, pytest random seeds and plugin
ordering, native shared-library architecture suffixes, and `ls` size/date or
SELinux display markers. For `git status --short`, the generated OpenClaw shared
directory is ignored. A recording's explicit middle-truncation marker permits
additional text between matching retained ends, but not a new exception.
Test counts, diagnostic text, file paths and command outcomes still must agree.
This remains a Tool failure and does not establish task correctness. New errors,
timeouts, refused commands, transport errors, missing comparison evidence, and
failed final validation fail the run. Runtime refusals are reported separately
from executed operations; they do not acquire synthetic resource measurements.

Changed comparison rules require a new run ID for a new experiment. They do not
require host apply, image rebuild or retraining. Keep any offline reclassification
of an existing run separate from its original result. Changes to task dependencies
require rebuilding and registering affected Tool images, updating their manifest
entries, and qualifying the resulting workload before a new run.

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
copies the existing checkout and original `/root/.gitconfig`, and builds the shared ARM64 environment and task
layers. It does not clone or reset the task repository. The current ARM64 recipe
substitutes `rasterio 1.3.11` for `1.4.3` to match the base system's GDAL.
Git author identity and `safe.directory` settings come from that original
configuration. The normalized Tool image exposes a writable copy through
`GIT_CONFIG_GLOBAL`, so recorded `git config --global` commands still work as
the Tool user. Source images without that file receive no invented identity.

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
  --workers 8 --batch-size 16 --minimum-free-gib MIN_FREE_GIB \
  --storage-check-path /absolute/path/cube-storage
```

Set `--expected-count` to the actual task count. Repeat `--storage-check-path`
for separate registry/artifact filesystems; each path must exist. The helper
adds collector-only layers, carries over prewarm settings, registers templates,
and writes `workload-collector-refreshed.json` and `catalog.json`. Old images and
templates are preserved by default. Use the generated workload in a new qualified run.

When replacing a large corpus under a disk limit, add
`--retire-replaced-templates` only after testing initial replacements. It verifies
each new template is READY with the expected image, then deletes the recorded
old template through CubeSandbox's reference-aware API. Historical workload
manifests referencing deleted templates can no longer launch unchanged; Docker
images and experiment results remain. API refusal stops retirement instead of
deleting files directly. Resume with the same output directory and options.
Budget temporary space for concurrent builds and retained shared artifacts;
the free-space floor alone does not prove the entire corpus will fit.
Shared filesystem extents (reflinks) can make file-size totals overstate the
space retirement will reclaim. Check the actual `df` change after an initial
batch. Set `MIN_FREE_GIB` to preserve Cube's snapshot admission reserve plus
space for concurrent builds: a host with an 85% snapshot usage limit needs
more than 15% of that filesystem free, even if the remaining templates fit.

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

If a recorded command creates a Conda environment, an offline Tool image must
retain the packages that command needs. Build a cache overlay from versions in
the recorded package plan before registering the replacement templates:

```bash
python scripts/cache-recorded-conda-environments.py \
  --manifest /absolute/path/conda-caches.json \
  --workload /absolute/path/workload-ready.json \
  --output-dir /absolute/path/conda-overlays \
  --registry REGISTRY/clawbox/conda-overlays
```

The manifest contains a `cases` list. Each entry supplies `case_id`, a digest
pinned `source_image_reference`, `conda_specs` such as `["python=3.9.21",
"pip=25.0", "setuptools=75.8.0", "wheel=0.45.1"]`, the recorded `channels`, and
a `validation_command` that creates the environment and checks its interpreter.
Derive the versions from the recording; do not substitute current releases.
The build requires native ARM64 Docker, Conda channel access, and push access to
the chosen registry. Optional `--proxy` applies only to the build; it is empty
by default. The overlay retains the package cache, pins the supplied versions,
and sets Conda offline mode;
the recorded command still creates its own environment during replay.

The helper checks the command in a disposable Docker container with networking
disabled and writes `overlays.json`. Pass its `workload-for-registration.json`
to the collector refresh command above to register the new images. This
intermediate workload still references the previous templates and must not be
run directly. Validate the refreshed cases in real VMs, regenerate template
sharing evidence, requalify, and use a new run ID. This changes Tool images;
it requires neither host apply nor predictor retraining.

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
sandbox:
  allow_internet_access: false
execution:
  apt_cache_proxy_url: http://HOST_ADDRESS:3142
  pypi_cache_index_url: http://HOST_ADDRESS:3141/root/pypi/+simple/
  dependency_install_concurrency: 8
```

For a high-concurrency replay, increase `replay_tool_timeout_multiplier` only
when failed Tool spans show exit 124 for commands that completed in the recorded
trace. For example, `replay_tool_timeout_multiplier: 4` changes a recorded
120-second deadline to 480 seconds; commands that finish sooner have no added
delay. Changing it requires a new run ID and does not require an image rebuild.

Both URLs default to unset and apply only when
`sandbox.allow_internet_access: false`. With Tool internet enabled, ClawBox
removes its APT proxy file and uses official PyPI directly, so an unavailable
cache cannot delay or reject dependency resolution. The optional install limit
counts active apt/apt-get/pip commands across the arm; it defaults to unlimited.
Other commands are unaffected. For high-concurrency direct downloads, set a
small explicit limit such as 8 and choose it for the available outbound path
rather than treating the example as a measured optimum. Recorded commands stay
unchanged.

Start the caches before launching. Changing these settings requires a new run
ID and qualification, without host apply, image rebuild, or retraining. Stop
them after use with `sudo bash scripts/clawbox-package-cache stop` and
`sudo bash scripts/clawbox-pypi-cache stop`; cached data and containers remain.
To restart an existing stopped cache, use `sudo docker start clawbox-apt-cache`
or `sudo docker start clawbox-pypi-cache`, then check its status. The helper's
`start` command creates a new container or accepts one already running; it does
not replace an existing stopped container or its configuration.
