from __future__ import annotations

import argparse
import inspect
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path


LEGACY_MARKERS = (
    "packages/openclaw-plugin",
    "services/scheduler",
    "swe_rebench/.runtime/bundle",
    "agent_scheduler",
)


def fail(message: str) -> None:
    raise SystemExit(f"ClawTune integration check failed: {message}")


def revision(root: Path) -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from clawbox.clawtune_integration import source_revision
    return source_revision(root)


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Validate the current ClawTune integration")
    parser.add_argument("--clawtune-root", type=Path, default=project_root.parent / "ClawTune")
    parser.add_argument("--require-assets", action="store_true")
    args = parser.parse_args()

    root = args.clawtune_root.resolve()
    plugin_dir = root / "packages" / "clawtune-plugin"
    sidecar_dir = root / "services" / "sidecar"
    assets_dir = root / "swe_rebench" / ".runtime" / "assets"

    required = (
        plugin_dir / "package.json",
        plugin_dir / "openclaw.plugin.json",
        plugin_dir / "src" / "index.ts",
        sidecar_dir / "pyproject.toml",
        sidecar_dir / "src" / "clawtune_sidecar" / "main.py",
        sidecar_dir / "src" / "tool_resource" / "runtime_kb.py",
        root / "tools" / "guest_collector_server.py",
        sidecar_dir / "src" / "clawtune_sidecar" / "trace.py",
        sidecar_dir / "src" / "tool_resource" / "sdk.py",
        sidecar_dir / "src" / "tool_resource" / "mvdan_client.py",
        sidecar_dir / "src" / "tool_resource" / "_mvdan_adapter" / "go.mod",
        sidecar_dir / "src" / "tool_resource" / "_mvdan_adapter" / "main.go",
        sidecar_dir / "src" / "clawtune_kb" / "store.py",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        fail("missing current source files: " + ", ".join(missing))

    sys.path.insert(0, str(sidecar_dir / "src"))
    from clawtune_kb import default_seed_directory
    selected_seed = (Path(os.environ["CLAWTUNE_KB_SEED"]) if os.getenv("CLAWTUNE_KB_SEED")
                     else default_seed_directory())
    if not selected_seed.is_dir():
        fail(f"seed is missing at {selected_seed}; select a valid CLAWTUNE_KB_SEED")

    manifest = json.loads((plugin_dir / "openclaw.plugin.json").read_text(encoding="utf-8"))
    if manifest.get("id") != "clawtune":
        fail(f"plugin id changed from clawtune to {manifest.get('id')!r}")
    properties = manifest.get("configSchema", {}).get("properties", {})
    expected_config = {
        "endpoint",
        "mode",
        "failOpen",
        "executionBackend",
        "sandboxExecEnvelope",
        "sandboxExecPredictionModel",
        "enableCgroup",
        "enableAffinity",
        "enableNuma",
        "autoStartSidecar",
        "sidecarCommand",
        "securityBoundaryAccepted",
        "trace",
    }
    if missing_config := sorted(expected_config - set(properties)):
        fail("plugin config schema is missing: " + ", ".join(missing_config))

    sidecar = tomllib.loads((sidecar_dir / "pyproject.toml").read_text(encoding="utf-8"))
    if sidecar.get("project", {}).get("name") != "clawtune-sidecar":
        fail("services/sidecar is not the clawtune-sidecar package")
    scripts = sidecar.get("project", {}).get("scripts", {})
    if scripts.get("clawtune-sidecar") != "clawtune_sidecar.main:main":
        fail("clawtune-sidecar console entry point changed")

    integration_files = (
        project_root / "docker" / "Dockerfile.runtime",
        project_root / "scripts" / "runtime-entrypoint.sh",
        project_root / "scripts" / "clawtune-sidecar-entrypoint.sh",
        project_root / "docker" / "Dockerfile.runtime-cube",
    )
    for path in integration_files:
        text = path.read_text(encoding="utf-8")
        for marker in LEGACY_MARKERS:
            if marker in text:
                fail(f"legacy marker {marker!r} remains in {path.relative_to(project_root)}")

    sys.path.insert(0, str(sidecar_dir / "src"))
    from clawtune_kb import validate_seed
    from jsonschema.exceptions import ValidationError
    from clawtune_sidecar.trace import AgentTestBenchTraceWriter
    from command_parser import ParseContext, default_service
    from tool_resource.sdk import DockerCommandObserver, DockerExecutionContext
    from clawtune_sidecar.predictors.call_load import predict_call_load
    from tool_resource.runtime_kb import ClauseResourceKB, RuntimeToolResourceKB
    sys.path.insert(0, str(project_root))
    from clawbox.experiments.prediction import clawtune_extra_peak
    from clawbox.tuning.schema import PmuProfile

    load_contract = json.loads((root / "contracts" / "call-load.schema.json").read_text(encoding="utf-8"))
    backends = load_contract["$defs"]["estimate"]["properties"]["backend"]["enum"]
    if set(backends) != {"runtime", "trie", "lattice"}:
        fail("expected ToolKB, TrieKB, and LatticeKB only; update the selected "
             "ClawTune source to e14b21a or later (EdgeKappa removal)")

    example = json.loads((root / "contracts" / "examples" / "call-load.json").read_text(encoding="utf-8"))
    # The public example uses ClawTune's Docker/cgroup measurement. Cube keeps
    # the same prediction schema but supplies its explicit guest measurement.
    example["memory_measurement"] = "guest_memtotal_minus_memavailable"
    example["targets"]["memory_extra_peak_bytes"]["backend"] = "lattice"
    try:
        clawtune_extra_peak(example)
    except Exception as exc:
        fail(f"call_load.v2 extra memory target is incompatible: {exc}")
    pmu_example = json.loads(
        (root / "contracts" / "examples" / "pmu-profile.json").read_text(
            encoding="utf-8"
        )
    )
    try:
        profile = PmuProfile.model_validate(pmu_example)
    except Exception as exc:
        fail(f"pmu_profile_v1 is incompatible: {exc}")
    expected_derived = {
        "ipc", "llc_mpki", "llc_miss_rate",
        "llc_read_accesses_per_cpu_second",
        "llc_read_misses_per_cpu_second",
    }
    if set(profile.derived) != expected_derived:
        fail("current PMU derived metrics are not all accepted")
    timeout_profile = profile.model_copy(
        update={"coverage": profile.coverage.model_copy(
            update={"reason": "tool_timeout"}
        )}
    )
    PmuProfile.model_validate(timeout_profile.model_dump(by_alias=True))
    features = default_service().argv(
        ("pytest", "-q", "tests/test_example.py"),
        ParseContext(repo="clawbox/integration-check"),
    )
    if (
        features.tool != "pytest"
        or features.status != "complete"
        or "tool=pytest" not in features.core
        or not any("--quiet" in item for item in features.features)
    ):
        fail("current command parser did not project the expected pytest features")
    envelope_source = (plugin_dir / "src" / "exec-instrumentation.ts").read_text(encoding="utf-8")
    if not all(marker in envelope_source for marker in (
        'model === "lattice" ? decision?.prediction.lattice',
        "memory_measurement: tool.memory_measurement",
        "targets: tool.targets",
    )):
        fail("hook-only execution envelope does not carry the selected LatticeKB prediction")
    try:
        validate_seed(selected_seed)
    except (ValidationError, ValueError, OSError) as exc:
        fail(f"invalid seed {selected_seed}: {exc}; select the updated bootstrap-v2 "
             "or regenerate a custom seed with the selected ClawTune source")
    seed_dir = selected_seed
    ClauseResourceKB.from_json_obj(json.loads(
        (seed_dir / "clause-resource-kb.json").read_text(encoding="utf-8")
    ))
    RuntimeToolResourceKB.from_json_obj(json.loads(
        (seed_dir / "runtime-tool-resource-kb.json").read_text(encoding="utf-8")
    ))
    for method in ("record_model", "record_tool", "flush", "close"):
        if not callable(getattr(AgentTestBenchTraceWriter, method, None)):
            fail(f"trace recorder method missing: {method}")
    if "runtime_paths" not in inspect.signature(AgentTestBenchTraceWriter).parameters:
        fail("trace recorder lacks canonical runtime path routing")

    if args.require_assets:
        asset_required = (
            assets_dir / "entrypoint.sh",
            assets_dir / "plugin" / "openclaw.plugin.json",
            assets_dir / "sidecar" / "pyproject.toml",
        )
        missing_assets = [str(path) for path in asset_required if not path.is_file()]
        if missing_assets:
            fail(
                "generated assets are missing; run `python3 -m swe_rebench.runner prepare`: "
                + ", ".join(missing_assets)
            )

    print(
        json.dumps(
            {
                "status": "ok",
                "clawtune_root": str(root),
                "clawtune_revision": revision(root),
                "plugin_id": "clawtune",
                "sidecar_package": "clawtune-sidecar",
                "prediction_backends": sorted(backends),
                "assets_required": args.require_assets,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
