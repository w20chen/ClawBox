import json
from types import SimpleNamespace

import pytest

from clawbox.experiments.spec import InferenceSpec
from clawbox.experiments.serving_sim_process import ServingSimProcess


def configuration():
    return dict(base_url='http://127.0.0.1:8000', serving_sim_launch=dict(
        sim_root='/sim', venv='/venv', trace_catalog='/catalog.json',
        cpu_affinity=[240, 241, 242, 243], cores=4, topology='8A'))


@pytest.mark.parametrize('field,value', [('cores', 3), ('cpu_affinity', [0, 0, 1, 2]),
    ('sim_root', 'relative'), ('cache_dir', 'relative'), ('hf_hub_cache', 'relative'),
    ('shutdown_timeout_seconds', 0), ('nofile', 0)])
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
