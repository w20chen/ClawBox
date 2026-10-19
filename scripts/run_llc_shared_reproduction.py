#!/usr/bin/env python3
"""Run the frozen C0/C2 experiment that measured a 5.03% mean gain."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
CLAWBOX = ROOT / "scripts" / "clawbox"
ASSETS = ROOT / "examples"
FORWARD = ASSETS / "experiments" / "llc-shared-c16-b32-20261019.yaml"
REVERSE = ASSETS / "experiments" / "llc-shared-c16-b32-reverse-20261019.yaml"
EARLY_TRACE = ASSETS / "traces" / "llc-shared-chase-early.jsonl"
LATE_TRACE = ASSETS / "traces" / "llc-shared-chase-late.jsonl"
PREDICTION = ASSETS / "predictions" / "llc-shared-prediction-progress.json"
PREDICTION_KB = ASSETS / "predictions" / "llc-shared-prediction-progress.kb.json"

RUNTIME_DIGEST = "sha256:104cf38eaf2cf470e7f84adae126c17c0cd0adf6705a0a2a582969225524984a"
TOOL_DIGEST = "sha256:2969104a6eac597504e137090e65c3df5d3e9f709bdce0c6e340ed0f7dc8cfb1"
ASSET_HASHES = {
    FORWARD: "7391701091ed4e6ac455971b1196c0499912f54de8f6b92aa295848752e41211",
    REVERSE: "53df3fab5bdd9637e856593eef6d71a42a9efacbf600556c3fb547cb5d5b255d",
    EARLY_TRACE: "698fda469572319e5895d906c82b0842e68ccbe7321474284314f3446e08e05e",
    LATE_TRACE: "4c5ee3940f3b0e8a6f6bce6c5405e4ddcc746eabf3410e549d299813416f4302",
    PREDICTION: "1ff1dd16cde3e752815966b8bb570964932014d44bfbbb55ee207e27737fe6e5",
    PREDICTION_KB: "8bab50604dc589bf1670739a6215f78de7937f9b2249f67e9a40866d5cb59027",
}
EXPECTED_NODE_MEMORY = (475136, 196608, 229376)
EXPECTED_MEMORY = {
    "local_memory_capacity_mib": 950272,
    "local_memory_low_watermark_mib": 393216,
    "local_memory_high_watermark_mib": 458752,
    "shared_memory_borrow_limit_mib": 65536,
    "warm_capacity_mib": 262144,
    "warm_numa_node": 2,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reproduce the frozen 2026-10-19 SQLGlot shared-LLC C0/C2 pair "
            "(16 sessions, 32 logical CPUs, both arm orders)."
        )
    )
    parser.add_argument(
        "--profile", type=Path,
        default=Path.home() / ".config" / "clawbox" / "host.json",
        help="applied host profile (default: ~/.config/clawbox/host.json)",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        help="new preparation directory (default: under the configured output root)",
    )
    parser.add_argument(
        "--prepare-only", action="store_true",
        help="verify assets/host and validate both generated specs without starting VMs",
    )
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_assets() -> None:
    failures = []
    for path, expected in ASSET_HASHES.items():
        if not path.is_file():
            failures.append(f"missing {path.relative_to(ROOT)}")
            continue
        actual = _sha256(path)
        if actual != expected:
            failures.append(
                f"changed {path.relative_to(ROOT)} (expected {expected}, got {actual})"
            )
    if failures:
        raise SystemExit(
            "Frozen LLC reproduction assets are incomplete or modified:\n  - "
            + "\n  - ".join(failures)
            + "\nRestore these files from the same Git commit and retry."
        )


def _load_profile(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(
            f"Applied host profile not found: {path}\n"
            "Install/configure the host using docs/installation.md, then run "
            "'bash scripts/clawbox --sudo experiment host apply HOST.yaml'."
        )
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read applied host profile {path}: {exc}") from exc

    for role, expected_shape, expected_digest in (
        ("runtime", (2, 2048), RUNTIME_DIGEST),
        ("sandbox", (2, 4096), TOOL_DIGEST),
    ):
        actual = profile.get(role) or {}
        shape = actual.get("vcpu"), actual.get("memory_mib")
        label = "Runtime" if role == "runtime" else "Tool"
        if shape != expected_shape:
            raise SystemExit(
                f"{label} must be {expected_shape[0]} vCPU / {expected_shape[1]} MiB; "
                f"the applied profile has {shape}. Re-register the frozen image/template."
            )
        if actual.get("image_digest") != expected_digest:
            raise SystemExit(
                f"{label} image digest must be {expected_digest}; the applied profile has "
                f"{actual.get('image_digest')!r}. Import/register the original image first."
            )
        if not actual.get("template_id"):
            raise SystemExit(f"{label} template_id is missing from {path}.")

    mismatches = [
        f"{key}: expected {expected!r}, got {profile.get(key)!r}"
        for key, expected in EXPECTED_MEMORY.items()
        if profile.get(key) != expected
    ]
    if mismatches:
        raise SystemExit(
            "The applied memory configuration differs from the measured experiment:\n  - "
            + "\n  - ".join(mismatches)
            + "\nApply a matching host configuration before reproducing the result."
        )
    return profile


def _format_cpu_list(cpus: set[int]) -> str:
    ordered = sorted(cpus)
    ranges: list[list[int]] = []
    for cpu in ordered:
        if not ranges or cpu != ranges[-1][1] + 1:
            ranges.append([cpu, cpu])
        else:
            ranges[-1][1] = cpu
    return ",".join(
        str(first) if first == last else f"{first}-{last}"
        for first, last in ranges
    )


def _select_cpu_nodes(profile: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, tuple[str, ...]]]:
    from clawbox.experiments.priority import physical_cluster_groups
    from clawbox.experiments.topology import parse_cpu_list

    saved_nodes = profile.get("compute_nodes") or []
    if len(saved_nodes) < 2:
        raise SystemExit("The frozen experiment requires two saved compute/NUMA nodes.")
    nodes = saved_nodes[:2]
    if len({node.get("numa_node") for node in nodes}) != 2:
        raise SystemExit("The first two saved compute nodes must use distinct NUMA nodes.")

    selected_groups: dict[str, tuple[str, ...]] = {}
    selected_nodes: list[dict[str, Any]] = []
    for node in nodes:
        node_id = node.get("node_id")
        actual_memory = (
            node.get("memory_capacity_mib"),
            node.get("low_watermark_mib"),
            node.get("high_watermark_mib"),
        )
        if actual_memory != EXPECTED_NODE_MEMORY:
            raise SystemExit(
                f"Compute node {node_id!r} memory settings must be "
                f"capacity/low/high={EXPECTED_NODE_MEMORY}; got {actual_memory}."
            )
        try:
            groups = physical_cluster_groups(
                node["cpus"], min_physical_cores=2, include_smt=True
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            raise SystemExit(f"Cannot inspect LLC topology for {node_id!r}: {exc}") from exc
        if len(groups) < 2:
            raise SystemExit(
                f"Compute node {node_id!r} exposes {len(groups)} eligible LLC cluster; "
                "the frozen experiment requires at least two."
            )
        chosen = tuple(groups[:2])
        cpus: set[int] = set()
        for group in chosen:
            cpus.update(parse_cpu_list(group))
        if len(cpus) != 16:
            raise SystemExit(
                f"The first two LLC clusters on {node_id!r} contain {len(cpus)} logical "
                "CPUs, not the required 16. Use the same 8-logical-CPU cluster topology."
            )
        selected_groups[node_id] = chosen
        selected_nodes.append({**node, "cpus": _format_cpu_list(cpus)})
    return selected_nodes, selected_groups


def _adapt_spec(source: Path, destination: Path, profile: dict[str, Any],
                nodes: list[dict[str, Any]]) -> None:
    spec = yaml.safe_load(source.read_text(encoding="utf-8"))
    environment = spec["environment"]
    for role in ("runtime", "sandbox"):
        configured = profile[role]
        target = environment[role]
        for key in ("template_id", "source_image_reference", "image_digest", "vcpu", "memory_mib"):
            target[key] = configured[key]

    resources = environment["resources"]
    resources["compute_nodes"] = nodes
    resources["target_node"] = profile.get("node")
    resources["local_memory_cgroup"] = profile.get("local_memory_cgroup")
    resources["warm_snapshot_root"] = profile.get("warm_root")
    resources["cold_snapshot_root"] = profile.get("cold_root")

    trace_paths = {"shared-early": EARLY_TRACE.resolve(), "shared-late": LATE_TRACE.resolve()}
    spec["workload"]["input"] = str(EARLY_TRACE.resolve())
    for case in spec["workload"]["cases"]:
        trace = str(trace_paths[case["case_id"]])
        case["source_reference"] = trace
        case["replay_trace_reference"] = trace
    spec["evidence"]["prediction_artifact"] = str(PREDICTION.resolve())
    destination.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")


def _command(*arguments: str, sudo: bool = False) -> list[str]:
    command = ["bash", str(CLAWBOX)]
    if sudo:
        command.append("--sudo")
    return [*command, "experiment", *arguments]


def _run(arguments: list[str]) -> None:
    print("+ " + " ".join(arguments), flush=True)
    subprocess.run(arguments, cwd=ROOT, check=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _verify_assets()
    machine = platform.machine().lower()
    if machine not in {"aarch64", "arm64"}:
        raise SystemExit(
            f"The frozen VM images and measured host are arm64; this host reports {machine!r}."
        )
    profile_path = args.profile.expanduser().resolve()
    profile = _load_profile(profile_path)
    nodes, groups = _select_cpu_nodes(profile)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_root = Path(os.environ.get("CLAWBOX_OUTPUT_ROOT", profile.get("output_root", Path.home())))
    output = (args.output_dir or output_root / f"llc-shared-exact-{stamp}").expanduser().resolve()
    if output.exists():
        raise SystemExit(f"Output directory already exists; choose a new path: {output}")
    output.mkdir(parents=True)

    specs = []
    for label, source in (("forward", FORWARD), ("reverse", REVERSE)):
        destination = output / f"experiment-{label}.yaml"
        _adapt_spec(source, destination, profile, nodes)
        specs.append((label, destination))
    shutil.copy2(PREDICTION, output / PREDICTION.name)
    shutil.copy2(PREDICTION_KB, output / PREDICTION_KB.name)

    assignments = [nodes[index % 2]["node_id"] for index in range(16)]
    manifest = {
        "experiment": "frozen 2026-10-19 shared-LLC C0/C2 reproduction",
        "expected_reference": {
            "forward_gain_percent": 4.118,
            "reverse_gain_percent": 5.955,
            "mean_gain_percent": 5.032,
        },
        "profile": str(profile_path),
        "assets": {str(path.relative_to(ROOT)): digest for path, digest in ASSET_HASHES.items()},
        "nodes": [
            {
                "node_id": node["node_id"],
                "numa_node": node["numa_node"],
                "cpus": node["cpus"],
                "llc_clusters": list(groups[node["node_id"]]),
                "sessions": Counter(assignments)[node["node_id"]],
            }
            for node in nodes
        ],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2), flush=True)

    for _, spec in specs:
        _run(_command("validate", str(spec), "--inputs"))
    if args.prepare_only:
        print(f"Prepared and validated both frozen orders: {output}")
        return 0

    run_prefix = f"llc-shared-c16-b32-{stamp}"
    for label, spec in specs:
        run_id = f"{run_prefix}-{label}"
        _run(_command(
            "launch", str(spec), "--run-id", run_id,
            "--qualification-concurrency", "16", "--force-qualify", sudo=True,
        ))
        _run(_command("report", run_id))
    print(f"Completed both frozen arm orders. Preparation manifest: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
