#!/usr/bin/env python3
"""Prepare and run the self-contained shared-LLC C0/C2 reproduction."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLAWBOX = ROOT / "scripts" / "clawbox"
BASE = ROOT / "examples" / "experiments" / "llc-shared-reproduction.yaml"
GENERATOR = ROOT / "scripts" / "prepare_llc_shared_experiment.py"
REPOSITORY = "tobymao/sqlglot"
BASE_COMMIT = "bc68289d4d368b29241e56b8f0aefc36db65ad47"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the ad-hoc shared-LLC C0/C2 experiment on an already installed "
            "and applied ClawBox host."
        )
    )
    parser.add_argument(
        "--profile", type=Path,
        default=Path.home() / ".config" / "clawbox" / "host.json",
        help="applied host profile (default: ~/.config/clawbox/host.json)",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        help="new directory for generated inputs; defaults below CLAWBOX_OUTPUT_ROOT",
    )
    parser.add_argument(
        "--node", action="append", default=[],
        help="compute-node ID; repeat twice (default: first two saved nodes)",
    )
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument(
        "--run-id-prefix",
        help="run ID prefix (default: llc-shared-c16-b32-UTC_TIMESTAMP)",
    )
    parser.add_argument(
        "--prepare-only", action="store_true",
        help="generate and validate inputs without qualification or VM execution",
    )
    return parser


def _run(arguments: list[str]) -> None:
    print("+ " + " ".join(arguments), flush=True)
    subprocess.run(arguments, cwd=ROOT, check=True)


def _clawbox(*arguments: str, sudo: bool = False) -> list[str]:
    command = ["bash", str(CLAWBOX)]
    if sudo:
        command.append("--sudo")
    return [*command, "experiment", *arguments]


def _load_profile(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(
            f"Applied host profile not found: {path}\n"
            "Run: bash scripts/clawbox --sudo experiment host apply HOST.yaml"
        )
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read applied host profile {path}: {exc}") from exc
    for role, expected in (("runtime", (2, 2048)), ("sandbox", (2, 4096))):
        actual = profile.get(role, {})
        shape = actual.get("vcpu"), actual.get("memory_mib")
        if shape != expected or not actual.get("template_id"):
            label = "Runtime" if role == "runtime" else "Tool"
            raise SystemExit(
                f"{label} template must be registered as {expected[0]} vCPU / "
                f"{expected[1]} MiB for this reproduction; saved profile has {shape}."
            )
    return profile


def _select_nodes(profile: dict, requested: list[str]) -> list[dict]:
    nodes = profile.get("compute_nodes") or []
    by_id = {node.get("node_id"): node for node in nodes}
    if requested:
        if len(requested) != 2 or len(set(requested)) != 2:
            raise SystemExit("--node must be supplied exactly twice with distinct IDs")
        missing = [node for node in requested if node not in by_id]
        if missing:
            raise SystemExit("Unknown compute node(s): " + ", ".join(missing))
        selected = [by_id[node] for node in requested]
    else:
        if len(nodes) < 2:
            raise SystemExit("The reproduction requires at least two saved compute nodes")
        selected = nodes[:2]
    required = {"node_id", "numa_node", "cpus", "high_watermark_mib"}
    for node in selected:
        missing = required - node.keys()
        if missing:
            raise SystemExit(
                f"Compute node {node.get('node_id', '<unknown>')} lacks: "
                + ", ".join(sorted(missing))
            )
    return selected


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


def _check_topology(nodes: list[dict]) -> dict[str, dict]:
    from clawbox.experiments.priority import physical_cluster_groups
    from clawbox.experiments.topology import parse_cpu_list

    groups = {}
    for node in nodes:
        try:
            found = physical_cluster_groups(
                node["cpus"], min_physical_cores=2, include_smt=True
            )
        except RuntimeError as exc:
            raise SystemExit(f"LLC topology check failed for {node['node_id']}: {exc}") from exc
        if len(found) < 2:
            raise SystemExit(
                f"{node['node_id']} exposes only {len(found)} eligible LLC cluster; "
                "C2 requires at least two"
            )
        selected = found[:2]
        pool: set[int] = set()
        for group in selected:
            pool.update(parse_cpu_list(group))
        groups[node["node_id"]] = {
            "eligible_count": len(found),
            "selected_groups": selected,
            "selected_cpu_pool": _format_cpu_list(pool),
        }
    return groups


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.concurrency < 2:
        raise SystemExit("--concurrency must be at least 2")
    profile_path = args.profile.expanduser().resolve()
    profile = _load_profile(profile_path)
    nodes = _select_nodes(profile, args.node)
    groups = _check_topology(nodes)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_root = Path(os.environ.get("CLAWBOX_OUTPUT_ROOT", Path.home()))
    output = (args.output_dir or output_root / f"llc-shared-reproduction-{stamp}").expanduser().resolve()
    if output.exists():
        raise SystemExit(f"Output directory already exists; choose a new path: {output}")
    output.mkdir(parents=True)
    base_spec = output / "base.yaml"
    run_prefix = args.run_id_prefix or f"llc-shared-c16-b32-{stamp}"

    assignments = [nodes[index % len(nodes)]["node_id"] for index in range(args.concurrency)]
    print(json.dumps({
        "profile": str(profile_path),
        "output_dir": str(output),
        "run_id_prefix": run_prefix,
        "concurrency": args.concurrency,
        "nodes": [{
            "node_id": node["node_id"], "numa_node": node["numa_node"],
            "saved_cpus": node["cpus"],
            "selected_cpus": groups[node["node_id"]]["selected_cpu_pool"],
            "sessions": Counter(assignments)[node["node_id"]],
            "selected_llc_clusters": list(groups[node["node_id"]]["selected_groups"]),
            "eligible_llc_cluster_count": groups[node["node_id"]]["eligible_count"],
        } for node in nodes],
    }, indent=2), flush=True)

    _run(_clawbox(
        "configure", str(BASE), str(base_spec), "--profile", str(profile_path),
        "--recipe", "custom", "--concurrency", str(args.concurrency),
        "--repetitions", "1", "--repository", REPOSITORY,
        "--base-commit", BASE_COMMIT, "--tool-workspace", "/testbed",
    ))

    generator = [
        sys.executable, str(GENERATOR), "--base-spec", str(base_spec),
        "--output-dir", str(output / "inputs"),
        "--trace-runtime-dir", (output / "inputs").as_posix(),
        "--experiment-id", run_prefix, "--repository", REPOSITORY,
        "--base-commit", BASE_COMMIT, "--concurrency", str(args.concurrency),
        "--runtime-vcpu", "2", "--runtime-memory-mib", "2048",
        "--tool-vcpu", "2", "--tool-memory-mib", "4096",
        "--threads", "2", "--working-set-mib", "2", "--steps", "300000000",
        "--heavy-phases", "12", "--late-short-phases", "8",
        "--short-seconds", "0.2", "--min-duration-seconds", "1",
        "--group-max-active", "4", "--pressure-ratio", "0.1",
        "--pressure-samples", "2", "--include-smt",
    ]
    for node in nodes:
        generator.extend(("--session-node", node["node_id"]))
        generator.extend((
            "--node-cpus",
            f"{node['node_id']}={groups[node['node_id']]['selected_cpu_pool']}",
        ))
    _run(generator)

    specs = [output / "inputs" / f"experiment-{order}.yaml"
             for order in ("c2-first", "c0-first")]
    for spec in specs:
        _run(_clawbox("validate", str(spec), "--inputs"))
    if args.prepare_only:
        print(f"Prepared and validated: {output}")
        return 0

    for order, spec in zip(("c2-first", "c0-first"), specs):
        _run(_clawbox("qualify", str(spec), "--concurrency", str(args.concurrency), sudo=True))
        run_id = f"{run_prefix}-{order}"
        _run(_clawbox("run", str(spec), "--run-id", run_id, sudo=True))
        _run(_clawbox("report", run_id))
    print(f"Completed both arm orders. Inputs and manifest: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
