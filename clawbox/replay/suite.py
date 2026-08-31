"""Paper-grade multi-workload, concurrency-sweep replay orchestration."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .study import _source_hash, expand_paper_policy_matrix, run_study
from .stats import summary_stats


SUITE_METRICS = (
    "wall_s",
    "throughput_tasks_per_minute",
    "throughput_correct_tasks_per_minute",
    "correctness_pass_fraction",
    "throughput_steps_per_minute",
    "mean_firecracker_rss_bytes",
    "mean_runtime_firecracker_rss_bytes",
    "mean_tool_firecracker_rss_bytes",
    "p95_firecracker_rss_bytes",
    "peak_firecracker_rss_bytes",
    "peak_runtime_firecracker_rss_bytes",
    "peak_tool_firecracker_rss_bytes",
    "firecracker_rss_time_byte_seconds",
    "mean_numa_memory_used_bytes",
    "peak_numa_memory_used_bytes",
    "mean_numa_memory_delta_bytes",
    "peak_numa_memory_delta_bytes",
    "mean_cgroup_memory_delta_bytes",
    "peak_cgroup_memory_delta_bytes",
    "peak_resident_vms",
    "checkpoint_cycles",
    "vm_snapshot_operations",
    "vm_restore_operations",
    "checkpoint_snapshot_service_s",
    "checkpoint_restore_service_s",
    "checkpoint_reclamation_observations",
    "checkpoint_verified_firecracker_rss_released_bytes",
    "checkpoint_verified_runtime_rss_released_bytes",
    "checkpoint_verified_tool_rss_released_bytes",
    "checkpoint_cgroup_memory_released_bytes",
    "checkpoint_numa_memory_released_bytes",
    "admission_wait_s",
    "admission_acquisitions",
    "mean_admission_wait_event_s",
    "p95_admission_wait_event_s",
    "max_admission_wait_event_s",
    "mean_session_wall_s",
    "p50_session_wall_s",
    "p95_session_wall_s",
    "p99_session_wall_s",
    "snapshot_allocated_bytes",
    "tool_reservation_events",
    "mean_tool_reservation_mib",
    "max_tool_reservation_mib",
    "tool_reservation_wait_s",
    "max_tool_reservation_wait_s",
    "tool_reservation_time_mib_s",
    "tool_working_set_observations",
    "max_actual_tool_command_memory_mib",
    "mean_actual_tool_command_memory_mib",
    "max_actual_tool_working_set_mib",
    "prediction_observations",
    "prediction_memory_coverage_fraction",
    "mean_prediction_error_mib",
    "peak_tool_resident_rss_bytes",
    "peak_tool_admission_charge_bytes",
    "tool_admission_over_budget_observations",
    "tool_prediction_exceeded_leases",
    "peak_vm_resident_rss_bytes",
    "peak_vm_admission_charge_bytes",
    "vm_admission_over_budget_observations",
    "vm_materialization_admission_wait_s",
    "checkpoint_vm_cgroup_memory_released_bytes",
    "tool_balloon_verified_rss_released_bytes",
    "host_oom_kill_events",
    "oversubscription_policy_failures",
    "tenant_guest_oom_events",
    "model_gateway_http_attempts",
    "model_gateway_reconnect_attempts",
    "model_gateway_delivery_failures",
    "model_gateway_responses_delivered",
)

RATIO_EFFECT_METRICS = {
    "wall_s", "throughput_tasks_per_minute", "throughput_correct_tasks_per_minute",
    "throughput_steps_per_minute",
    "mean_firecracker_rss_bytes", "p95_firecracker_rss_bytes",
    "peak_firecracker_rss_bytes", "firecracker_rss_time_byte_seconds",
    "mean_runtime_firecracker_rss_bytes", "mean_tool_firecracker_rss_bytes",
    "peak_runtime_firecracker_rss_bytes", "peak_tool_firecracker_rss_bytes",
    "checkpoint_verified_firecracker_rss_released_bytes",
    "checkpoint_verified_runtime_rss_released_bytes",
    "checkpoint_verified_tool_rss_released_bytes",
    "mean_admission_wait_event_s", "p95_admission_wait_event_s",
    "max_admission_wait_event_s", "mean_session_wall_s",
    "p50_session_wall_s", "p95_session_wall_s", "p99_session_wall_s",
    "snapshot_allocated_bytes",
    "mean_tool_reservation_mib", "max_tool_reservation_mib",
    "tool_reservation_wait_s", "max_tool_reservation_wait_s",
    "tool_reservation_time_mib_s",
    "max_actual_tool_command_memory_mib", "mean_actual_tool_command_memory_mib",
    "max_actual_tool_working_set_mib", "mean_prediction_error_mib",
    "peak_tool_resident_rss_bytes", "peak_tool_admission_charge_bytes",
}

EXECUTION_ARTIFACTS = {
    "firecracker_binary": Path("/opt/kata/bin/firecracker"),
    "guest_kernel": Path("/opt/kata/share/kata-containers/vmlinux.container"),
}


def _baseline_label(row: dict[str, Any]) -> str:
    if row.get("label"):
        return f"{row.get('inference_backend', 'replay')}-{row['label']}"
    memory = "snapshot" if row.get("residency_policy") == "llm_wait_checkpoint" else "resident"
    return f"{row.get('inference_backend', 'replay')}-{row['sizing_policy']}-{memory}"


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
    free_kib = None
    file_pages_kib = 0
    reclaimable_kib = 0
    shmem_kib = 0
    for line in (directory / "meminfo").read_text(encoding="utf-8").splitlines():
        if "MemTotal:" in line:
            memory_kib = int(line.split()[-2])
        elif "MemFree:" in line:
            free_kib = int(line.split()[-2])
        elif "FilePages:" in line:
            file_pages_kib = int(line.split()[-2])
        elif "SReclaimable:" in line:
            reclaimable_kib = int(line.split()[-2])
        elif "Shmem:" in line:
            shmem_kib = int(line.split()[-2])
    if memory_kib is None:
        raise ValueError(f"NUMA node {node} has no MemTotal entry")
    return {"node": node, "cpulist": cpulist, "cpus": sorted(_cpus(cpulist)),
            "memory_mib": memory_kib // 1024,
            "free_memory_mib": None if free_kib is None else free_kib // 1024,
            "available_memory_mib": (
                None if free_kib is None else
                max(0, free_kib + file_pages_kib + reclaimable_kib - shmem_kib) // 1024
            )}


def _cpu_ticks(cpus: set[int], proc_stat: Path = Path("/proc/stat")) -> tuple[int, int]:
    total = idle = 0
    for line in proc_stat.read_text(encoding="ascii").splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith("cpu") or not fields[0][3:].isdigit():
            continue
        if int(fields[0][3:]) not in cpus:
            continue
        values = [int(value) for value in fields[1:]]
        total += sum(values)
        idle += sum(values[index] for index in (3, 4) if index < len(values))
    return total, idle


def _running_firecracker_pids(proc_root: Path = Path("/proc")) -> list[int]:
    result: list[int] = []
    for path in proc_root.glob("[0-9]*/comm"):
        try:
            if path.read_text(encoding="ascii").strip() == "firecracker":
                result.append(int(path.parent.name))
        except (OSError, ValueError):
            continue
    return sorted(result)


def validate_host_readiness(
    raw: dict[str, Any], topology: dict[str, Any], *, sample_seconds: float = 0.5,
) -> dict[str, Any]:
    before_total, before_idle = _cpu_ticks(set(topology["cpus"]))
    time.sleep(sample_seconds)
    after_total, after_idle = _cpu_ticks(set(topology["cpus"]))
    total_delta = after_total - before_total
    busy_fraction = (
        0.0 if total_delta <= 0
        else 1.0 - (after_idle - before_idle) / total_delta
    )
    max_busy = float(raw.get("max_numa_cpu_busy_fraction", 1.0))
    if not 0 <= max_busy <= 1:
        raise ValueError("max_numa_cpu_busy_fraction must be between 0 and 1")
    if busy_fraction > max_busy:
        raise ValueError(
            f"NUMA node {topology['node']} CPU busy fraction {busy_fraction:.3f} "
            f"exceeds clean-host limit {max_busy:.3f}"
        )
    resources = raw.get("resources", {})
    max_sessions = max(int(value) for value in raw["concurrency_levels"])
    if "paper_experiment" in raw:
        configured_mib = int(
            (raw.get("vm_pool_memory") or {}).get("hard_limit_mib", 0)
        )
    else:
        configured_mib = max_sessions * (
            int(resources.get("runtime_memory_mib", 2048))
            + int(resources.get("tool_memory_mib", 4096))
        )
    if raw.get("resident_memory_budget_mib") is not None:
        configured_mib = min(configured_mib, int(raw["resident_memory_budget_mib"]))
    required_free_mib = configured_mib + int(raw.get("numa_host_reserve_mib", 32768))
    free_mib = topology.get("free_memory_mib")
    available_mib = topology.get("available_memory_mib", free_mib)
    if available_mib is not None and int(available_mib) < required_free_mib:
        raise ValueError(
            f"NUMA node {topology['node']} has {available_mib} MiB approximately available; "
            f"at least {required_free_mib} MiB is required for the largest fixed arm"
        )
    firecracker_pids = _running_firecracker_pids()
    if bool(raw.get("require_no_firecracker", True)) and firecracker_pids:
        raise ValueError(f"pre-existing Firecracker processes would contaminate the suite: {firecracker_pids}")
    return {
        "sample_seconds": sample_seconds,
        "numa_cpu_busy_fraction": busy_fraction,
        "max_numa_cpu_busy_fraction": max_busy,
        "numa_free_memory_mib": free_mib,
        "numa_available_memory_mib": available_mib,
        "required_free_memory_mib": required_free_mib,
        "preexisting_firecracker_pids": firecracker_pids,
    }


def validate_disk_readiness(raw: dict[str, Any], filesystem_path: Path) -> dict[str, Any]:
    resources = raw.get("resources", {})
    max_sessions = max(int(value) for value in raw["concurrency_levels"])
    checkpoint_enabled = (
        any(
            arm["reclamation_policy"] in {"checkpoint", "hybrid"}
            for arm in expand_paper_policy_matrix(raw)
        ) if "paper_experiment" in raw else any(
            value in {"snapshot", "llm_wait_checkpoint"}
            for value in raw.get("memory_policies", [])
        )
    )
    pair_mib = (
        int(resources.get("runtime_memory_mib", 2048))
        + int(resources.get("tool_memory_mib", 4096))
    )
    resident_budget = raw.get("resident_memory_budget_mib")
    if checkpoint_enabled and resident_budget is not None:
        resident_slots = min(max_sessions, int(resident_budget) // pair_mib)
        # Every evicted session owns one snapshot generation. Resident restored
        # sessions may still map the prior generation while writing the next.
        snapshot_generations = max_sessions + resident_slots
    else:
        snapshot_generations = max_sessions * 2 if checkpoint_enabled else 0
    checkpoint_scope = str(
        (raw.get("reclamation") or {}).get("checkpoint_scope", "pair")
    )
    if checkpoint_scope not in {"pair", "tool"}:
        raise ValueError("reclamation.checkpoint_scope must be pair or tool")
    snapshot_unit_mib = (
        int(resources.get("tool_memory_mib", 4096))
        if "paper_experiment" in raw and checkpoint_scope == "tool"
        else pair_mib
    )
    snapshot_mib = snapshot_generations * snapshot_unit_mib
    reserve_mib = int(raw.get("snapshot_disk_reserve_mib", 32768))
    required_mib = snapshot_mib + reserve_mib
    available_mib = shutil.disk_usage(filesystem_path).free // (1024 * 1024)
    if available_mib < required_mib:
        raise ValueError(
            f"snapshot filesystem has {available_mib} MiB free; at least {required_mib} MiB "
            "is required for the bounded concurrent snapshot generations plus reserve"
        )
    return {
        "filesystem_path": str(filesystem_path),
        "available_mib": available_mib,
        "required_mib": required_mib,
        "snapshot_generation_bound": snapshot_generations,
        "resident_memory_budget_mib": resident_budget,
        "reserve_mib": reserve_mib,
    }


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
        if "paper_experiment" in raw:
            if tool_mib != 4096:
                raise ValueError("paper experiments require fixed 4096 MiB Tool VMs")
            configured_mib = int(
                (raw.get("vm_pool_memory") or {}).get("hard_limit_mib", 0)
            )
        resident_budget = raw.get("resident_memory_budget_mib")
        if resident_budget is not None:
            if int(resident_budget) < runtime_mib + tool_mib:
                raise ValueError("resident_memory_budget_mib cannot admit one VM pair")
            configured_mib = min(configured_mib, int(resident_budget))
        available_mib = int(topology["memory_mib"]) - reserve_mib
        if configured_mib > available_mib:
            raise ValueError(
                f"concurrency {sessions} configures {configured_mib} MiB of guest memory, "
                f"above the {available_mib} MiB NUMA-local budget after host reserve"
            )


def _absolute(base: Path, value: object) -> str:
    path = Path(str(value))
    return str(path if path.is_absolute() else (base / path).resolve())


def validate_tool_pool_memory(raw: dict[str, Any], base: Path) -> dict[str, Any] | None:
    if "paper_experiment" not in raw:
        return None
    memory = raw.get("tool_pool_memory")
    if not isinstance(memory, dict):
        raise ValueError("tool_pool_memory configuration is required")
    hard = int(memory.get("hard_limit_mib", 0))
    high = int(memory.get("high_watermark_mib", 0))
    low = int(memory.get("low_watermark_mib", 0))
    headroom = int(memory.get("headroom_mib", 0))
    if not 0 < low < high < hard:
        raise ValueError("Tool pool watermarks must satisfy 0 < Wlow < Whigh < H")
    if headroom < 0 or headroom >= high:
        raise ValueError("Tool admission headroom must be non-negative and below Whigh")
    if not memory.get("cgroup"):
        raise ValueError("tool_pool_memory.cgroup is required")
    path = Path(_absolute(base, memory.get("cgroup")))
    if not (path / "cgroup.procs").is_file():
        raise ValueError(f"Tool pool is not a cgroup v2 directory: {path}")
    value = (path / "memory.max").read_text(encoding="ascii").strip()
    expected = hard * 1024 * 1024
    if value == "max" or int(value) != expected:
        raise ValueError(f"Tool pool memory.max must equal {expected} bytes")
    return {
        "cgroup": str(path), "hard_limit_mib": hard,
        "high_watermark_mib": high, "low_watermark_mib": low,
        "headroom_mib": headroom,
    }


def validate_vm_pool_memory(
    raw: dict[str, Any],
    base: Path,
    tool_pool_memory: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if "paper_experiment" not in raw:
        return None
    memory = raw.get("vm_pool_memory")
    if not isinstance(memory, dict):
        raise ValueError("vm_pool_memory configuration is required")
    hard = int(memory.get("hard_limit_mib", 0))
    high = int(memory.get("high_watermark_mib", 0))
    low = int(memory.get("low_watermark_mib", 0))
    headroom = int(memory.get("headroom_mib", 0))
    if not 0 < low < high < hard:
        raise ValueError("VM pool watermarks must satisfy 0 < Wlow < Whigh < H")
    if headroom < 0 or headroom >= high:
        raise ValueError("VM-pool headroom must be non-negative and below Whigh")
    if not memory.get("cgroup") or not memory.get("runtime_cgroup"):
        raise ValueError("vm_pool_memory requires cgroup and runtime_cgroup")
    path = Path(_absolute(base, memory["cgroup"]))
    runtime_path = Path(_absolute(base, memory["runtime_cgroup"]))
    for label, candidate in (("VM pool", path), ("Runtime pool", runtime_path)):
        if not (candidate / "cgroup.procs").is_file():
            raise ValueError(f"{label} is not a cgroup v2 directory: {candidate}")
    expected = hard * 1024 * 1024
    value = (path / "memory.max").read_text(encoding="ascii").strip()
    if value == "max" or int(value) != expected:
        raise ValueError(f"VM pool memory.max must equal {expected} bytes")
    if runtime_path.parent != path:
        raise ValueError("Runtime pool cgroup must be a direct child of the VM pool")
    if tool_pool_memory is None or Path(tool_pool_memory["cgroup"]).parent != path:
        raise ValueError("Tool pool cgroup must be a direct child of the VM pool")
    if Path(tool_pool_memory["cgroup"]) == runtime_path:
        raise ValueError("Runtime and Tool pools must be distinct")
    initial_runtime = int(memory.get("initial_runtime_rss_mib", 256))
    initial_tool = int(memory.get("initial_tool_rss_mib", 256))
    restore_headroom = int(memory.get("restore_transient_headroom_mib", 256))
    if initial_runtime <= 0 or initial_tool <= 0:
        raise ValueError("initial VM RSS reservations must be positive")
    if restore_headroom < 0:
        raise ValueError("restore transient headroom must be non-negative")
    if tool_pool_memory is None or hard <= int(tool_pool_memory["hard_limit_mib"]):
        raise ValueError("VM-pool hard limit must exceed the Tool-pool hard limit")
    if initial_tool + int(tool_pool_memory["headroom_mib"]) > int(
        tool_pool_memory["high_watermark_mib"]
    ):
        raise ValueError("initial Tool RSS reservation does not fit Tool Whigh")
    if max(initial_runtime, initial_tool) + headroom > high:
        raise ValueError("initial VM RSS reservation does not fit VM Whigh")
    return {
        "cgroup": str(path),
        "runtime_cgroup": str(runtime_path),
        "hard_limit_mib": hard,
        "high_watermark_mib": high,
        "low_watermark_mib": low,
        "headroom_mib": headroom,
        "initial_runtime_rss_mib": initial_runtime,
        "initial_tool_rss_mib": initial_tool,
        "restore_transient_headroom_mib": restore_headroom,
    }


def _existing_ancestor(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise FileNotFoundError(f"no existing ancestor for {path}")
        candidate = parent
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _process_placement() -> dict[str, Any]:
    status: dict[str, str] = {}
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith(("Cpus_allowed_list:", "Mems_allowed_list:")):
                key, value = line.split(":", 1)
                status[key.lower()] = value.strip()
    except OSError:
        pass
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="ascii").strip().splitlines()
    except OSError:
        cgroup = []
    affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    numa_policy = None
    if shutil.which("numactl"):
        completed = subprocess.run(
            ["numactl", "--show"], check=False, capture_output=True, text=True,
        )
        numa_policy = completed.stdout.strip()
    return {
        "cpu_affinity": affinity, "status": status, "cgroup_membership": cgroup,
        "numactl_show": numa_policy,
    }


def _suite_identity(
    *, config_path: Path, raw: dict[str, Any], topology: dict[str, Any],
    artifact_provenance: dict[str, Any], prediction_provenance: dict[str, Any],
) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    def read_optional(path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None

    def command_output(argv: list[str]) -> str | None:
        try:
            completed = subprocess.run(argv, check=False, capture_output=True, text=True)
        except OSError:
            return None
        output = (completed.stdout + completed.stderr).strip()
        return output or None

    first_cpu = int(topology["cpus"][0])
    runtime_rootfs = artifact_provenance["source"]["runtime_rootfs"]["path"]
    host_environment = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "kernel_cmdline": read_optional("/proc/cmdline"),
        "os_release": read_optional("/etc/os-release"),
        "cpuinfo": read_optional("/proc/cpuinfo"),
        "scaling_governor": read_optional(
            f"/sys/devices/system/cpu/cpu{first_cpu}/cpufreq/scaling_governor"
        ),
        "scaling_driver": read_optional(
            f"/sys/devices/system/cpu/cpu{first_cpu}/cpufreq/scaling_driver"
        ),
        "firecracker_version": command_output(["/opt/kata/bin/firecracker", "--version"]),
        "filesystem": command_output(["findmnt", "-T", runtime_rootfs, "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"]),
        "python_packages": command_output([sys.executable, "-m", "pip", "freeze", "--all"]),
    }
    return {
        "schema_version": 1,
        "config_sha256": _sha256(config_path),
        "config": raw,
        "repository_commit": commit,
        "source_tree_sha256": _source_hash(root),
        "host": platform.node(),
        "machine": platform.machine(),
        "numa_topology": {
            key: topology.get(key) for key in ("node", "cpulist", "cpus", "memory_mib")
        },
        "process_placement": _process_placement(),
        "host_environment": host_environment,
        "artifact_provenance": artifact_provenance,
        "prediction_provenance": prediction_provenance,
    }


def _validate_resumed_study(
    summary: dict[str, Any], child: dict[str, Any], identity: dict[str, Any],
) -> None:
    manifest = summary.get("manifest") or {}
    if manifest.get("commit") != identity["repository_commit"]:
        raise ValueError("resumed child repository commit does not match suite manifest")
    if manifest.get("source_tree_sha256") != identity["source_tree_sha256"]:
        raise ValueError("resumed child source tree does not match suite manifest")
    if manifest.get("config") != child:
        raise ValueError("resumed child embedded configuration does not match")
    expected_runs = (
        int(child.get("repetitions", 1))
        * len(child.get("inference_backends", ["replay"]))
        * (
            len(expand_paper_policy_matrix(child))
            if "paper_experiment" in child else (
                len(child.get("memory_policies", ["resident", "snapshot"]))
                * (
                    len(child.get("sizing_policies", ["fixed"]))
                    + (1 if child.get("fixed_control_tool_memory_mib") is not None else 0)
                )
            )
        )
    )
    runs = summary.get("runs") or []
    if len(runs) != expected_runs:
        raise ValueError(
            f"resumed child has {len(runs)} arm repetitions; expected {expected_runs}"
        )
    expected_sessions = int(child["sessions"])
    if any(int(row.get("sessions_requested", -1)) != expected_sessions for row in runs):
        raise ValueError("resumed child has an unexpected session count")
    if not bool(summary.get("final_state_equal")):
        raise ValueError("resumed child failed final-state equivalence")
    if any(
        int(row.get("sessions_completed", -1)) != expected_sessions
        or bool(row.get("failures"))
        for row in runs
    ):
        raise ValueError("resumed child contains a failed or incomplete arm")


def _macro_statistics(
    suite_runs: list[dict[str, Any]], concurrency_levels: list[int],
) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for concurrency in concurrency_levels:
        baselines = sorted({
            baseline for run in suite_runs
            if run["concurrency"] == concurrency
            and int(run.get("status", 1)) == 0
            and bool(run.get("final_state_equal"))
            for baseline in run["groups"]
        })
        for baseline in baselines:
            key = f"c{concurrency:02d}/{baseline}"
            result[key] = {}
            for metric in SUITE_METRICS:
                trajectory_rows = [
                    {
                        "trajectory": run["workload"],
                        "independent_unit": run["independent_unit"],
                        "mean": float(
                            run["groups"][baseline]["statistics"][metric]["mean"]
                        ),
                    }
                    for run in suite_runs
                    if run["concurrency"] == concurrency
                    and int(run.get("status", 1)) == 0
                    and bool(run.get("final_state_equal"))
                    and baseline in run["groups"]
                    and run["groups"][baseline].get("statistics", {}).get(metric, {}).get("mean") is not None
                ]
                units = sorted({row["independent_unit"] for row in trajectory_rows})
                unit_means = [
                    {
                        "independent_unit": unit,
                        "trajectory_count": sum(
                            row["independent_unit"] == unit for row in trajectory_rows
                        ),
                        "mean": statistics.fmean(
                            row["mean"] for row in trajectory_rows
                            if row["independent_unit"] == unit
                        ),
                    }
                    for unit in units
                ]
                result[key][metric] = {
                    **summary_stats(row["mean"] for row in unit_means),
                    "sampling_unit": "independent_unit",
                    "trajectory_count": len(trajectory_rows),
                    "trajectory_means": trajectory_rows,
                    "independent_unit_means": unit_means,
                }
    return result


def _paired_contrasts(measurement_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Paired arm effects; inference uses task IDs rather than trajectories/steps."""
    indexed: dict[tuple[Any, ...], dict[tuple[str, str], dict[str, Any]]] = {}
    for row in measurement_rows:
        if (
            int(row.get("failure_count", 0)) != 0
            or int(row.get("sessions_completed", -1))
            != int(row.get("sessions_requested", -2))
            or not bool(row.get("block_final_state_equal", False))
        ):
            continue
        key = (
            row["workload"], row["independent_unit"], int(row["concurrency"]),
            int(row["repetition"]), row.get("inference_backend", "replay"),
        )
        arm_key = (row["sizing_policy"], row["residency_policy"])
        arms = indexed.setdefault(key, {})
        if arm_key in arms:
            raise ValueError(f"duplicate paired arm for {key}: {arm_key}")
        arms[arm_key] = row

    observations: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def add(label: str, metric: str, key: tuple[Any, ...], treatment: float, control: float,
            *, percent: float | None = None) -> None:
        label = f"c{int(key[2]):02d}/{label}"
        effect = treatment - control
        if percent is None and metric in RATIO_EFFECT_METRICS and treatment > 0 and control > 0:
            percent = 100.0 * effect / control
        row = {
            "trajectory": key[0], "independent_unit": key[1],
            "concurrency": key[2], "repetition": key[3], "inference_backend": key[4],
            "effect": effect, "percent_effect": percent,
            "treatment": treatment, "control": control,
        }
        observations.setdefault(label, {}).setdefault(metric, []).append(row)

    for key, arms in indexed.items():
        predictive = (
            "p90_reservation"
            if any(size == "p90_reservation" for size, _residency in arms)
            else "p90_static"
        )
        for residency in ("resident", "llm_wait_checkpoint"):
            for treatment_sizing, control_sizing, label in (
                (predictive, "fixed", f"p90_reservation_vs_fixed/{residency}"),
                ("fixed2", "fixed", f"fixed2_vs_fixed/{residency}"),
                (predictive, "fixed2", f"p90_reservation_vs_fixed2/{residency}"),
            ):
                treatment_row = arms.get((treatment_sizing, residency))
                control_row = arms.get((control_sizing, residency))
                if treatment_row is None or control_row is None:
                    continue
                for metric in SUITE_METRICS:
                    if treatment_row.get(metric) is not None and control_row.get(metric) is not None:
                        add(label, metric, key, float(treatment_row[metric]), float(control_row[metric]))
        for sizing in ("fixed", "fixed2", predictive):
            treatment_row = arms.get((sizing, "llm_wait_checkpoint"))
            control_row = arms.get((sizing, "resident"))
            if treatment_row is None or control_row is None:
                continue
            for metric in SUITE_METRICS:
                if treatment_row.get(metric) is not None and control_row.get(metric) is not None:
                    add(
                        f"checkpoint_vs_resident/{sizing}", metric, key,
                        float(treatment_row[metric]), float(control_row[metric]),
                    )
        for treatment_sizing, control_sizing, label in (
            (predictive, "fixed", "interaction_checkpoint_x_p90_reservation_vs_fixed"),
            ("fixed2", "fixed", "interaction_checkpoint_x_fixed2_vs_fixed"),
        ):
            rows = (
                arms.get((treatment_sizing, "llm_wait_checkpoint")),
                arms.get((treatment_sizing, "resident")),
                arms.get((control_sizing, "llm_wait_checkpoint")),
                arms.get((control_sizing, "resident")),
            )
            if any(row is None for row in rows):
                continue
            treatment_checkpoint, treatment_resident, control_checkpoint, control_resident = rows
            assert all(row is not None for row in rows)
            for metric in SUITE_METRICS:
                values = [row.get(metric) for row in rows]
                if any(value is None for value in values):
                    continue
                tc, tr, cc, cr = (float(value) for value in values)
                percent = None
                if metric in RATIO_EFFECT_METRICS and min(tc, tr, cc, cr) > 0:
                    percent = 100.0 * ((tc / tr) / (cc / cr) - 1.0)
                add(label, metric, key, tc - tr, cc - cr, percent=percent)

    aggregated: dict[str, Any] = {}
    for label, metrics in observations.items():
        aggregated[label] = {}
        for metric, rows in metrics.items():
            units = sorted({row["independent_unit"] for row in rows})
            unit_effects = [
                {
                    "independent_unit": unit,
                    "pair_count": sum(row["independent_unit"] == unit for row in rows),
                    "mean_effect": statistics.fmean(
                        row["effect"] for row in rows if row["independent_unit"] == unit
                    ),
                    "mean_percent_effect": statistics.fmean(
                        row["percent_effect"] for row in rows
                        if row["independent_unit"] == unit and row["percent_effect"] is not None
                    ) if any(
                        row["independent_unit"] == unit and row["percent_effect"] is not None
                        for row in rows
                    ) else None,
                }
                for unit in units
            ]
            aggregated[label][metric] = {
                **summary_stats(row["mean_effect"] for row in unit_effects),
                "percent_effect_statistics": summary_stats(
                    row["mean_percent_effect"] for row in unit_effects
                    if row["mean_percent_effect"] is not None
                ),
                "sampling_unit": "independent_unit",
                "pair_count": len(rows),
                "independent_unit_effects": unit_effects,
                "pairs": rows,
            }
    return aggregated


def _prediction_provenance(
    prediction_path: Path,
    *,
    workload_name: str,
    trace_path: Path,
    require_held_out: bool,
) -> dict[str, Any]:
    payload = json.loads(prediction_path.read_text(encoding="utf-8"))
    evaluation = payload.get("evaluation")
    if require_held_out:
        if not isinstance(evaluation, dict):
            raise ValueError(f"{workload_name}: prediction has no held-out evaluation provenance")
        protocol = evaluation.get("protocol")
        if protocol == "leave-one-recording-out":
            if evaluation.get("held_out_workload") != workload_name:
                raise ValueError(f"{workload_name}: prediction was prepared for another workload")
            if evaluation.get("held_out_trace_sha256") != _sha256(trace_path):
                raise ValueError(f"{workload_name}: prediction held-out trace digest does not match")
            held_out_run = str(evaluation.get("held_out_run_id") or "")
            training_runs = {
                str(item.get("run_id"))
                for item in (payload.get("training") or {}).get("runs", [])
                if isinstance(item, dict)
            }
            if not held_out_run or any(run.endswith(f"/{held_out_run}") for run in training_runs):
                raise ValueError(f"{workload_name}: held-out run appears in prediction training data")
        elif protocol == "independent-recording-set":
            trace_entry = (evaluation.get("traces") or {}).get(workload_name)
            if not isinstance(trace_entry, dict):
                raise ValueError(f"{workload_name}: prediction has no evaluation trace entry")
            if trace_entry.get("sha256") != _sha256(trace_path):
                raise ValueError(f"{workload_name}: prediction evaluation trace digest does not match")
            training_set = (payload.get("training") or {}).get("recording_set_id")
            if not training_set or training_set == evaluation.get("recording_set_id"):
                raise ValueError(f"{workload_name}: training and evaluation recording sets overlap")
        else:
            raise ValueError(f"{workload_name}: prediction has unsupported evaluation protocol")
    return {
        "path": str(prediction_path),
        "sha256": _sha256(prediction_path),
        "source_digest": payload.get("source_digest"),
        "pair_digest": payload.get("pair_digest"),
        "artifact_count": payload.get("artifact_count"),
        "training": payload.get("training"),
        "evaluation": evaluation,
    }


def _preflight_suite(
    config_path: Path,
) -> tuple[
    dict[str, Any], Path, dict[str, Any], dict[str, Any],
    list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any],
]:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    base = config_path.resolve().parent
    tool_pool_memory = validate_tool_pool_memory(raw, base)
    vm_pool_memory = validate_vm_pool_memory(raw, base, tool_pool_memory)
    node = int(raw.get("resources", {}).get("numa_node", 0))
    topology = numa_topology(node)
    validate_numa_budget(raw, topology)
    host_state = validate_host_readiness(raw, topology)
    host_state["tool_pool_memory"] = tool_pool_memory
    host_state["vm_pool_memory"] = vm_pool_memory
    parent_placement = _process_placement()
    host_state["parent_process_placement"] = parent_placement
    if bool(raw.get("require_parent_numa_binding", False)):
        affinity = set(parent_placement.get("cpu_affinity") or [])
        if not affinity or not affinity <= set(topology["cpus"]):
            raise ValueError(
                "suite parent is not CPU-bound to the selected NUMA node; launch through numactl"
            )
        policy = str(parent_placement.get("numactl_show") or "")
        expected_lines = {f"cpubind: {node}", f"nodebind: {node}", f"membind: {node}"}
        actual_lines = {line.strip() for line in policy.splitlines()}
        if not expected_lines <= actual_lines:
            raise ValueError(
                "suite parent is not memory-bound to the selected NUMA node; launch through numactl"
            )
    workloads = raw.get("workloads")
    if not isinstance(workloads, list) or len(workloads) < 2:
        raise ValueError("workloads must contain at least two representative traces")
    source = raw.get("source")
    if not isinstance(source, dict):
        raise ValueError("source configuration is required")
    if not str(source.get("tool_repository_commit") or "").strip():
        raise ValueError("source.tool_repository_commit is required for workload provenance")
    for field in ("runtime_rootfs", "tool_rootfs"):
        path = Path(_absolute(base, source.get(field)))
        if not path.is_file():
            raise FileNotFoundError(path)
    host_state["snapshot_disk"] = validate_disk_readiness(
        raw, _existing_ancestor(Path(_absolute(base, raw["output"])))
    )
    artifact_provenance: dict[str, Any] = {
        "source": {
            field: {
                "path": _absolute(base, source[field]),
                "size_bytes": Path(_absolute(base, source[field])).stat().st_size,
                "sha256": _sha256(Path(_absolute(base, source[field]))),
            }
            for field in ("runtime_rootfs", "tool_rootfs")
        },
        "execution": {
            name: {
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for name, path in EXECUTION_ARTIFACTS.items()
            if path.is_file()
        },
        "tool_repository_commit": source.get("tool_repository_commit"),
        "workloads": {},
    }
    missing_execution_artifacts = [
        str(path) for path in EXECUTION_ARTIFACTS.values() if not path.is_file()
    ]
    if missing_execution_artifacts:
        raise FileNotFoundError(
            "required execution artifacts are missing: "
            + ", ".join(missing_execution_artifacts)
        )
    predictions: dict[str, dict[str, Any]] = {}
    paper_arms = expand_paper_policy_matrix(raw) if "paper_experiment" in raw else []
    paper_admission = {arm["admission_policy"] for arm in paper_arms}
    for workload in workloads:
        if not isinstance(workload, dict) or not workload.get("name"):
            raise ValueError("each workload requires a name")
        name = str(workload["name"])
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"unsafe workload name: {name}")
        if not workload.get("repository"):
            raise ValueError(f"{name}: repository is required for prediction provenance")
        if not str(workload.get("independent_unit") or "").strip():
            raise ValueError(
                f"{name}: independent_unit is required (use the SWE task ID, not the trace name)"
            )
        for field in ("trace", "prompt"):
            path = Path(_absolute(base, workload.get(field)))
            if not path.is_file():
                raise FileNotFoundError(path)
        artifact_provenance["workloads"][name] = {
            field: {
                "path": _absolute(base, workload[field]),
                "size_bytes": Path(_absolute(base, workload[field])).stat().st_size,
                "sha256": _sha256(Path(_absolute(base, workload[field]))),
            }
            for field in ("trace", "prompt")
        }
        artifact_provenance["workloads"][name]["independent_unit"] = str(
            workload["independent_unit"]
        )
        if "paper_experiment" in raw and paper_admission.intersection({"p90", "oracle"}):
            provenance: dict[str, Any] = {}
            experiment = raw["paper_experiment"]
            for policy, field, workload_field in (
                ("p90", "p90_plan", "prediction_file"),
                ("oracle", "oracle_plan", "oracle_file"),
            ):
                if policy not in paper_admission:
                    continue
                configured = workload.get(workload_field) or experiment.get(field)
                if not configured:
                    raise ValueError(f"{name}: {workload_field} is required for {policy} admission")
                plan_path = Path(_absolute(base, configured))
                provenance[policy] = _prediction_provenance(
                    plan_path, workload_name=name,
                    trace_path=Path(_absolute(base, workload["trace"])),
                    require_held_out=bool(raw.get("require_held_out_predictions", False)),
                )
            predictions[name] = provenance
        elif {"p90_static", "p90_reservation"}.intersection(raw.get("sizing_policies", [])):
            p90 = raw.get("p90_reservation") or raw.get("p90_static")
            if not isinstance(p90, dict):
                raise ValueError("p90_reservation configuration is required")
            configured = workload.get("prediction_file") or p90.get("prediction_file")
            if not configured:
                raise ValueError(f"{name}: prediction_file is required")
            prediction_path = Path(_absolute(base, configured))
            predictions[name] = _prediction_provenance(
                prediction_path,
                workload_name=name,
                trace_path=Path(_absolute(base, workload["trace"])),
                require_held_out=bool(raw.get("require_held_out_predictions", False)),
            )
    return raw, base, topology, host_state, workloads, predictions, artifact_provenance


def validate_suite_config(config_path: Path) -> dict[str, Any]:
    """Fail-closed validation that performs no experiment or output writes."""
    raw, _base, topology, host_state, workloads, predictions, artifacts = _preflight_suite(config_path)
    return {
        "valid": True,
        "numa_topology": topology,
        "host_preflight": host_state,
        "concurrency_levels": raw["concurrency_levels"],
        "cpu_placement": raw.get("cpu_placement", "exclusive"),
        "workloads": [str(item["name"]) for item in workloads],
        "independent_units": sorted({str(item["independent_unit"]) for item in workloads}),
        "prediction_provenance": predictions,
        "artifact_provenance": artifacts,
    }


def run_suite(config_path: Path) -> int:
    (raw, base, topology, host_state, workloads, prediction_provenance,
     artifact_provenance) = _preflight_suite(config_path)
    output = Path(_absolute(base, raw["output"]))
    resume = bool(raw.get("resume", False))
    output_existed = output.exists()
    output.mkdir(parents=True, exist_ok=resume)
    identity = _suite_identity(
        config_path=config_path.resolve(), raw=raw, topology=topology,
        artifact_provenance=artifact_provenance,
        prediction_provenance=prediction_provenance,
    )
    manifest_path = output / "suite-manifest.json"
    frozen_config_path = output / "suite-config.json"
    if output_existed:
        if not manifest_path.is_file() or not frozen_config_path.is_file():
            raise ValueError("resume output is missing its immutable suite manifest/config")
        prior_identity = json.loads(manifest_path.read_text(encoding="utf-8"))
        if prior_identity != identity:
            raise ValueError("suite identity changed; refusing to mix resumed measurements")
        if json.loads(frozen_config_path.read_text(encoding="utf-8")) != raw:
            raise ValueError("frozen suite configuration changed")
    else:
        manifest_path.write_text(
            json.dumps(identity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        frozen_config_path.write_text(
            json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    suite_runs: list[dict[str, Any]] = []
    measurement_rows: list[dict[str, Any]] = []
    failed = False
    stopped_early = False
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
            child.pop("require_held_out_predictions", None)
            child.pop("continue_after_block_failure", None)
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
            child["seed"] = int.from_bytes(hashlib.sha256(
                f"{int(raw.get('seed', 0))}:{name}:{concurrency}".encode()
            ).digest()[:4], "big")
            child.setdefault("retain_vm_artifacts", False)
            if raw.get("cpu_placement", "exclusive") == "round_robin":
                child.setdefault("resources", {})["cpu_list"] = topology["cpulist"]
            child["output"] = str(child_output)
            if workload.get("validation_command"):
                child["validation_command"] = workload["validation_command"]
            if "paper_experiment" in child:
                paper = child["paper_experiment"]
                paper["workload_name"] = name
                selected_admission = {
                    arm["admission_policy"] for arm in expand_paper_policy_matrix(child)
                }
                for policy, field, workload_field in (
                    ("p90", "p90_plan", "prediction_file"),
                    ("oracle", "oracle_plan", "oracle_file"),
                ):
                    if policy not in selected_admission:
                        continue
                    configured = workload.get(workload_field) or paper.get(field)
                    paper[field] = _absolute(base, configured)
                    if _sha256(Path(paper[field])) != prediction_provenance[name][policy]["sha256"]:
                        raise ValueError(f"{name}: {policy} plan changed while the suite was running")
                memory = child.get("tool_pool_memory") or {}
                if memory.get("cgroup"):
                    memory["cgroup"] = _absolute(base, memory["cgroup"])
                vm_memory = child.get("vm_pool_memory") or {}
                for field in ("cgroup", "runtime_cgroup"):
                    if vm_memory.get(field):
                        vm_memory[field] = _absolute(base, vm_memory[field])
            p90 = child.get("p90_reservation") or child.get("p90_static")
            if "paper_experiment" not in child and isinstance(p90, dict):
                p90["workload_name"] = name
                configured_prediction = workload.get("prediction_file") or p90.get("prediction_file")
                if configured_prediction:
                    p90["prediction_file"] = _absolute(base, configured_prediction)
                    if _sha256(Path(p90["prediction_file"])) != prediction_provenance[name]["sha256"]:
                        raise ValueError(f"{name}: prediction changed while the suite was running")
            child_config = output / f"config-{name}-c{concurrency:02d}.json"
            rendered_config = json.dumps(child, indent=2, sort_keys=True) + "\n"
            if child_config.exists() and child_config.read_text(encoding="utf-8") != rendered_config:
                raise ValueError(f"{name} c{concurrency}: generated config changed since the prior run")
            child_config.write_text(rendered_config, encoding="utf-8")
            summary_path = child_output / "study-summary.json"
            resumed = resume and summary_path.is_file()
            if resumed:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                _validate_resumed_study(summary, child, identity)
                block_preflight = None
                status = 0 if (
                    summary.get("final_state_equal")
                    and all(not row.get("failures") for row in summary.get("runs", []))
                ) else 1
            else:
                if child_output.exists():
                    raise ValueError(
                        f"{name} c{concurrency}: partial output exists without study-summary.json"
                    )
                current_topology = numa_topology(int(topology["node"]))
                block_preflight = validate_host_readiness(raw, current_topology)
                block_preflight["snapshot_disk"] = validate_disk_readiness(
                    raw, _existing_ancestor(child_output)
                )
                status = run_study(child_config)
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            for arm in summary.get("runs", []):
                sessions = arm.get("sessions", [])
                measurement_rows.append({
                    "workload": name,
                    "independent_unit": str(workload["independent_unit"]),
                    "concurrency": concurrency,
                    "seed": child["seed"],
                    "baseline": _baseline_label(arm),
                    "repetition": arm.get("repetition"),
                    "arm_order_position": arm.get("arm_order_position"),
                    "inference_backend": arm.get("inference_backend", "replay"),
                    "sizing_policy": arm.get("admission_policy", arm.get("sizing_policy")),
                    "admission_policy": arm.get("admission_policy"),
                    "residency_policy": arm.get("reclamation_policy", arm.get("residency_policy")),
                    "reclamation_policy": arm.get("reclamation_policy"),
                    "decision_policy": arm.get("decision_policy"),
                    "restore_policy": arm.get("restore_policy"),
                    "checkpoint_scope": arm.get("checkpoint_scope"),
                    "configured_tool_memory_mib": arm.get("configured_tool_memory_mib"),
                    "sessions_requested": arm.get("sessions_requested"),
                    "sessions_completed": arm.get("sessions_completed"),
                    "failure_count": len(arm.get("failures", [])),
                    "block_final_state_equal": bool(summary.get("final_state_equal")),
                    "model_steps_completed": arm.get("model_steps_completed"),
                    "replay_requests_input_validated": arm.get("replay_requests_input_validated"),
                    "replay_requests_input_unvalidated": arm.get("replay_requests_input_unvalidated"),
                    **{metric: arm.get(metric) for metric in SUITE_METRICS},
                    "checkpoint_cycles": sum(
                        int(item.get("checkpoint_cycles", item.get("snapshots", 0)))
                        for item in sessions
                    ),
                    "vm_snapshot_operations": sum(
                        int(item.get("vm_snapshot_operations", 2 * int(item.get("snapshots", 0))))
                        for item in sessions
                    ),
                    "vm_restore_operations": sum(
                        int(item.get("vm_restore_operations", 2 * int(item.get("snapshots", 0))))
                        for item in sessions
                    ),
                    "snapshot_s": sum(float(item.get("snapshot_s", 0.0)) for item in sessions),
                    "restore_s": sum(float(item.get("restore_s", 0.0)) for item in sessions),
                })
            suite_runs.append({"workload": name,
                               "independent_unit": str(workload["independent_unit"]),
                               "concurrency": concurrency,
                               "seed": child["seed"], "resumed": resumed,
                               "host_preflight": block_preflight,
                               "status": status, "summary": str(summary_path),
                               "prediction": prediction_provenance.get(name),
                               "groups": summary["groups"],
                               "final_state_equal": summary["final_state_equal"]})
            failed = failed or status != 0
            if status != 0 and not bool(raw.get("continue_after_block_failure", False)):
                stopped_early = True
                break
        if stopped_early:
            break

    measurement_path = output / "measurements.csv"
    if measurement_rows:
        with measurement_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(measurement_rows[0]))
            writer.writeheader()
            writer.writerows(measurement_rows)
    macro_statistics = _macro_statistics(
        suite_runs, [int(item) for item in raw["concurrency_levels"]]
    )

    final = {"schema_version": 2, "numa_topology": topology,
             "suite_manifest": str(manifest_path),
             "host_preflight": host_state,
             "numa_host_reserve_mib": int(raw.get("numa_host_reserve_mib", 32768)),
             "concurrency_levels": raw["concurrency_levels"],
             "cpu_placement": raw.get("cpu_placement", "exclusive"),
             "require_held_out_predictions": bool(raw.get("require_held_out_predictions", False)),
             "resume_enabled": resume,
             "prediction_provenance": prediction_provenance,
             "artifact_provenance": artifact_provenance,
             "measurement_csv": str(measurement_path),
             "macro_statistics": macro_statistics,
             "paired_contrasts": _paired_contrasts(measurement_rows),
             "workloads": [item["name"] for item in workloads],
             "independent_units": sorted({str(item["independent_unit"]) for item in workloads}),
             "runs": suite_runs,
             "stopped_early": stopped_early,
             "continue_after_block_failure": bool(
                 raw.get("continue_after_block_failure", False)
             ),
             "all_successful": not failed}
    (output / "suite-summary.json").write_text(
        json.dumps(final, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 1 if failed else 0
