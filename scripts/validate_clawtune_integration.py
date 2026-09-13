from __future__ import annotations

import argparse
import inspect
import json
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
        sidecar_dir / "src" / "clawtune_kb" / "store.py",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if not (root / "seeds" / "bootstrap-v1").is_dir():
        missing.append(str(root / "seeds" / "bootstrap-v1"))
    if missing:
        fail("missing current source files: " + ", ".join(missing))

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

    import sys
    sys.path.insert(0, str(sidecar_dir / "src"))
    from clawtune_kb import validate_seed
    from clawtune_sidecar.trace import AgentTestBenchTraceWriter
    from tool_resource.sdk import DockerCommandObserver, DockerExecutionContext
    from clawtune_sidecar.predictors.call_load import predict_call_load
    from tool_resource.runtime_kb import ClauseResourceKB, RuntimeToolResourceKB
    sys.path.insert(0, str(project_root))
    from clawbox.experiments.prediction import clawtune_extra_peak

    example = json.loads((root / "contracts" / "examples" / "call-load.json").read_text(encoding="utf-8"))
    try:
        clawtune_extra_peak(example)
    except Exception as exc:
        fail(f"call_load.v2 extra memory target is incompatible: {exc}")
    envelope_source = (plugin_dir / "src" / "exec-instrumentation.ts").read_text(encoding="utf-8")
    if "memory_extra_peak_bytes: decision.prediction.call_prediction.targets.memory_extra_peak_bytes" not in envelope_source:
        fail("hook-only execution envelope does not carry the selected call prediction")
    validate_seed(root / "seeds" / "bootstrap-v1")
    seed_dir = root / "seeds" / "bootstrap-v1"
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
                "assets_required": args.require_assets,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
