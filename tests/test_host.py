from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from clawbox.experiments.host import HostConfig, apply, init_config, load_config


@pytest.mark.parametrize("values", [
    {"local_node": 1}, {"low_gib": 35, "high_gib": 32},
    {"warm_root": "/data", "cold_root": "/data/cold"},
    {"warm_root": "/data/../warm"}, {"warm_gib": -1},
    {"warm_gbi": 100}, {"local_node": "0"}, {"shared_borrow_percent": 51},
])
def test_invalid_host_plan_rejected(values):
    with pytest.raises(ValidationError):
        HostConfig(**values)


def test_host_init_never_overwrites(tmp_path):
    path = tmp_path / "host.yaml"
    init_config(path)
    before = path.read_bytes()
    assert load_config(path).local_node == 0
    with pytest.raises(FileExistsError):
        init_config(path)
    assert path.read_bytes() == before


def test_malformed_host_yaml_has_actionable_cli_error(tmp_path, capsys):
    from clawbox.cli import main
    path = tmp_path / "host.yaml"
    path.write_text("warm: [")
    assert main(["experiment", "host", "check", str(path)]) == 1
    assert "cannot read host configuration" in capsys.readouterr().err


def test_failed_preflight_cannot_mutate_host(monkeypatch, tmp_path):
    from clawbox.experiments import host
    monkeypatch.setattr(host, "check", lambda _: {"ready_for_apply": False})
    monkeypatch.setattr(host.subprocess, "run", lambda *a, **k: pytest.fail("host mutation"))
    with pytest.raises(ValueError, match="prerequisites failed"):
        apply(HostConfig(), tmp_path / "profile.json")
    assert not (tmp_path / "profile.json").exists()


@pytest.mark.parametrize("changes", [
    {"local_gib": -1}, {"warm_node": 0}, {"high_gib": 40},
    {"local_memory_cgroup": "/sys/fs/cgroup/other"},
])
def test_setup_rejects_invalid_request_before_services(monkeypatch, changes):
    from clawbox import lab
    values = HostConfig().model_dump()
    values.update(local_memory_cgroup=lab.DEFAULT_LOCAL_CGROUP, profile=Path("absent.json"))
    values.update(changes)
    monkeypatch.setattr(lab, "command", lambda *a, **k: pytest.fail("host mutation"))
    with pytest.raises(ValueError):
        lab.setup(SimpleNamespace(**values))


def test_profile_import_preserves_host_resources(tmp_path):
    import json
    original = tmp_path / "host.json"
    original.write_text(json.dumps({
        "node": "10.0.0.4", "runtime": {"template_id": "runtime"},
        "sandbox": {"template_id": "tool"}, "local_numa_node": 2,
        "warm_numa_node": 3, "local_memory_capacity_mib": 24 * 1024,
        "local_memory_low_watermark_mib": 16 * 1024,
        "local_memory_high_watermark_mib": 20 * 1024,
        "warm_capacity_mib": 32 * 1024, "shared_memory_borrow_limit_mib": 8 * 1024,
    }))
    path = tmp_path / "host.yaml"
    init_config(path, original)
    config = load_config(path)
    assert (config.local_node, config.warm_node) == (2, 3)
    assert config.shared_borrow_percent == 25
    assert config.local_gib == 24


def test_profile_import_supports_dense_host_without_warm_memory(tmp_path):
    import json
    profile = tmp_path / 'dense.json'
    profile.write_text(json.dumps({'node': 'host', 'runtime': {'template_id': 'runtime'},
        'sandbox': {'template_id': 'tool'}, 'local_memory_capacity_mib': 16*1024,
        'local_numa_node': 0, 'warm_capacity_mib': 0, 'warm_numa_node': None,
        'warm_root': None, 'shared_memory_borrow_limit_mib': None}))
    output = tmp_path / 'dense.yaml'
    init_config(output, profile)
    config = load_config(output)
    assert not config.warm and config.local_gib == 16


def test_doctor_checks_dense_compute_nodes_without_a_shared_node(tmp_path, monkeypatch):
    from clawbox import lab
    for name, value in {'memory.max': str(16*1024**2), 'memory.swap.max': '0',
                        'cpuset.mems.effective': '0-1', 'cpuset.cpus.effective': '0-3'}.items():
        (tmp_path / name).write_text(value)
    monkeypatch.setattr(lab, 'template_record', lambda _: {'ready': True})
    monkeypatch.setattr(lab, 'command', lambda *args, **kw: 'active' if args[0] == 'systemctl' else 'true')
    monkeypatch.setattr(lab, 'refresh_snapshot_storage', lambda _: True)
    failures = lab.doctor({'node': 'host', 'runtime': {'template_id': 'r'},
        'sandbox': {'template_id': 't'}, 'local_memory_cgroup': str(tmp_path),
        'local_memory_capacity_mib': 16, 'local_memory_high_watermark_mib': 12,
        'shared_memory_borrow_limit_mib': None, 'warm_numa_node': None,
        'compute_nodes': [{'node_id': 'a', 'numa_node': 0, 'cpus': '0-1'},
                          {'node_id': 'b', 'numa_node': 1, 'cpus': '2-3'}]})
    assert 'LOCAL/shared NUMA nodes' not in failures
    assert 'combined live memory.max' not in failures
