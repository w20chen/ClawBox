import json
from types import SimpleNamespace

import pytest

from clawbox.experiments.spec import InferenceSpec, ServingSimLaunchSpec
from clawbox.experiments.serving_sim_process import (
    ServingSimProcess, serving_sim_cluster_layouts,
)


def configuration():
    return dict(base_url='http://127.0.0.1:8000', serving_sim_launch=dict(
        sim_root='/sim', venv='/venv', trace_catalog='/catalog.json',
        cpu_affinity=[240, 241, 242, 243], cores=4, topology='8A'))


@pytest.mark.parametrize('field,value', [('cores', 3), ('cpu_affinity', [0, 0, 1, 2]),
    ('sim_root', 'relative'), ('cache_dir', 'relative'), ('hf_hub_cache', 'relative'),
    ('shutdown_timeout_seconds', 0), ('nofile', 0),
    ('tpot_ms', 0), ('tpot_ms', float('inf')), ('tpot_ms', True),
    ('decode_time_scale', -1), ('decode_time_scale', float('nan')),
    ('cpu_refill_enter_utilization', .96),
    ('cpu_refill_feedback_interval_seconds', 3.),
    ('cpu_refill_enter_samples', 0), ('cpu_refill_throttle_steps', 0)])
def test_launch_rejects_invalid_environment_contract(field, value):
    config = configuration()
    config['serving_sim_launch'][field] = value
    with pytest.raises(ValueError):
        InferenceSpec(backend='serving_sim', configuration=config)


@pytest.mark.parametrize('base', ['http://remote:8000', 'https://localhost:8000', 'http://localhost/wrong'])
def test_managed_service_cannot_adopt_remote_or_mismatched_endpoint(base):
    config = configuration()
    config['base_url'] = base
    with pytest.raises(ValueError):
        InferenceSpec(backend='serving_sim', configuration=config)


def test_cluster_port_range_must_fit():
    config = configuration()
    config['base_url'] = 'http://127.0.0.1:65530'
    config['serving_sim_launch']['cluster_count'] = 16
    with pytest.raises(ValueError, match='ports exceed'):
        InferenceSpec(backend='serving_sim', configuration=config)


def test_cluster_layouts_use_independent_ports_and_round_robin_cpus(tmp_path):
    config = configuration()
    config['serving_sim_launch'].update(
        cluster_count=16, cores=8, cpu_affinity=list(range(240, 248)),
    )
    spec = InferenceSpec(backend='serving_sim', configuration=config)
    launch = spec.configuration['serving_sim_launch']
    layouts = serving_sim_cluster_layouts(
        config, ServingSimLaunchSpec.model_validate(launch),
        tmp_path / 'gpu-service',
    )
    assert [row['port'] for row in layouts] == list(range(8000, 8016))
    assert [row['cpu_affinity'] for row in layouts] == [
        [cpu] for cpu in list(range(240, 248)) * 2
    ]
    assert {row['cores'] for row in layouts} == {1}
    assert layouts[0]['output'] == tmp_path / 'gpu-service/clusters/000/output'
    assert layouts[-1]['log'] == tmp_path / 'gpu-service/clusters/015/service.log'


def test_single_cluster_keeps_the_existing_cpu_and_artifact_layout(tmp_path):
    service = ServingSimProcess(configuration(), tmp_path)
    layout = service.layouts[0]
    assert layout['port'] == 8000
    assert layout['cpu_affinity'] == [240, 241, 242, 243]
    assert layout['cores'] == 4
    assert layout['output'] == tmp_path / 'gpu-service/output'


@pytest.mark.parametrize('knobs', [{}, {'tpot_ms': 20.}, {'decode_time_scale': 2.}])
def test_decode_timing_reaches_every_managed_cluster(tmp_path, knobs):
    config = configuration()
    config['serving_sim_launch'].update(cluster_count=2, **knobs)
    service = ServingSimProcess(config, tmp_path)
    for layout in service.layouts:
        command = service._command(layout)
        for name in ('decode_time_scale', 'tpot_ms'):
            flag = '--' + name.replace('_', '-')
            if name in knobs:
                assert float(command[command.index(flag) + 1]) == knobs[name]
            else:
                assert flag not in command
        assert command[command.index('--k') + 1] == '1'


def test_decode_timing_modes_are_exclusive():
    config = configuration()
    config['serving_sim_launch'].update(tpot_ms=20., decode_time_scale=2.)
    with pytest.raises(ValueError, match='mutually exclusive'):
        InferenceSpec(backend='serving_sim', configuration=config)


def test_cpu_refill_configuration_is_default_off_and_forwarded_to_every_cluster(tmp_path):
    config = configuration()
    config['serving_sim_launch'].update(
        cluster_count=2, cpu_refill_mode='decode-first',
        cpu_refill_enter_utilization=.85, cpu_refill_exit_utilization=.93,
        cpu_refill_enter_samples=3, cpu_refill_stale_seconds=4.,
        cpu_refill_throttle_steps=4, cpu_refill_release_steps=2,
    )
    service = ServingSimProcess(config, tmp_path)
    expected = service.spec.cpu_refill_configuration()
    assert expected['mode'] == 'decode-first'
    for layout in service.layouts:
        command = service._command(layout)
        for field, value in expected.items():
            flag = '--cpu-refill-' + field.replace('_', '-')
            assert command[command.index(flag) + 1] == str(value)
    assert ServingSimLaunchSpec.model_validate(configuration()['serving_sim_launch']) \
        .cpu_refill_configuration()['mode'] == 'off'


@pytest.mark.parametrize('flags', [['--tpot-ms', 'nan'], ['--decode-time-scale', '0']])
def test_manual_decode_timing_rejects_invalid_values(flags, capsys):
    from clawbox.integrations.serving_sim import main
    assert main(['serve', '--sim-root', '/sim', *flags]) == 2
    assert 'positive and finite' in capsys.readouterr().err


@pytest.mark.parametrize('invalid_cluster', [None, 1])
def test_multi_cluster_stop_requires_and_aggregates_every_final_report(
    tmp_path, invalid_cluster,
):
    config = configuration()
    config['serving_sim_launch'].update(
        cluster_count=2, cores=2, cpu_affinity=[240, 241],
    )
    service = ServingSimProcess(config, tmp_path)
    service.processes = [
        SimpleNamespace(returncode=0, poll=lambda: 0),
        SimpleNamespace(returncode=0, poll=lambda: 0),
    ]
    service.identities = [None, None]
    service.logs = [None, None]
    for index, layout in enumerate(service.layouts):
        layout['output'].mkdir(parents=True)
        (layout['output']/'clawbox-persistence.json').write_text(json.dumps(dict(
            finalized=True, native_close_succeeded=index != invalid_cluster,
        )))
        (layout['output']/'validity.json').write_text(
            '{"prompt_source":"actual","lag":{"p99_s":0.4},"non_physical":{"tpot_ms":20}}'
        )
        (layout['output']/'realized.json').write_text('[]')
        (layout['output']/'realized-inputs.jsonl').touch()
        (layout['output']/'realized-tokens.bin').touch()
    result = service.stop()
    assert result['stopped']
    if invalid_cluster is None:
        assert result['finalized']
        assert result['validity']['cluster_count'] == 2
        assert result['validity']['prompt_source'] == 'actual'
        assert len(result['validity']['clusters']) == 2
        assert 'warning_threshold_exceeded' not in result['validity']
        for row in result['validity']['clusters']:
            assert row['non_physical'] == {'tpot_ms': 20}
            assert row['lag'] == {'p99_s': .4}
            assert 'warning_threshold_exceeded' not in row
    else:
        assert not result['finalized']
        assert result['clusters'][0]['finalized']
        assert not result['clusters'][1]['finalized']
        assert 'cluster 1' in result['shutdown_error']['chain'][0]['message']


def test_stop_refuses_reused_pid_and_preserves_failure_receipt(tmp_path, monkeypatch):
    service = ServingSimProcess(configuration(), tmp_path)
    service.root.mkdir()
    service.identity = dict(pid=123, create_time=1.)
    service.process = SimpleNamespace(pid=123, returncode=None, poll=lambda: None)
    def forbidden():
        pytest.fail('must not signal an unrelated process')
    monkeypatch.setattr('psutil.Process', lambda pid: SimpleNamespace(create_time=lambda: 2., terminate=forbidden))
    receipt = service.stop()
    assert not receipt['stopped'] and not receipt['finalized']
    assert 'identity changed' in receipt['shutdown_error']['chain'][0]['message']
    assert service.stop() == receipt
    assert json.loads((service.root/'process.json').read_text()) == receipt


@pytest.mark.parametrize('native_close', [False, True])
def test_finalized_marker_requires_successful_native_close(tmp_path, native_close):
    service = ServingSimProcess(configuration(), tmp_path)
    service.out.mkdir(parents=True)
    service.process = SimpleNamespace(returncode=0, poll=lambda: 0)
    (service.out/'clawbox-persistence.json').write_text(json.dumps(dict(finalized=True, native_close_succeeded=native_close)))
    (service.out/'validity.json').write_text('{"prompt_source":"actual"}')
    (service.out/'realized.json').write_text('[]')
    (service.out/'realized-inputs.jsonl').touch()
    (service.out/'realized-tokens.bin').touch()
    result = service.stop()
    assert result['finalized'] == native_close
    assert ('shutdown_error' in result) != native_close


@pytest.mark.parametrize('requested', [False, True])
def test_uvicorn_signal_exit_requires_requested_stop_and_saved_reports(tmp_path, requested):
    import signal
    service = ServingSimProcess(configuration(), tmp_path)
    service.out.mkdir(parents=True)
    service.process = SimpleNamespace(returncode=-int(signal.SIGTERM), poll=lambda: -int(signal.SIGTERM))
    if requested:
        service.receipt['shutdown_signal'] = int(signal.SIGTERM)
    (service.out/'clawbox-persistence.json').write_text(json.dumps(dict(finalized=True, native_close_succeeded=True)))
    (service.out/'validity.json').write_text('{"prompt_source":"actual"}')
    (service.out/'realized.json').write_text('[]')
    (service.out/'realized-inputs.jsonl').touch()
    (service.out/'realized-tokens.bin').touch()
    assert ('shutdown_error' in service.stop()) != requested


@pytest.mark.parametrize('missing', ['realized-inputs.jsonl', 'realized-tokens.bin'])
def test_final_reports_require_incremental_inputs(tmp_path, missing):
    from clawbox.experiments.serving_sim_process import final_reports
    (tmp_path/'validity.json').write_text('{"prompt_source":"actual"}')
    for name in ('realized.json', 'realized-inputs.jsonl', 'realized-tokens.bin'):
        if name != missing:
            (tmp_path/name).touch()
    with pytest.raises(RuntimeError, match=missing):
        final_reports(tmp_path)
