"""Explicit worker-model fixtures, separate from the public configuration loader."""
from pathlib import Path
import yaml

from clawbox.experiments.spec import ExperimentSpec, PolicySpec, load_experiment


def policy(preset, **changes):
    values = {
        'uncontrolled-resident': dict(admission='off'),
        'default-resident': dict(admission='off', deployment='os-default'),
        'fixed-resident': dict(admission='fixed'),
        'predicted-resident': dict(admission='predicted'),
        'predicted-wait-snapshot': dict(admission='predicted', reclamation='snapshot_pause',
                                       eviction='wait_aware_pressure', restore='reactive'),
        'phase_memcg': dict(admission='off', runtime_control='explicit', memory_protection=True),
        'release_cpu': dict(admission='off', runtime_control='explicit', cpu_release_boost=True),
    }[preset]
    return PolicySpec.model_validate(dict(name=preset, reclamation='resident', eviction='none',
        restore='none') | values | changes)


def worker_fixture(path):
    raw = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    if raw.get('kind') == 'clawbox-resolved-experiment':
        return load_experiment(Path(path))
    return ExperimentSpec.model_validate(raw)


def simulator_configuration(**overrides):
    """Complete launch contract for model-only tests; never starts a service."""
    return dict(model='test', base_url='http://127.0.0.1:8000',
                serving_sim_catalog_manifest='/test/catalog-manifest.json',
                serving_sim_launch=dict(sim_root='/sim', venv='/venv', trace_catalog='/catalog.json',
                                        cpu_affinity=[0], cores=1, topology='8A')) | overrides
