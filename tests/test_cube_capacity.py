import json
import subprocess
import sys

import pytest
import yaml

from clawbox.experiments import cube_capacity as capacity
from clawbox.experiments.host import HostConfig


def node():
    return dict(InstanceType='cubebox', QuotaCpu=640000, QuotaMem=2577974,
                QuotaCpuUsage=0, QuotaMemUsage=0, MaxMvmLimit=5035, mvm_num=0)


def demand(count):
    return dict(vm_count=count*2, cpu_milli=count*4000, memory_mib=count*5120,
                max_vm_cpu_milli=2000, max_vm_memory_mib=4096)


def test_default_cube_quota_explains_480_session_ceiling():
    assert capacity.capacity_report(node(), {}, demand(479))['ready']
    assert not capacity.capacity_report(node(), {}, demand(480))['ready']
    report = capacity.capacity_report(node(), {}, demand(640))
    assert report['free_cpu_milli'] == 1920000
    assert report['required_cpu_milli'] == 2560000


def test_cube_omitempty_counters_mean_zero():
    idle = node()
    del idle['QuotaCpuUsage'], idle['QuotaMemUsage']
    assert capacity.capacity_report(idle, {}, demand(320)) == capacity.capacity_report(node(), {}, demand(320))


def test_explicit_ratios_cover_1280_without_changing_physical_capacity():
    settings = {'overcommit_ratio_conf': {'cubebox': {'cpu_ratio': 10, 'mem_ratio': 3}}}
    assert capacity.capacity_report(node(), settings, demand(1280))['ready']
    assert node()['QuotaCpu'] == 640000
    settings['overcommit_ratio_conf']['cubebox']['mem_ratio'] = 2
    assert not capacity.capacity_report(node(), settings, demand(1280))['ready']


def test_vm_count_multiplier_covers_2560_pairs_with_headroom():
    settings = {'overcommit_ratio_conf': {'cubebox': {'cpu_ratio': 20, 'mem_ratio': 6}},
                'node_max_mvm_num_conf': {'cubebox': {'mvm_num_reserve_num_percent': 1.05}}}
    report = capacity.capacity_report(node(), settings, demand(2560))
    assert report['ready']
    assert report['vm_limit'] == 5287
    assert not capacity.capacity_report(node(), settings | {
        'node_max_mvm_num_conf': {'cubebox': {'mvm_num_reserve_num_percent': 1.0}}}, demand(2560))['ready']


def test_type_override_and_current_allocations_are_honored():
    settings = {'overcommit_ratio': {'cpu_ratio': 10, 'mem_ratio': 3},
                'overcommit_ratio_conf': {'cubebox': {'cpu_ratio': 3, 'mem_ratio': 2}}}
    assert not capacity.capacity_report(node(), settings, demand(640))['ready']
    busy = node() | {'QuotaCpuUsage': 1920000}
    assert not capacity.capacity_report(busy, {}, demand(1))['ready']
    assert capacity.capacity_report(busy, {'ignore_redis_allocation': True}, demand(1280))['ready']


@pytest.mark.parametrize('ratios', [{}, {'cubebox': {'cpu_ratio': 0, 'mem_ratio': 3}},
                                  {'cubebox': {'cpu_ratio': float('inf'), 'mem_ratio': 3}}])
def test_invalid_ratios_rejected(ratios):
    with pytest.raises(ValueError):
        HostConfig(cube_scheduler_overcommit=ratios)


def test_config_update_preserves_other_settings_and_is_idempotent(tmp_path, monkeypatch):
    from clawbox.experiments import host
    path = tmp_path/'conf.yaml'
    original = {'redis': {'password': 'test-only'}, 'scheduler': {
        'filter': {'enable_filters': ['cpu','mem']},
        'overcommit_ratio_conf': {'other': {'cpu_ratio': 2, 'mem_ratio': 1}}}}
    path.write_text(yaml.safe_dump(original))
    monkeypatch.setattr(capacity, 'CONFIG', path)
    def run(*args):
        assert args[:2] == ('sudo','-n')
        return subprocess.check_output(args[2:], text=True)
    monkeypatch.setattr(host, '_run', run)
    desired = {'cubebox': {'cpu_ratio': 10, 'mem_ratio': 3}}
    assert capacity.apply_ratios(desired)
    assert not capacity.apply_ratios(desired)
    updated = yaml.safe_load(path.read_text())
    assert updated['redis'] == original['redis']
    assert updated['scheduler']['filter'] == original['scheduler']['filter']
    assert updated['scheduler']['overcommit_ratio_conf']['other'] == original['scheduler']['overcommit_ratio_conf']['other']
    assert len(list(tmp_path.glob('conf.yaml.pre-clawbox-quota-*'))) == 1
    assert 'password' not in json.dumps(capacity.scheduler_settings())


def test_vm_count_config_update_preserves_other_scheduler_fields(tmp_path, monkeypatch):
    from clawbox.experiments import host
    path = tmp_path/'conf.yaml'
    path.write_text(yaml.safe_dump({'scheduler': {'filter': {'enable_filters':['cpu']}}}))
    monkeypatch.setattr(capacity, 'CONFIG', path)
    monkeypatch.setattr(host, '_run', lambda *args: subprocess.check_output(args[2:], text=True))
    assert capacity.apply_vm_count_ratios({'cubebox':1.05})
    assert not capacity.apply_vm_count_ratios({'cubebox':1.05})
    updated=yaml.safe_load(path.read_text())
    assert updated['scheduler']['filter']=={'enable_filters':['cpu']}
    assert updated['scheduler']['node_max_mvm_num_conf']['cubebox']['mvm_num_reserve_num_percent']==1.05


def test_log_level_update_preserves_configuration_and_is_idempotent(tmp_path, monkeypatch):
    from clawbox.experiments import host
    path = tmp_path/'conf.yaml'
    original = {'log': {'level': 'info', 'path': '/data/log/CubeMaster'},
                'redis': {'password': 'test-only'}}
    path.write_text(yaml.safe_dump(original))
    monkeypatch.setattr(capacity, 'CONFIG', path)
    monkeypatch.setattr(host, '_run', lambda *args: subprocess.check_output(args[2:], text=True))
    assert capacity.apply_log_level('warn')
    assert not capacity.apply_log_level('warn')
    updated = yaml.safe_load(path.read_text())
    assert updated['log'] == {'level': 'warn', 'path': '/data/log/CubeMaster'}
    assert updated['redis'] == original['redis']
    assert len(list(tmp_path.glob('conf.yaml.pre-clawbox-log-level-*'))) == 1


def test_cube_master_log_level_is_explicit_and_validated():
    assert HostConfig().cube_master_log_level is None
    assert HostConfig(cube_master_log_level='warn').cube_master_log_level == 'warn'
    with pytest.raises(ValueError, match='cube_master_log_level'):
        HostConfig(cube_master_log_level='trace')


def test_insufficient_capacity_fails_with_remedy(monkeypatch):
    monkeypatch.setattr(capacity, 'node_capacity', lambda _: node())
    monkeypatch.setattr(capacity, 'scheduler_settings', lambda: {})
    with pytest.raises(RuntimeError, match='cube_scheduler_overcommit'):
        capacity.require_capacity({'node':'host','cube_capacity_demand':demand(640)})
