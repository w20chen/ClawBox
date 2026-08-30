"""Paper-grade multi-workload, concurrency-sweep replay orchestration."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .study import run_study


def _cpus(value: str) -> set[int]:
    result: set[int] = set()
    for part in value.strip().split(","):
        if "-" in part:
            first, last = (int(item) for item in part.split("-", 1))
            result.update(range(first, last + 1))
        elif part:
            result.add(int(part))
    return result


def numa_topology(node: int, *, sys_root: Path = Path("/sys")) -> dict[str, Any]:
    directory = sys_root / "devices" / "system" / "node" / f"node{node}"
    cpulist = (directory / "cpulist").read_text(encoding="utf-8").strip()
    memory_kib = None
    for line in (directory / "meminfo").read_text(encoding="utf-8").splitlines():
        if "MemTotal:" in line:
            memory_kib = int(line.split()[-2])
            break
    if memory_kib is None:
        raise ValueError(f"NUMA node {node} has no MemTotal entry")
    return {"node": node, "cpulist": cpulist, "cpus": sorted(_cpus(cpulist)),
            "memory_mib": memory_kib // 1024}


def validate_numa_budget(raw: dict[str, Any], topology: dict[str, Any]) -> None:
    resources = raw.get("resources", {})
    cpu_first = int(resources.get("cpu_first", 0))
    runtime_mib = int(resources.get("runtime_memory_mib", 2048))
    tool_mib = int(resources.get("tool_memory_mib", 4096))
    reserve_mib = int(raw.get("numa_host_reserve_mib", 32768))
    levels = [int(item) for item in raw.get("concurrency_levels", [])]
    placement = str(raw.get("cpu_placement", "exclusive"))
    if placement not in {"exclusive", "round_robin"}:
        raise ValueError("cpu_placement must be exclusive or round_robin")
    if not levels or any(item < 1 for item in levels):
        raise ValueError("concurrency_levels must contain positive integers")
    node_cpus = set(topology["cpus"])
    for sessions in levels:
        assigned = (
            set(node_cpus)
            if placement == "round_robin"
            else set(range(cpu_first, cpu_first + sessions * 2))
        )
        if not assigned <= node_cpus:
            raise ValueError(
                f"concurrency {sessions} needs CPUs {cpu_first}-{cpu_first + sessions * 2 - 1}, "
                f"outside NUMA node {topology['node']} ({topology['cpulist']})"
            )
        configured_mib = sessions * (runtime_mib + tool_mib)
        available_mib = int(topology["memory_mib"]) - reserve_mib
        if configured_mib > available_mib:
            raise ValueError(
                f"concurrency {sessions} configures {configured_mib} MiB of guest memory, "
                f"above the {available_mib} MiB NUMA-local budget after host reserve"
            )


def _absolute(base: Path, value: object) -> str:
    path = Path(str(value))
    return str(path if path.is_absolute() else (base / path).resolve())


def run_suite(config_path: Path) -> int:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    base = config_path.resolve().parent
    output = Path(_absolute(base, raw["output"]))
    output.mkdir(parents=True, exist_ok=False)
    node = int(raw.get("resources", {}).get("numa_node", 0))
    topology = numa_topology(node)
    validate_numa_budget(raw, topology)
    workloads = raw.get("workloads")
    if not isinstance(workloads, list) or len(workloads) < 2:
        raise ValueError("workloads must contain at least two representative traces")

    suite_runs: list[dict[str, Any]] = []
    failed = False
    for workload in workloads:
        if not isinstance(workload, dict) or not workload.get("name"):
            raise ValueError("each workload requires a name")
        name = str(workload["name"])
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"unsafe workload name: {name}")
        for concurrency in (int(item) for item in raw["concurrency_levels"]):
            child = copy.deepcopy(raw)
            child.pop("workloads", None)
            child.pop("concurrency_levels", None)
            child.pop("numa_host_reserve_mib", None)
            child_output = output / "runs" / name / f"c{concurrency:02d}"
            source = child.setdefault("source", {})
            for field in ("runtime_rootfs", "tool_rootfs"):
                source[field] = _absolute(base, source[field])
            source["trace"] = _absolute(base, workload["trace"])
            source["prompt"] = _absolute(base, workload["prompt"])
            if not workload.get("repository"):
                raise ValueError(f"{name}: repository is required for prediction provenance")
            source["repository"] = str(workload["repository"])
            child["sessions"] = concurrency
            child.setdefault("retain_vm_artifacts", False)
            if raw.get("cpu_placement", "exclusive") == "round_robin":
                child.setdefault("resources", {})["cpu_list"] = topology["cpulist"]
            child["output"] = str(child_output)
            if workload.get("validation_command"):
                child["validation_command"] = workload["validation_command"]
            p90 = child.get("p90_static")
            if isinstance(p90, dict) and p90.get("prediction_file"):
                p90["prediction_file"] = _absolute(base, p90["prediction_file"])
            child_config = output / f"config-{name}-c{concurrency:02d}.json"
            child_config.write_text(json.dumps(child, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            status = run_study(child_config)
            summary_path = child_output / "study-summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            suite_runs.append({"workload": name, "concurrency": concurrency,
                               "status": status, "summary": str(summary_path),
                               "groups": summary["groups"],
                               "final_state_equal": summary["final_state_equal"]})
            failed = failed or status != 0

    final = {"schema_version": 1, "numa_topology": topology,
             "numa_host_reserve_mib": int(raw.get("numa_host_reserve_mib", 32768)),
             "concurrency_levels": raw["concurrency_levels"],
             "cpu_placement": raw.get("cpu_placement", "exclusive"),
             "workloads": [item["name"] for item in workloads], "runs": suite_runs,
             "all_successful": not failed}
    (output / "suite-summary.json").write_text(
        json.dumps(final, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 1 if failed else 0
