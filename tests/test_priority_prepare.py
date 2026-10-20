from pathlib import Path
from types import SimpleNamespace

from clawbox.experiments.priority import PriorityDomain


def test_prepare_skips_identical_cpuset_and_resets_changed_placement(tmp_path, monkeypatch):
    (tmp_path/'cgroup.procs').write_text('')
    (tmp_path/'cpuset.cpus').write_text('0-3\n')
    (tmp_path/'cpuset.mems').write_text('0\n')
    domain = SimpleNamespace(
        leaves={(0, 'tool'): tmp_path}, _vcpu_threads={},
        placements={0: ('0-3', 0)}, leaf_release_grace_seconds=0,
    )
    original = Path.write_text
    writes = []
    def capture(path, value, *args, **kwargs):
        writes.append((path.name, value))
        return original(path, value, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', capture)
    PriorityDomain.prepare(domain, 0, 'tool')
    assert writes == []
    original(tmp_path/'cpuset.mems', '2')
    PriorityDomain.prepare(domain, 0, 'tool')
    assert writes == [('cpuset.mems', '0')]
    writes.clear()
    original(tmp_path/'cpuset.cpus', '4-7')
    PriorityDomain.prepare(domain, 0, 'tool')
    assert writes == [('cpuset.cpus', '0-3')]
