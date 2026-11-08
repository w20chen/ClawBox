from collections import Counter
from copy import deepcopy
import hashlib
import json
import random

import pytest

from clawbox.experiments.spec import ExperimentSpec, expand_matrix
from clawbox.experiments.worker import session_case_for, session_offer_offsets, session_worker_count
from clawbox.experiments.qualification import qualification_spec
from test_experiments_v2 import raw_spec


def grouped_raw():
    raw = raw_spec()
    raw['workload'].update(repetitions=1, session_assignment='grouped', rollouts_per_case=16,
        cases=[{'case_id': f'base-{index:03d}', 'source': 'recorded_trace',
                'source_reference': f'trace-{index}.jsonl', 'replay_trace_reference': f'trace-{index}.jsonl'}
               for index in range(80)])
    raw['execution'].update(concurrency_levels=[1280], arrival_schedule='normal',
                            normal_arrival_std_seconds=60, random_seed=20261109)
    raw['inference']['configuration']['time_scale'] = 1.0
    return raw


def test_80_bases_have_16_independent_offered_sessions_at_shared_group_time():
    spec = ExperimentSpec.model_validate(grouped_raw())
    arms = expand_matrix(spec)
    assert len(arms) == 2  # policies, not base cases
    first, second = arms
    cases = [session_case_for(first, i) for i in range(1280)]
    assert Counter(case.case_id for case in cases) == {f'base-{i:03d}': 16 for i in range(80)}
    assert cases[15].case_id == 'base-000' and cases[16].case_id == 'base-001'
    assert len({case.replay_trace_reference for case in cases[:16]}) == 1
    offsets = session_offer_offsets(first)
    assert offsets == session_offer_offsets(second) and len(offsets) == 1280
    assert offsets[0] == 0 and all(a <= b for a, b in zip(offsets, offsets[1:]))
    assert len(set(offsets)) == 80
    assert all(len(set(offsets[i:i+16])) == 1 for i in range(0, 1280, 16))
    assert session_worker_count(first) == 1280  # grouping imposes no active cap
    material = json.dumps({'random_seed': 20261109, 'repetition': 0,
                           'group_case_ids': [f'base-{i:03d}' for i in range(80)]},
                          sort_keys=True, separators=(',', ':'))
    rng = random.Random(int(hashlib.sha256(material.encode()).hexdigest()[:16], 16))
    draws = sorted(rng.gauss(0, 60) for _ in range(80))
    assert offsets == tuple(value-draws[0] for value in draws for _ in range(16))


def test_grouped_explicit_offsets_preserve_mapping_and_reject_split_group():
    raw = grouped_raw()
    raw['execution']['session_arrival_offsets_seconds'] = [float(i//16) for i in range(1280)]
    arm = expand_matrix(ExperimentSpec.model_validate(raw))[0]
    assert session_offer_offsets(arm)[16] == 1
    raw['execution']['session_arrival_offsets_seconds'][15] = .5
    with pytest.raises(ValueError, match='same offered arrival'):
        ExperimentSpec.model_validate(raw)


def test_grouped_requires_full_count_and_no_silent_replication_on_other_assignments():
    raw = grouped_raw()
    raw['execution']['concurrency_levels'] = [16]
    with pytest.raises(ValueError, match='cases × rollouts_per_case'):
        ExperimentSpec.model_validate(raw)
    raw = grouped_raw()
    raw['workload']['session_assignment'] = 'round_robin'
    with pytest.raises(ValueError, match='rollouts_per_case requires grouped'):
        ExperimentSpec.model_validate(raw)


def test_grouped_qualification_uses_one_rollout_per_base_without_mutating_formal():
    original = ExperimentSpec.model_validate(grouped_raw())
    qualified = qualification_spec(original)
    validated = ExperimentSpec.model_validate(qualified.model_dump())
    assert validated.execution.concurrency_levels == (80,)
    assert validated.workload.rollouts_per_case == 1
    assert validated.execution.arrival_schedule.value == 'burst'
    assert validated.inference.configuration['time_scale'] == 0
    assert original.execution.concurrency_levels == (1280,)
    assert original.workload.rollouts_per_case == 16
    assert original.inference.configuration['time_scale'] == 1


def test_grouped_offered_capacity_sums_each_cases_rollouts():
    from clawbox.experiments.reporting import _offered_capacity_text
    spec = ExperimentSpec.model_validate(grouped_raw())
    assert _offered_capacity_text(spec, 1280) == '7680 GiB'


def test_grouped_audit_counts_policies_instead_of_multiplying_base_cases(monkeypatch):
    from pathlib import Path
    from clawbox.experiments import audit
    spec = ExperimentSpec.model_validate(grouped_raw())
    monkeypatch.setattr(audit, 'load_experiment', lambda _: spec)
    monkeypatch.setattr(audit, '_artifact_provenance', lambda *_: {})
    monkeypatch.setattr(audit, 'ensure_supported_experiment', lambda _: None)
    assert audit.audit_experiment(Path('unused'))['arm_count'] == 2
