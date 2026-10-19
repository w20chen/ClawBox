from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from clawbox import cli


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_llc_shared_reproduction",
    ROOT / "scripts" / "run_llc_shared_reproduction.py",
)
assert SPEC and SPEC.loader
reproduction = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reproduction)
GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "prepare_llc_shared_experiment",
    ROOT / "scripts" / "prepare_llc_shared_experiment.py",
)
assert GENERATOR_SPEC and GENERATOR_SPEC.loader
generator = importlib.util.module_from_spec(GENERATOR_SPEC)
GENERATOR_SPEC.loader.exec_module(generator)


def _profile(path: Path, *, tool_memory: int = 4096) -> Path:
    runtime_digest = "sha256:" + "a" * 64
    tool_digest = "sha256:" + "b" * 64
    value = {
        "node": "cube-node",
        "runtime": {"template_id": "runtime", "vcpu": 2, "memory_mib": 2048,
                    "source_image_reference": "registry/runtime@" + runtime_digest,
                    "image_digest": runtime_digest},
        "sandbox": {"template_id": "tool", "vcpu": 2, "memory_mib": tool_memory,
                    "source_image_reference": "registry/tool@" + tool_digest,
                    "image_digest": tool_digest},
        "local_memory_cgroup": "/sys/fs/cgroup/cube_sandbox/sandbox",
        "local_memory_capacity_mib": 294912,
        "local_memory_low_watermark_mib": 147456,
        "local_memory_high_watermark_mib": 196608,
        "shared_memory_borrow_limit_mib": None,
        "warm_capacity_mib": 0,
        "warm_root": None,
        "warm_numa_node": None,
        "cold_root": "/data/clawbox-cold",
        "compute_nodes": [
            {"node_id": "node0", "numa_node": 0, "cpus": "0-15",
             "memory_capacity_mib": 98304, "low_watermark_mib": 49152,
             "high_watermark_mib": 65536},
            {"node_id": "node1", "numa_node": 1, "cpus": "80-95",
             "memory_capacity_mib": 98304, "low_watermark_mib": 49152,
             "high_watermark_mib": 65536},
            {"node_id": "node2", "numa_node": 2, "cpus": "160-175",
             "memory_capacity_mib": 98304, "low_watermark_mib": 49152,
             "high_watermark_mib": 65536},
        ],
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_reproduction_prepares_both_orders_from_saved_host(tmp_path, monkeypatch) -> None:
    profile = _profile(tmp_path / "host.json")
    output = tmp_path / "reproduction"
    commands = []
    monkeypatch.setattr(reproduction, "_run", commands.append)
    monkeypatch.setattr(
        reproduction, "_check_topology",
        lambda nodes: {
            node["node_id"]: {
                "eligible_count": 2,
                "selected_groups": ("0-7", "8-15"),
                "selected_cpu_pool": "0-15" if node["node_id"] == "node0" else "80-95",
            }
            for node in nodes
        },
    )

    assert reproduction.main([
        "--profile", str(profile), "--output-dir", str(output), "--prepare-only",
    ]) == 0

    assert commands[0][2:4] == ["experiment", "configure"]
    generator = commands[1]
    assert generator.count("--session-node") == 2
    assert "node0=0-15" in generator
    assert "node1=80-95" in generator
    assert len([command for command in commands if "validate" in command]) == 2
    assert all("qualify" not in command and "run" not in command for command in commands)


def test_reproduction_rejects_a_different_vm_shape(tmp_path) -> None:
    profile = _profile(tmp_path / "host.json", tool_memory=8192)
    with pytest.raises(SystemExit, match="4096 MiB"):
        reproduction.main([
            "--profile", str(profile),
            "--output-dir", str(tmp_path / "reproduction"),
            "--prepare-only",
        ])


def test_reproduction_accepts_two_explicit_nodes(tmp_path) -> None:
    profile = reproduction._load_profile(_profile(tmp_path / "host.json"))
    selected = reproduction._select_nodes(profile, ["node2", "node0"])
    assert [node["node_id"] for node in selected] == ["node2", "node0"]


def test_portable_base_configures_with_an_applied_profile(tmp_path, capsys) -> None:
    profile = _profile(tmp_path / "host.json")
    output = tmp_path / "base.yaml"
    assert cli.main([
        "experiment", "configure", str(reproduction.BASE), str(output),
        "--profile", str(profile), "--recipe", "custom", "--concurrency", "16",
        "--repository", reproduction.REPOSITORY,
        "--base-commit", reproduction.BASE_COMMIT, "--tool-workspace", "/testbed",
    ]) == 0
    capsys.readouterr()
    configured = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert configured["environment"]["runtime"]["template_id"] == "runtime"
    assert configured["environment"]["sandbox"]["template_id"] == "tool"
    assert len(configured["environment"]["resources"]["compute_nodes"]) == 3

    inputs = tmp_path / "inputs"
    assert generator.main([
        "--base-spec", str(output), "--output-dir", str(inputs),
        "--trace-runtime-dir", "/data/llc-shared-reproduction",
        "--repository", reproduction.REPOSITORY,
        "--base-commit", reproduction.BASE_COMMIT,
        "--session-node", "node0", "--session-node", "node1",
        "--node-cpus", "node0=0-15", "--node-cpus", "node1=80-95",
    ]) == 0
    generated = yaml.safe_load((inputs / "experiment-c2-first.yaml").read_text())
    assert [arm["name"] for arm in generated["comparison"]["arms"]] == [
        "c2-shared-smt", "c0-shared",
    ]
