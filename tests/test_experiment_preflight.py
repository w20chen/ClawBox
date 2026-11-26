import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from clawbox import cli
from clawbox.experiments import preflight, host, qualification
from clawbox.experiments.configuration import load_configuration
from clawbox.experiments.prepare import prepare
from test_unified_configuration import documents


def inputs(tmp_path):
    b, w = documents()
    bp, wp, mp = (tmp_path / n for n in ('b.yaml', 'w.yaml', 'machine.env'))
    bp.write_text(yaml.safe_dump(b))
    wp.write_text(yaml.safe_dump(w))
    mp.write_text('# selected environment\n')
    return bp, wp, mp


def test_prepare_preserves_policy_templates_and_submission(tmp_path, monkeypatch):
    bp, wp, mp = inputs(tmp_path)
    before = load_configuration(bp, wp)
    target = tmp_path / 'new run'
    report = prepare(target, baseline=bp, workload=wp, machine_env=mp, output_root=tmp_path/'results')
    monkeypatch.chdir(target)
    assert load_configuration(target/'baseline.yaml', target/'workload.yaml') == before
    assert report['bindings']['machine_env'] == str(mp.resolve())
    assert not (target/'machine.env').exists()
    with pytest.raises(ValueError, match='already exists'):
        prepare(target, baseline=bp, workload=wp, machine_env=mp, output_root=tmp_path)


def test_prepare_missing_bindings_leaves_no_directory(tmp_path):
    bp, wp, _ = inputs(tmp_path)
    with pytest.raises(ValueError, match='machine_env'):
        prepare(tmp_path/'new', baseline=bp, workload=wp, machine_env=tmp_path/'missing', output_root=tmp_path)
    assert not (tmp_path/'new').exists()


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux fresh-shell wrapper')
def test_prepared_wrapper_in_fresh_shell_from_other_directory(tmp_path):
    bp, wp, mp = inputs(tmp_path)
    mp.write_text('CLAWBOX_PYTHON=' + sys.executable + '\n')
    target = tmp_path/'prepared'
    prepare(target, baseline=bp, workload=wp, machine_env=mp, output_root=tmp_path/'results')
    for _ in range(2):
        result = subprocess.run(['bash', str(target/'experiment.sh'), 'describe', '--json'], cwd='/',
                                env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path)}, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['experiment_id'] == load_configuration(bp, wp).experiment_id


def test_tap_count_excludes_owned_and_duplicate_entries(monkeypatch):
    taps = [dict(tapName='one', state='Ready'), dict(tapName='one', state='Ready'),
            dict(tapName='two', state='Ready', ownerSandboxID='owner'),
            dict(tapName='three', state='Ready', sandboxID='vm'),
            dict(tapName='four', state='Ready', networkHandle='handle'),
            dict(tapName='five', state='Preparing'), dict(tapName='six', state='Ready')]
    monkeypatch.setattr(preflight.urllib.request, 'build_opener', lambda *_: SimpleNamespace(
        open=lambda *a, **k: io.StringIO(json.dumps(dict(taps=taps)))))
    assert preflight.tap_inventory('unused') == dict(total=7, ready_unowned=2)


def test_actual_process_limit_is_read(monkeypatch):
    import psutil
    monkeypatch.setattr(preflight, 'command', lambda *_: '42')
    monkeypatch.setattr(psutil, 'RLIMIT_NOFILE', 7, raising=False)
    monkeypatch.setattr(psutil, 'Process', lambda pid: SimpleNamespace(rlimit=lambda _: (1024, 524288)))
    assert preflight.process_nofile('cube-api') == dict(pid=42, soft=1024, hard=524288)


def test_aggregate_reports_input_host_and_explicit_receipt_failures(monkeypatch, tmp_path):
    from clawbox.experiments import inputs as input_module
    monkeypatch.setattr(preflight, 'prerequisites', lambda *a, **k: dict(checks=[], ready=True))
    def fail(*a, **k):
        raise ValueError('mismatch')
    monkeypatch.setattr(input_module, 'validate_launch_inputs', fail)
    monkeypatch.setattr(qualification, 'require_host_ready', fail)
    monkeypatch.setattr(qualification, 'validate_receipt', fail)
    report = preflight.check(None, output_root=tmp_path, qualification=tmp_path/'receipt')
    assert not report['ready']
    assert len(report['checks']) == 3
    assert all(not c['ok'] and c['remedy'] for c in report['checks'])


def test_cli_preflight_nonzero_json(monkeypatch, tmp_path, capsys):
    bp, wp, _ = inputs(tmp_path)
    monkeypatch.setattr(preflight, 'check', lambda *a, **k: dict(ready=False, checks=[]))
    assert cli.main(['experiment', 'preflight', '--baseline', str(bp), '--workload', str(wp), '--json']) == 2
    assert json.loads(capsys.readouterr().out)['ready'] is False


def test_nofile_idempotent_preserves_existing_higher_limit(monkeypatch):
    monkeypatch.setattr(preflight, 'process_nofile', lambda _: dict(soft=65536, hard=524288))
    commands = []
    monkeypatch.setattr(host, '_run', lambda *args: commands.append(args) or '65536')
    assert not host.apply_cube_api_nofile(host.HostConfig(cube_api_nofile=65536))
    assert len(commands) == 1
    assert commands[0][0] == 'systemctl'


def test_nofile_reuses_existing_helper_and_verifies_live_limit(monkeypatch):
    limits = iter([dict(soft=1024, hard=524288), dict(soft=65536, hard=524288)])
    monkeypatch.setattr(preflight, 'process_nofile', lambda _: next(limits))
    calls = []
    def run(*args):
        calls.append(args)
        if args[0] == 'systemctl':
            return '1024' if 'LimitNOFILESoft' in args else '524288'
        return ''
    monkeypatch.setattr(host, '_run', run)
    assert host.apply_cube_api_nofile(host.HostConfig(cube_api_nofile=65536))
    assert calls[-1][-3:] == ('--minimum', '65536', '--apply')
    assert calls[-1][3].endswith('configure-cube-api-nofile.sh')


def test_recover_preview_and_failure_do_not_apply(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, 'idle_domain', lambda _: dict(idle=True))
    monkeypatch.setattr(host, 'apply', lambda *a: pytest.fail('must not apply'))
    def fail(*a, **k):
        raise subprocess.CalledProcessError(1, 'recover')
    monkeypatch.setattr(host.subprocess, 'run', fail)
    assert not host.recover(host.HostConfig(), tmp_path/'profile')['apply']
    with pytest.raises(RuntimeError, match='host recovery failed'):
        host.recover(host.HostConfig(), tmp_path/'profile', execute=True)


def test_apply_and_recover_reject_held_empty_domain_before_mutation(monkeypatch, tmp_path):
    def busy(_):
        raise RuntimeError('cgroup lease is held')
    monkeypatch.setattr(preflight, 'idle_domain', busy)
    monkeypatch.setattr(host, 'check', lambda *_: pytest.fail('must reject first'))
    monkeypatch.setattr(host.subprocess, 'run', lambda *a, **k: pytest.fail('must not start services'))
    for action in (host.apply, host.recover):
        with pytest.raises(RuntimeError, match='lease is held'):
            action(host.HostConfig(), tmp_path/'profile')


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux cgroup lease')
def test_idle_domain_rejects_real_flock_with_empty_cgroup(monkeypatch, tmp_path):
    import fcntl
    import hashlib
    domain = tmp_path/'domain'
    domain.mkdir()
    (domain/'cgroup.events').write_text('populated 0\nfrozen 0\n')
    locks = tmp_path/'locks'
    locks.mkdir()
    monkeypatch.setattr(preflight, 'Path', lambda p: locks if p == '/run/clawbox/cgroups' else Path(p))
    monkeypatch.setattr(preflight, 'active_experiments', lambda: [dict(run_id='other-run')])
    lock = locks/(hashlib.sha256(str(domain).encode()).hexdigest()+'.lock')
    with lock.open('w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match='other-run'):
            preflight.idle_domain(str(domain))
    assert preflight.idle_domain(str(domain))['idle']
    assert lock.exists()  # Never unlink a lease another process may reuse.


def test_owner_report_omits_this_command_and_sudo_parent(monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, 'Process', lambda: SimpleNamespace(parents=lambda: [SimpleNamespace(pid=10)]))
    entries = [SimpleNamespace(pid=p, info=dict(cmdline=['python', '-m', 'clawbox.cli', '--run-id', r]))
               for p, r in [(10, 'this-run'), (os.getpid(), 'this-run'), (99, 'other-run')]]
    monkeypatch.setattr(psutil, 'process_iter', lambda *_: entries)
    assert preflight.active_experiments() == [dict(pid=99, run_id='other-run', output_root=None)]


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux flock and proc')
def test_simulator_ports_reserved_ephemeral_and_occupied(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(preflight.os, 'access', lambda *a: True)
    monkeypatch.setattr(preflight.os, 'sched_getaffinity', lambda _: {0})
    original = Path.read_text
    def read(path, *a, **k):
        if path.name == 'ip_local_port_range':
            return '10000 19999'
        if path.name == 'ip_local_reserved_ports':
            return '18001'
        return original(path, *a, **k)
    monkeypatch.setattr(Path, 'read_text', read)
    config = dict(base_url='http://127.0.0.1:18000', serving_sim_launch=dict(
        sim_root=str(tmp_path), venv=str(tmp_path), trace_catalog=str(tmp_path),
        cpu_affinity=[0], cores=1, topology='H100:1'))
    with pytest.raises(ValueError, match='ephemeral'):
        preflight.check_ports(config)
    config['base_url'] = 'http://127.0.0.1:18001'
    assert preflight.check_ports(config)['ports'] == [18001]
    with socket.socket() as s:
        s.bind(('127.0.0.1', 18001))
        with pytest.raises(RuntimeError, match='occupied'):
            preflight.check_ports(config)
