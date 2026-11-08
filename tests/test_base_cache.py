from contextlib import nullcontext
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from clawbox.experiments import base_cache as cache
from clawbox.experiments.spec import ResourcesSpec


@pytest.mark.skipif(os.name != 'posix', reason='Linux flock and registry permissions')
def test_maintenance_uses_private_cgroup_registry_parent(tmp_path, monkeypatch):
    import stat
    registry = tmp_path / 'run' / 'clawbox' / 'base-cache.json'
    monkeypatch.setattr(cache, 'REGISTRY', registry)
    monkeypatch.setattr(cache.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(cache, '_assert_idle', lambda: None)
    with cache._maintenance():
        assert stat.S_IMODE(registry.parent.stat().st_mode) == 0o700
    with cache.experiment_lease(), pytest.raises(RuntimeError, match='cache lease'):
        with cache._maintenance():
            pass


@pytest.fixture
def prepared_env(tmp_path, monkeypatch):
    source = tmp_path / "ssd.mem"
    source.write_bytes(b"base" * 8192 + b"\0" * (2 * cache.MIB))
    warm = tmp_path / "warm"
    warm.mkdir()
    binary = tmp_path / "vmm"
    binary.write_bytes(cache.CAPABILITY)
    plan = cache.BaseCachePlan(warm_root=warm.as_posix(), numa_node=2,
                               capacity_mib=4, vmm_binary=binary.as_posix(),
                               templates={"template-a": source.as_posix()}) if os.name == "posix" else None
    if plan is None:
        # Production config accepts Linux absolute paths. Unit tests still run on Windows.
        plan = cache.BaseCachePlan.model_construct(warm_root=str(warm), numa_node=2,
                      capacity_mib=4, vmm_binary=str(binary), templates={"template-a": str(source)})
    registry = tmp_path / "registry.json"
    monkeypatch.setattr(cache, "REGISTRY", registry)
    monkeypatch.setattr(cache, "_mount", lambda _: {})
    monkeypatch.setattr(cache, "_maintenance", nullcontext)
    monkeypatch.setattr(cache.BaseCachePlan, "model_validate", lambda raw: cache.BaseCachePlan.model_construct(**raw))
    if os.name != "posix":
        monkeypatch.setattr(cache, "allocated", lambda path: path.stat().st_size)
    def preview(value):
        rows = {}
        for template, name in value.templates.items():
            info = cache.identity(Path(name))
            row = rows.setdefault((info['dev'], info['ino']), {
                "source": name, "source_identity": info, "templates": [],
                "source_allocated_bytes": cache.allocated(Path(name)),
            })
            row['templates'].append(template)
        return {'plan': value.model_dump(mode='json'), 'entries': list(rows.values())}
    monkeypatch.setattr(cache, 'preview', preview)
    return source, plan, registry


def test_prepare_reuse_and_private_cache_cleanup_preserve_source(prepared_env):
    source, plan, registry = prepared_env
    before = cache.identity(source)
    result = cache.prepare(plan, apply=True)
    target = Path(result['entries'][0]['cache'])
    assert target.read_bytes() == source.read_bytes()
    assert cache.identity(source) == before
    repeated = cache.prepare(plan, apply=True)
    assert repeated['reused'] and repeated['entries'] == result['entries']
    assert cache.status(full=True)['enabled']
    assert cache.cleanup()['removed'] is False and target.exists()
    if os.name != 'posix':
        # Windows prevents unlinking read-only files; production cleanup is Linux-only.
        target.chmod(0o644)
    assert cache.cleanup(apply=True)['removed']
    assert not registry.exists() and not target.exists()
    assert cache.identity(source) == before


def test_aliases_are_copied_and_charged_once(prepared_env):
    source, plan, _ = prepared_env
    alias = source.with_name('alias.mem')
    os.link(source, alias)
    plan = plan.model_copy(update={'templates': {**plan.templates, 'template-b': str(alias)}})
    result = cache.prepare(plan, apply=True)
    assert len(result['entries']) == 1
    assert result['entries'][0]['templates'] == ['template-a', 'template-b']
    assert result['allocated_bytes'] == cache.allocated(Path(result['entries'][0]['cache']))


def test_budget_failure_does_not_publish_or_leave_partial_pages(prepared_env):
    source, plan, registry = prepared_env
    source.write_bytes(b'x' * (2 * cache.MIB))
    plan = plan.model_copy(update={'capacity_mib': 1})
    with pytest.raises(RuntimeError, match='capacity'):
        cache.prepare(plan, apply=True)
    assert not registry.exists()
    assert list((Path(plan.warm_root) / '.clawbox-base-cache').iterdir()) == []


def test_prepare_rejects_full_warm_filesystem_without_partial_cache(prepared_env, monkeypatch):
    source, plan, registry = prepared_env
    monkeypatch.setattr(cache.os, 'statvfs', lambda _: SimpleNamespace(f_bavail=0, f_frsize=4096), raising=False)
    with pytest.raises(RuntimeError, match='insufficient free WARM'):
        cache.prepare(plan, apply=True)
    assert source.exists() and not registry.exists()
    assert not (Path(plan.warm_root) / '.clawbox-base-cache').exists()


def test_stale_source_is_rejected_not_silently_reused(prepared_env):
    source, plan, _ = prepared_env
    cache.prepare(plan, apply=True)
    source.write_bytes(b'changed')
    with pytest.raises(RuntimeError, match='identity changed'):
        cache.status()


def test_damaged_cache_is_rejected_even_with_same_length(prepared_env):
    _, plan, _ = prepared_env
    result = cache.prepare(plan, apply=True)
    target = Path(result['entries'][0]['cache'])
    target.chmod(0o644)
    with target.open('r+b') as file:
        file.write(b'bad!')
    target.chmod(0o444)
    with pytest.raises(RuntimeError, match='identity changed|byte verification failed'):
        cache.status(full=True)


def test_source_change_during_copy_aborts(prepared_env, monkeypatch):
    source, plan, registry = prepared_env
    copy = cache._copy_base
    def change(*args, **kwargs):
        result = copy(*args, **kwargs)
        source.write_bytes(b'new source')
        return result
    monkeypatch.setattr(cache, '_copy_base', change)
    with pytest.raises(RuntimeError, match='source changed'):
        cache.prepare(plan, apply=True)
    assert not registry.exists()


def test_policy_mismatch_fails_and_receipt_is_returned(prepared_env):
    _, plan, _ = prepared_env
    resources = SimpleNamespace(base_template_cache='required',
                 warm_snapshot_root=plan.warm_root, warm_numa_node=2, warm_memory_capacity_mib=4)
    with pytest.raises(RuntimeError, match='differs from host'):
        cache.check_experiment(resources)
    cache.prepare(plan, apply=True)
    assert cache.check_experiment(resources)['enabled']
    resources.base_template_cache = 'disabled'
    with pytest.raises(RuntimeError, match='differs from host'):
        cache.check_experiment(resources)


def test_missing_or_wrong_native_capability_fails(tmp_path):
    binary = tmp_path / 'vmm'
    binary.write_bytes(b'old VMM')
    with pytest.raises(RuntimeError, match='lacks'):
        cache._capable(str(binary))
    binary.write_bytes(cache.CAPABILITY)
    cache._capable(str(binary))


def test_required_cache_needs_managed_warm_pool():
    with pytest.raises(ValueError, match='base_template_cache requires'):
        ResourcesSpec(target_node='node', pool_memory_budget_mib=4096,
                      emergency_free_memory_mib=512, base_template_cache='required')


def test_preparation_materializes_zero_pages_before_vm_faults(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.write_bytes(b'\0' * (2 * cache.MIB) + b'last')
    checksum = cache._copy_base(source, target, budget=3 * cache.MIB)
    assert target.read_bytes() == source.read_bytes()
    assert cache.digest(target) == checksum
    if os.name == 'posix':
        import mmap
        before = cache.allocated(target)
        assert before >= target.stat().st_size
        with target.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_COPY) as mapping:
            for offset in range(0, len(mapping), mmap.PAGESIZE):
                _ = mapping[offset]
        assert cache.allocated(target) == before


def test_interrupted_preparation_cleanup_is_narrow_and_preserves_source(prepared_env):
    source, plan, registry = prepared_env
    attempt = Path(plan.warm_root) / '.clawbox-base-cache' / 'prepared-interrupted'
    attempt.mkdir(parents=True)
    (attempt / 'preparation.json').write_text(json.dumps({
        'kind': 'clawbox.base-cache.preparation', 'plan': plan.model_dump(mode='json')}))
    (attempt / '000000.mem').write_bytes(b'partial')
    assert cache.cleanup(plan=plan)['preparations'] == [str(attempt)]
    assert not registry.exists()
    cache.cleanup(plan=plan, apply=True)
    assert not attempt.exists() and source.exists()


def test_cleanup_refuses_unexpected_files_in_preparation(prepared_env):
    _, plan, _ = prepared_env
    attempt = Path(plan.warm_root) / '.clawbox-base-cache' / 'prepared-other'
    attempt.mkdir(parents=True)
    (attempt / 'preparation.json').write_text(json.dumps({
        'kind': 'clawbox.base-cache.preparation', 'plan': plan.model_dump(mode='json')}))
    important = attempt / 'keep.txt'
    important.write_text('unrelated')
    with pytest.raises(RuntimeError, match='unexpected file'):
        cache.cleanup(plan=plan, apply=True)
    assert important.read_text() == 'unrelated'


def test_cache_receipt_changes_qualification_binding(prepared_env):
    _, plan, _ = prepared_env
    resources = SimpleNamespace(base_template_cache='required',
        warm_snapshot_root=plan.warm_root, warm_numa_node=2, warm_memory_capacity_mib=4)
    cache.prepare(plan, apply=True)
    evidence = cache.qualification_input(resources)
    assert evidence['base_template_cache']['registry_sha256'] == cache.status()['registry_sha256']
    resources.base_template_cache = 'disabled'
    assert cache.qualification_input(resources) == {}


def test_selected_template_must_really_map_cache_during_qualification(prepared_env, monkeypatch):
    from clawbox.experiments import qualification
    _, plan, _ = prepared_env
    resources = SimpleNamespace(base_template_cache='required',
        warm_snapshot_root=plan.warm_root, warm_numa_node=2, warm_memory_capacity_mib=4)
    cache.prepare(plan, apply=True)
    monkeypatch.setattr(qualification, '_sandbox_pids', lambda _: [])
    with pytest.raises(RuntimeError, match='did not map'):
        cache.verify_created_mapping(resources, 'vm', 'template-a')
    assert not cache.verify_created_mapping(resources, 'vm', 'unselected')['required']


def test_init_resolves_catalog_and_never_overwrites(tmp_path, monkeypatch):
    import clawbox.experiments as experiments
    from clawbox.experiments.spec import ResourcesSpec
    storage = tmp_path / 'storage'
    catalog = storage / 'xfs/snapshots/tpl-example/metadata/catalog.json'
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({'snapshot_id': 'tpl-example', 'memory_kind': 'volume',
                                   'component_versions': {'cube-shim': 'test-version'},
                                   'memory_vol': 'tpl-tpl-example-memory'}))
    source = storage / 'xfs/objects/volumes/tpl-tpl-example-memory/tpl-tpl-example-memory'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'base')
    binary = tmp_path / 'vmm'
    binary.write_bytes(cache.CAPABILITY)
    resources = SimpleNamespace(warm_snapshot_root=str(tmp_path / 'warm'), warm_numa_node=2,
                                warm_memory_capacity_mib=8, cubelet_storage_root=str(storage))
    monkeypatch.setattr(experiments, 'load_experiment', lambda _: SimpleNamespace(resources=resources))
    if os.name != 'posix':
        monkeypatch.setattr(cache.BaseCachePlan, 'valid_paths', lambda self: self)
        # Construct rather than validate Linux-only paths in this isolated Windows test.
        original = cache.BaseCachePlan
        monkeypatch.setattr(cache, 'BaseCachePlan', lambda **kw: original.model_construct(**kw))
    output = tmp_path / 'plan.yaml'
    result = cache.init_plan(output, spec_path=tmp_path/'spec.yaml', templates=['tpl-example'],
                             capacity_mib=4, vmm_binary=str(binary))
    assert result['plan']['templates'] == {'tpl-example': str(source)}
    with pytest.raises(FileExistsError):
        cache.init_plan(output, spec_path=tmp_path/'spec.yaml', templates=['tpl-example'],
                        capacity_mib=4, vmm_binary=str(binary))
