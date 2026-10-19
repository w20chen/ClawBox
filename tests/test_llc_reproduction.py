from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
MODULE_SPEC = importlib.util.spec_from_file_location(
    "run_llc_shared_reproduction",
    ROOT / "scripts" / "run_llc_shared_reproduction.py",
)
assert MODULE_SPEC and MODULE_SPEC.loader
reproduction = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(reproduction)


def _profile(path: Path, *, tool_digest: str = reproduction.TOOL_DIGEST) -> Path:
    value = {
        "node": "cube-node",
        "runtime": {
            "template_id": "runtime-template",
            "vcpu": 2,
            "memory_mib": 2048,
            "source_image_reference": "registry/runtime@" + reproduction.RUNTIME_DIGEST,
            "image_digest": reproduction.RUNTIME_DIGEST,
        },
        "sandbox": {
            "template_id": "tool-template",
            "vcpu": 2,
            "memory_mib": 4096,
            "source_image_reference": "registry/tool@" + tool_digest,
            "image_digest": tool_digest,
        },
        "local_memory_cgroup": "/sys/fs/cgroup/cube_sandbox/sandbox",
        "local_memory_capacity_mib": 950272,
        "local_memory_low_watermark_mib": 393216,
        "local_memory_high_watermark_mib": 458752,
        "shared_memory_borrow_limit_mib": 65536,
        "warm_capacity_mib": 262144,
        "warm_root": "/mnt/clawbox-pool",
        "warm_numa_node": 2,
        "cold_root": "/data/clawbox-cold",
        "output_root": str(path.parent / "results"),
        "compute_nodes": [
            {
                "node_id": "node0", "numa_node": 0, "cpus": "0-79",
                "memory_capacity_mib": 475136,
                "low_watermark_mib": 196608,
                "high_watermark_mib": 229376,
            },
            {
                "node_id": "node1", "numa_node": 1, "cpus": "80-159",
                "memory_capacity_mib": 475136,
                "low_watermark_mib": 196608,
                "high_watermark_mib": 229376,
            },
        ],
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _topology(cpus: str, **_: object) -> tuple[str, ...]:
    return ("0-7", "8-15") if cpus == "0-79" else ("80-87", "88-95")


def test_frozen_asset_hashes_match() -> None:
    reproduction._verify_assets()


def test_one_command_prepares_exact_pair(tmp_path, monkeypatch) -> None:
    profile = _profile(tmp_path / "host.json")
    output = tmp_path / "prepared"
    commands: list[list[str]] = []
    monkeypatch.setattr(reproduction.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr("clawbox.experiments.priority.physical_cluster_groups", _topology)
    monkeypatch.setattr(reproduction, "_run", commands.append)

    assert reproduction.main([
        "--profile", str(profile),
        "--output-dir", str(output),
        "--prepare-only",
    ]) == 0

    assert len(commands) == 2
    assert all(command[2:4] == ["experiment", "validate"] for command in commands)
    forward = yaml.safe_load((output / "experiment-forward.yaml").read_text())
    reverse = yaml.safe_load((output / "experiment-reverse.yaml").read_text())
    assert forward["execution"]["randomized_order"] is True
    assert reverse["execution"]["randomized_order"] is False
    assert forward["execution"]["concurrency_levels"] == [16]
    assert [node["cpus"] for node in forward["environment"]["resources"]["compute_nodes"]] == [
        "0-15", "80-95",
    ]
    assert forward["environment"]["runtime"]["template_id"] == "runtime-template"
    assert forward["environment"]["sandbox"]["template_id"] == "tool-template"
    assert forward["mechanisms"]["placement"]["tool_call_cpu_policy"] == "c2"
    assert [arm["name"] for arm in forward["comparison"]["arms"]] == [
        "c0-shared-c16", "c2-shared-c16",
    ]
    assert Path(forward["evidence"]["prediction_artifact"]) == reproduction.PREDICTION.resolve()
    assert Path(forward["workload"]["cases"][0]["source_reference"]) == reproduction.EARLY_TRACE.resolve()
    assert (output / "manifest.json").is_file()


def test_execution_launches_both_orders_and_reports(tmp_path, monkeypatch) -> None:
    profile = _profile(tmp_path / "host.json")
    commands: list[list[str]] = []
    monkeypatch.setattr(reproduction.platform, "machine", lambda: "arm64")
    monkeypatch.setattr("clawbox.experiments.priority.physical_cluster_groups", _topology)
    monkeypatch.setattr(reproduction, "_run", commands.append)

    assert reproduction.main([
        "--profile", str(profile),
        "--output-dir", str(tmp_path / "prepared"),
    ]) == 0

    launches = [command for command in commands if "launch" in command]
    reports = [command for command in commands if "report" in command]
    assert len(launches) == 2
    assert len(reports) == 2
    assert all("--force-qualify" in command for command in launches)
    assert all("--qualification-concurrency" in command for command in launches)


def test_rejects_nonoriginal_tool_image(tmp_path) -> None:
    profile = _profile(tmp_path / "host.json", tool_digest="sha256:" + "0" * 64)
    with pytest.raises(SystemExit, match="Tool image digest must be"):
        reproduction._load_profile(profile)
