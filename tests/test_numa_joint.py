from types import SimpleNamespace

import pytest

from clawbox.experiments.worker import WatermarkController, observed_tool_concurrency
from clawbox.experiments.snapshot_pool import WarmSnapshotPool
from clawbox.experiments.memory_actions import MemoryActionCostModel
from clawbox.experiments.numa_borrow import NumaBorrowRecord, NumaResidencyObservation
from clawbox.experiments.spec import (
    ExperimentSpec, MemoryActionCostSpec, ReclamationPolicy, SnapshotTier,
    expand_matrix,
)
from clawbox.experiments.configure import experiment_overview
from test_experiments_v2 import raw_spec


def controller(mode, *, idle=False, costs=None):
    events = []
    calls = []
    arm = SimpleNamespace(resources=SimpleNamespace(
        local_memory_low_watermark_mib=28, local_memory_high_watermark_mib=32,
        local_memory_capacity_mib=36, memory_scheduling=mode,
        borrow_cooldown_seconds=5, return_cooldown_seconds=5,
        memory_action_costs=costs),
        policy=SimpleNamespace(reclamation=ReclamationPolicy.SNAPSHOT_PAUSE))
    def evict():
        calls.append('reclaim')
        return idle
    c = WatermarkController(arm, SimpleNamespace(
        set_new_session_admission_blocked=lambda _: None,
        evict_one_for_watermark=evict, blocked_demand_pressure=lambda: True),
        SimpleNamespace(tier_usage=lambda: (33*1024**2, 0, 33*1024**2)),
        SimpleNamespace(node_id='n0', shared_pool=WarmSnapshotPool(100*1024**2)),
        SimpleNamespace(write=events.append))
    c._borrow_one = lambda: calls.append('borrow') or True
    return c, calls, events


def test_joint_reclaims_idle_before_considering_borrow():
    c, calls, events = controller('joint', idle=True)
    c._sample_once(.2, False)
    assert calls == ['reclaim']
    assert events[-1]['action'] == 'reclaim'


def test_joint_checks_idle_reclaim_for_blocked_demand_below_high():
    c, calls, events = controller('joint', idle=True)
    c.sampler.tier_usage = lambda: (20*1024**2, 0, 20*1024**2)
    c._sample_once(.2, False)
    assert calls == ['reclaim']
    assert events[-1]['action'] == 'reclaim'


def test_joint_borrows_at_high_when_no_safe_idle_victim():
    c, calls, events = controller('joint')
    c._sample_once(.2, False)
    assert calls == ['reclaim', 'borrow']
    assert events[-1]['action'] == 'borrow'
    assert events[-1]['cost_model'] == 'uncalibrated_deterministic_fallback'


def test_snapshot_only_never_borrows():
    c, calls, _ = controller('snapshot_only')
    c._sample_once(.2, False)
    assert calls == ['reclaim']


def test_snapshot_watermarks_work_without_a_live_borrower():
    original, calls, events = controller('snapshot_only', idle=True)
    blocked = []
    original.coordinator.set_new_session_admission_blocked = blocked.append
    c = WatermarkController(original.arm, original.coordinator, original.sampler,
                            None, original.events, shared_pool=WarmSnapshotPool(100*1024**2),
                            node_id='node0')
    assert c._sample_once(.2, False) is True
    assert blocked == [True] and calls == ['reclaim']
    assert events[0]['compute_node'] == 'node0'
    assert c.route_progress_to_shared(SimpleNamespace(), 100) is False
    c.sampler.tier_usage = lambda: (27*1024**2, 0, 27*1024**2)
    assert c._sample_once(.2, True) is False
    assert blocked == [True, False]


@pytest.mark.parametrize('mechanism', ['incremental-cow', 'full-copy'])
@pytest.mark.parametrize('storage', ['tiered', 'warm-only'])
def test_snapshot_only_preserves_warm_without_live_borrow_in_arms_and_overview(mechanism, storage):
    raw = raw_spec()
    raw['policies'] = [raw['policies'][1]]
    raw['resources'].update(memory_scheduling='snapshot_only',
        warm_memory_capacity_mib=8192, warm_snapshot_root='/warm',
        snapshot_mechanism=mechanism, snapshot_storage=storage)
    spec = ExperimentSpec.model_validate(raw)
    arms = expand_matrix(spec)
    assert all(a.resources.warm_memory_capacity_mib == 8192 for a in arms)
    assert all(a.resources.default_snapshot_tier is SnapshotTier.WARM for a in arms)
    assert experiment_overview(spec)['effective_policy_resources']['proposed']['shared_pool_gib'] == 8
    # The existing static non-oracle SSD baseline retains its former semantics.
    raw['resources'].update(memory_scheduling='static', snapshot_storage='tiered')
    assert all(a.resources.warm_memory_capacity_mib == 0
               for a in expand_matrix(ExperimentSpec.model_validate(raw)))


@pytest.mark.parametrize('missing', ['warm_snapshot_root', 'cold_snapshot_root'])
def test_snapshot_only_validates_both_destinations_before_running(missing):
    raw = raw_spec()
    raw['policies'] = [raw['policies'][1]]
    raw['resources'].update(memory_scheduling='snapshot_only',
        snapshot_mechanism='full-copy', warm_memory_capacity_mib=8192,
        warm_snapshot_root='/warm', cold_snapshot_root='/cold')
    raw['resources'][missing] = None
    with pytest.raises(ValueError, match=missing):
        ExperimentSpec.model_validate(raw)


def test_joint_waits_when_no_capacity_or_candidate():
    c, _, events = controller('joint')
    c._borrow_one = lambda: False
    c._sample_once(.2, False)
    assert events[-1]['action'] == 'wait'


def test_empirical_cost_model_compares_configured_vm_footprints():
    costs = MemoryActionCostSpec(
        provenance='/results/calibration.json',
        live_borrow_service_seconds_per_gib=.1,
        live_borrow_runtime_penalty_seconds_per_gib=.4,
        warm_checkpoint_seconds_per_gib=.2,
        warm_restore_seconds_per_gib=.4,
        expected_restore_probability=.5,
    )
    action, estimates = MemoryActionCostModel(costs).choose(
        borrow_bytes=4*1024**3, reclaim_bytes=8*1024**3,
    )
    assert action == 'borrow'
    assert estimates['borrow']['estimated_seconds'] == 2
    assert estimates['reclaim']['estimated_seconds'] == pytest.approx(3.2)
    assert estimates['reclaim']['basis'] == 'configured_vm_gib'


def test_cost_calibration_is_joint_only():
    raw = raw_spec()
    raw['resources']['memory_action_costs'] = {
        'provenance': '/results/calibration.json',
        'live_borrow_service_seconds_per_gib': .1,
        'live_borrow_runtime_penalty_seconds_per_gib': .4,
        'warm_checkpoint_seconds_per_gib': .2,
        'warm_restore_seconds_per_gib': .4,
        'cold_checkpoint_seconds_per_gib': .8,
        'cold_restore_seconds_per_gib': .7,
        'expected_restore_probability': .5,
    }
    with pytest.raises(ValueError, match='memory_scheduling=joint'):
        ExperimentSpec.model_validate(raw)


def test_describe_exposes_joint_calibration_and_return_cooldown():
    raw = raw_spec()
    raw['policies'] = [raw['policies'][1]]
    raw['resources'].update(
        memory_scheduling='joint', pool_memory_budget_mib=100000,
        local_memory_low_watermark_mib=90000,
        local_memory_high_watermark_mib=100000,
        local_memory_capacity_mib=110000,
        shared_memory_borrow_limit_mib=8000,
        warm_memory_capacity_mib=16000,
        local_numa_node=0, warm_numa_node=1,
        warm_snapshot_root='/warm', cold_snapshot_root='/cold',
        return_cooldown_seconds=7,
        memory_action_costs={
            'provenance': 'calibration-run-1',
            'live_borrow_service_seconds_per_gib': .1,
            'live_borrow_runtime_penalty_seconds_per_gib': .2,
            'warm_checkpoint_seconds_per_gib': .3,
            'warm_restore_seconds_per_gib': .4,
            'cold_checkpoint_seconds_per_gib': .5,
            'cold_restore_seconds_per_gib': .6,
            'expected_restore_probability': .75,
            'remote_access_latency_ns': 400,
            'remote_stream_bandwidth_mib_per_second': 12000,
        },
    )
    overview = experiment_overview(ExperimentSpec.model_validate(raw))
    assert overview['return_cooldown_seconds'] == 7
    assert overview['memory_action_costs']['provenance'] == 'calibration-run-1'
    assert overview['memory_action_costs']['remote_access_latency_ns'] == 400


def test_joint_uses_calibrated_order_and_records_candidate_costs():
    gib = 1024**3
    costs = MemoryActionCostSpec(
        provenance='/results/calibration.json',
        live_borrow_service_seconds_per_gib=.05,
        live_borrow_runtime_penalty_seconds_per_gib=.05,
        warm_checkpoint_seconds_per_gib=1,
        warm_restore_seconds_per_gib=1,
        expected_restore_probability=1,
    )
    events = []
    pool = WarmSnapshotPool(32*gib, borrow_capacity_bytes=16*gib)
    borrowed = set()

    class Borrower:
        node_id = 'node0'
        shared_node = 2
        shared_pool = pool
        def borrowed(self):
            return tuple(borrowed)
        def allocates_shared(self, sandbox_id):
            return sandbox_id in borrowed

    class Lifecycle:
        sandbox_id = 'tool-1'
        role = 'tool'
        resident = True
        configured_memory_bytes = 4*gib
        def borrow_shared(self):
            pool.reserve_borrow(self.sandbox_id, self.configured_memory_bytes)
            borrowed.add(self.sandbox_id)
            return NumaBorrowRecord(
                self.sandbox_id, '/cg/tool-1', self.configured_memory_bytes,
                0, 2, 1, 2, .1, 4*gib, 4*gib, 0, 0,
            )

    reclaim_calls = []
    coordinator = SimpleNamespace(
        evict_one_for_watermark=lambda **_kwargs: reclaim_calls.append(True) or True,
        watermark_reclaim_candidate=lambda: {
            'session_id': 'idle-1', 'configured_bytes': 4*gib, 'vm_count': 1,
            'snapshot_reservation_bytes': 5*gib,
            'predicted_idle_seconds_remaining': 10,
        },
    )
    arm = SimpleNamespace(resources=SimpleNamespace(
        local_memory_low_watermark_mib=28*1024,
        local_memory_high_watermark_mib=32*1024,
        local_memory_capacity_mib=36*1024,
        memory_scheduling='joint', borrow_cooldown_seconds=0,
        return_cooldown_seconds=0, memory_action_costs=costs, snapshot_destination='auto'),
        policy=SimpleNamespace(reclamation=ReclamationPolicy.SNAPSHOT_PAUSE))
    c = WatermarkController(
        arm, coordinator, SimpleNamespace(), Borrower(),
        SimpleNamespace(write=events.append),
    )
    assert c._joint_relieve_pressure(
        preferred_borrow=Lifecycle(), reason='test_pressure',
    ) == 'borrow'
    assert reclaim_calls == []
    decision = events[-1]
    assert decision['cost_model'] == 'configured_empirical_action_cost_v1'
    assert decision['cost_provenance'] == '/results/calibration.json'
    assert decision['cost_estimates']['borrow']['estimated_seconds'] < \
        decision['cost_estimates']['reclaim']['estimated_seconds']


@pytest.mark.parametrize('forced_cold', [False, True])
def test_calibrated_joint_plans_cold_when_warm_capacity_is_reserved(forced_cold):
    gib = 1024**3
    costs = MemoryActionCostSpec(
        provenance='/results/calibration.json',
        live_borrow_service_seconds_per_gib=3,
        live_borrow_runtime_penalty_seconds_per_gib=3,
        warm_checkpoint_seconds_per_gib=.1,
        warm_restore_seconds_per_gib=.1,
        cold_checkpoint_seconds_per_gib=.2,
        cold_restore_seconds_per_gib=.2,
        expected_restore_probability=1,
    )
    pool = WarmSnapshotPool(8*gib, borrow_capacity_bytes=8*gib)
    if not forced_cold:
        pool.reserve_borrow('existing-vm', 8*gib)
    events, tiers = [], []
    coordinator = SimpleNamespace(
        evict_one_for_watermark=lambda **kwargs: tiers.append(kwargs['tier']) or True,
        watermark_reclaim_candidate=lambda: {
            'session_id': 'idle-1', 'configured_bytes': 4*gib,
            'snapshot_reservation_bytes': 5*gib, 'vm_count': 1,
            'predicted_idle_seconds_remaining': 10,
        },
    )
    arm = SimpleNamespace(resources=SimpleNamespace(
        local_memory_low_watermark_mib=28*1024,
        local_memory_high_watermark_mib=32*1024,
        local_memory_capacity_mib=36*1024,
        memory_scheduling='joint', borrow_cooldown_seconds=0,
        return_cooldown_seconds=0, memory_action_costs=costs,
        snapshot_storage='tiered', cold_snapshot_root='/cold',
        snapshot_destination='cold' if forced_cold else 'auto'),
        policy=SimpleNamespace(reclamation=ReclamationPolicy.SNAPSHOT_PAUSE))
    borrower = SimpleNamespace(
        node_id='node0', shared_pool=pool, borrowed=lambda: ('existing-vm',),
    )
    c = WatermarkController(
        arm, coordinator, SimpleNamespace(), borrower,
        SimpleNamespace(write=events.append),
    )
    assert c._joint_relieve_pressure(reason='test_pressure') == 'reclaim'
    assert tiers == [SnapshotTier.COLD]
    assert events[-1]['candidates']['reclaim']['planned_snapshot_tier'] == 'cold'
    assert events[-1]['cost_estimates']['reclaim']['snapshot_tier'] == 'cold'


def test_low_watermark_returns_only_shared_residency_that_fits_below_high():
    mib = 1024**2
    pool = WarmSnapshotPool(128*mib, borrow_capacity_bytes=64*mib)
    events = []
    observation = NumaResidencyObservation(
        sandbox_id='tool-1', cgroup='/cg/tool-1', effective_mems='2',
        local_bytes=4*mib, shared_bytes=8*mib, other_numa_bytes=0,
        unattributed_bytes=0, charged_bytes=12*mib, reserved_bytes=16*mib,
        observed_unix_s=1,
    )

    class Borrower:
        node_id = 'node0'
        shared_node = 2
        shared_pool = pool
        def borrowed(self):
            return ('tool-1',)
        def residency(self, sandbox_id):
            assert sandbox_id == 'tool-1'
            return observation

    class Lifecycle:
        sandbox_id = 'tool-1'
        role = 'tool'
        resident = True
        configured_memory_bytes = 16*mib
        def observe_borrow_candidate(self):
            return observation
        def return_local_memory(self):
            return NumaBorrowRecord(
                self.sandbox_id, '/cg/tool-1', 16*mib, 2, 0,
                1, 2, .2, 4*mib, 12*mib, 8*mib, 0,
                reservation_released=True,
                physical_effect='local_policy_restored_reservation_released',
            )

    arm = SimpleNamespace(resources=SimpleNamespace(
        local_memory_low_watermark_mib=20,
        local_memory_high_watermark_mib=32,
        local_memory_capacity_mib=36,
        memory_scheduling='joint', borrow_cooldown_seconds=0,
        return_cooldown_seconds=0, memory_action_costs=None),
        policy=SimpleNamespace(reclamation=ReclamationPolicy.SNAPSHOT_PAUSE))
    c = WatermarkController(
        arm, SimpleNamespace(claim_memory_return=lambda _: True,
                             release_memory_return=lambda _: None),
        SimpleNamespace(), Borrower(),
        SimpleNamespace(write=events.append),
    )
    c.register('session-1', Lifecycle(), SimpleNamespace(
        sandbox_id='runtime-1', resident=False,
    ))
    assert c._return_one(25*mib) is False
    assert c._return_one(20*mib) is True
    assert c.return_count == 1 and c.return_release_count == 1
    assert events[-2]['event'] == 'shared_memory_returned'
    assert events[-1]['action'] == 'return'


def test_low_watermark_does_not_return_a_tool_with_inflight_growth():
    mib = 1024**2
    pool = WarmSnapshotPool(128*mib, borrow_capacity_bytes=64*mib)
    observation = NumaResidencyObservation(
        sandbox_id='tool-1', cgroup='/cg/tool-1', effective_mems='2',
        local_bytes=0, shared_bytes=mib, other_numa_bytes=0,
        unattributed_bytes=0, charged_bytes=mib, reserved_bytes=4*mib,
        observed_unix_s=1,
    )

    class Lifecycle:
        sandbox_id = 'tool-1'
        role = 'tool'
        resident = True
        returned = False
        def observe_borrow_candidate(self):
            return observation
        def return_local_memory(self):
            self.returned = True
            return NumaBorrowRecord(
                self.sandbox_id, '/cg/tool-1', 4*mib, 2, 0,
                1, 2, .1, 0, mib, 0, mib,
                reservation_released=True,
                physical_effect='local_policy_restored_reservation_released',
            )

    from clawbox.experiments.policy import PolicyCoordinator
    from clawbox.experiments.spec import PolicySpec
    policy = PolicySpec(name='joint', admission='predicted',
                        reclamation='snapshot_pause',
                        eviction='wait_aware_pressure', restore='reactive')
    coordinator = PolicyCoordinator(
        policy, budget_mib=16, progress_budget_mib=18,
        emergency_free_mib=0, operation_headroom_mib=1,
        physical_sample=lambda: (10*mib, 100*mib),
    )
    lifecycle = Lifecycle()
    coordinator.register('session-1', lifecycle)
    coordinator.begin_tool_admission('session-1', 4, 0, total_mib=4)
    borrower = SimpleNamespace(
        node_id='node0', shared_node=2, shared_pool=pool,
        borrowed=lambda: ('tool-1',),
    )
    arm = SimpleNamespace(resources=SimpleNamespace(
        local_memory_low_watermark_mib=12,
        local_memory_high_watermark_mib=16,
        local_memory_capacity_mib=18,
        memory_scheduling='joint', borrow_cooldown_seconds=0,
        return_cooldown_seconds=0, memory_action_costs=None),
        policy=SimpleNamespace(reclamation=ReclamationPolicy.SNAPSHOT_PAUSE))
    controller = WatermarkController(
        arm, coordinator, SimpleNamespace(), borrower,
        SimpleNamespace(write=lambda event: None),
    )
    controller.register('session-1', lifecycle, SimpleNamespace(
        sandbox_id='runtime-1', resident=False,
    ))

    assert controller._return_one(10*mib) is False
    assert not lifecycle.returned
    coordinator.release('session-1', 4)
    coordinator.set_tool_active('session-1', False)
    assert controller._return_one(10*mib) is True
    assert lifecycle.returned


def test_low_watermark_releases_return_claim_when_return_fails():
    mib = 1024**2
    calls = []
    pool = WarmSnapshotPool(128*mib, borrow_capacity_bytes=64*mib)

    class Lifecycle:
        sandbox_id = 'tool-1'
        role = 'tool'
        resident = True
        def observe_borrow_candidate(self):
            return NumaResidencyObservation(
                sandbox_id=self.sandbox_id, cgroup='/cg/tool-1', effective_mems='2',
                local_bytes=0, shared_bytes=mib, other_numa_bytes=0,
                unattributed_bytes=0, charged_bytes=mib, reserved_bytes=mib,
                observed_unix_s=1,
            )
        def return_local_memory(self):
            raise RuntimeError('return failed')

    coordinator = SimpleNamespace(
        claim_memory_return=lambda session_id: calls.append(('claim', session_id)) or True,
        release_memory_return=lambda session_id: calls.append(('release', session_id)),
    )
    borrower = SimpleNamespace(
        node_id='node0', shared_node=2, shared_pool=pool,
        borrowed=lambda: ('tool-1',),
    )
    arm = SimpleNamespace(resources=SimpleNamespace(
        local_memory_low_watermark_mib=12,
        local_memory_high_watermark_mib=16,
        local_memory_capacity_mib=18,
        memory_scheduling='joint', borrow_cooldown_seconds=0,
        return_cooldown_seconds=0, memory_action_costs=None),
        policy=SimpleNamespace(reclamation=ReclamationPolicy.SNAPSHOT_PAUSE))
    controller = WatermarkController(
        arm, coordinator, SimpleNamespace(), borrower,
        SimpleNamespace(write=lambda event: None),
    )
    controller.register('session-1', Lifecycle(), SimpleNamespace(
        sandbox_id='runtime-1', resident=False,
    ))

    with pytest.raises(RuntimeError, match='return failed'):
        controller._return_one(10*mib)

    assert calls == [('claim', 'session-1'), ('release', 'session-1')]


def test_low_watermark_runtime_return_does_not_use_tool_coordination():
    mib = 1024**2
    pool = WarmSnapshotPool(128*mib, borrow_capacity_bytes=64*mib)

    class Lifecycle:
        sandbox_id = 'runtime-1'
        role = 'runtime'
        resident = True
        def observe_borrow_candidate(self):
            return NumaResidencyObservation(
                sandbox_id=self.sandbox_id, cgroup='/cg/runtime-1', effective_mems='2',
                local_bytes=0, shared_bytes=mib, other_numa_bytes=0,
                unattributed_bytes=0, charged_bytes=mib, reserved_bytes=mib,
                observed_unix_s=1,
            )
        def return_local_memory(self):
            return NumaBorrowRecord(
                self.sandbox_id, '/cg/runtime-1', mib, 2, 0,
                1, 2, .1, 0, mib, 0, mib,
                reservation_released=True,
                physical_effect='local_policy_restored_reservation_released',
            )

    coordinator = SimpleNamespace(
        claim_memory_return=lambda _session_id: pytest.fail(
            'Runtime return must not use Tool coordination'
        ),
        release_memory_return=lambda _session_id: pytest.fail(
            'Runtime return must not use Tool coordination'
        ),
    )
    borrower = SimpleNamespace(
        node_id='node0', shared_node=2, shared_pool=pool,
        borrowed=lambda: ('runtime-1',),
    )
    arm = SimpleNamespace(resources=SimpleNamespace(
        local_memory_low_watermark_mib=12,
        local_memory_high_watermark_mib=16,
        local_memory_capacity_mib=18,
        memory_scheduling='joint', borrow_cooldown_seconds=0,
        return_cooldown_seconds=0, memory_action_costs=None),
        policy=SimpleNamespace(reclamation=ReclamationPolicy.SNAPSHOT_PAUSE))
    controller = WatermarkController(
        arm, coordinator, SimpleNamespace(), borrower,
        SimpleNamespace(write=lambda event: None),
    )
    controller.register('session-1', SimpleNamespace(
        sandbox_id='tool-1', resident=True,
    ), Lifecycle())

    assert controller._return_one(10*mib) is True


def test_low_watermark_does_not_return_an_already_local_residual_again():
    mib = 1024**2
    pool = WarmSnapshotPool(128*mib, borrow_capacity_bytes=64*mib)

    class Borrower:
        node_id = 'node0'
        shared_node = 2
        shared_pool = pool
        def borrowed(self):
            return ('tool-1',)
        def residency(self, sandbox_id):
            return NumaResidencyObservation(
                sandbox_id=sandbox_id, cgroup='/cg/tool-1',
                effective_mems='0', local_bytes=12*mib,
                shared_bytes=1, other_numa_bytes=0,
                unattributed_bytes=0, charged_bytes=12*mib,
                reserved_bytes=16*mib, observed_unix_s=1,
            )

    class Lifecycle:
        sandbox_id = 'tool-1'
        role = 'tool'
        resident = True
        configured_memory_bytes = 16*mib
        def observe_borrow_candidate(self):
            return NumaResidencyObservation(
                sandbox_id=self.sandbox_id, cgroup='/cg/tool-1',
                effective_mems='0', local_bytes=12*mib,
                shared_bytes=1, other_numa_bytes=0,
                unattributed_bytes=0, charged_bytes=12*mib,
                reserved_bytes=16*mib, observed_unix_s=1,
            )
        def return_local_memory(self):
            raise AssertionError('an already-LOCAL VM must not be returned again')

    arm = SimpleNamespace(resources=SimpleNamespace(
        local_memory_low_watermark_mib=20,
        local_memory_high_watermark_mib=32,
        local_memory_capacity_mib=36,
        memory_scheduling='joint', borrow_cooldown_seconds=0,
        return_cooldown_seconds=0, memory_action_costs=None),
        policy=SimpleNamespace(reclamation=ReclamationPolicy.SNAPSHOT_PAUSE))
    controller = WatermarkController(
        arm, SimpleNamespace(claim_memory_return=lambda _: True,
                             release_memory_return=lambda _: None),
        SimpleNamespace(), Borrower(),
        SimpleNamespace(write=lambda event: None),
    )
    controller.register('session-1', Lifecycle(), SimpleNamespace(
        sandbox_id='runtime-1', resident=False,
    ))

    assert controller._return_one(20*mib) is False
    assert controller.return_count == 0


def test_low_watermark_ignores_lifecycle_removed_after_registry_snapshot():
    """Normal session cleanup must not kill the shared policy controller."""
    mib = 1024**2
    pool = WarmSnapshotPool(128*mib, borrow_capacity_bytes=64*mib)

    class Lifecycle:
        sandbox_id = 'tool-1'
        role = 'tool'
        resident = True
        configured_memory_bytes = 16*mib
        def observe_borrow_candidate(self):
            return None
        def return_local_memory(self):
            raise AssertionError('a removed lifecycle cannot be returned')

    borrower = SimpleNamespace(
        node_id='node0', shared_node=2, shared_pool=pool,
        borrowed=lambda: ('tool-1',),
    )
    arm = SimpleNamespace(resources=SimpleNamespace(
        local_memory_low_watermark_mib=20,
        local_memory_high_watermark_mib=32,
        local_memory_capacity_mib=36,
        memory_scheduling='joint', borrow_cooldown_seconds=0,
        return_cooldown_seconds=0, memory_action_costs=None),
        policy=SimpleNamespace(reclamation=ReclamationPolicy.SNAPSHOT_PAUSE))
    controller = WatermarkController(
        arm, SimpleNamespace(claim_memory_return=lambda _: True,
                             release_memory_return=lambda _: None),
        SimpleNamespace(), borrower,
        SimpleNamespace(write=lambda event: None),
    )
    controller.register('session-1', Lifecycle(), SimpleNamespace(
        sandbox_id='runtime-1', resident=False,
    ))

    assert controller._return_one(20*mib) is False
    assert controller._error is None


def test_snapshot_policies_cannot_be_labeled_local_only():
    raw = raw_spec()
    raw['resources']['memory_scheduling'] = 'local_only'
    raw['policies'][0].update(reclamation='snapshot_pause', eviction='wait_aware_pressure', restore='reactive')
    with pytest.raises(ValueError, match='local_only'):
        ExperimentSpec.model_validate(raw)


def test_transfer_timeout_and_failure_release_slot():
    pool = WarmSnapshotPool(100, transfer_concurrency=1, transfer_timeout_seconds=.01)
    with pool.transfer('checkpoint'):
        with pytest.raises(TimeoutError):
            with pool.transfer('rebind'):
                pytest.fail('overcommitted transfer slots')
    with pytest.raises(ValueError):
        with pool.transfer('restore'):
            raise ValueError('injected VMM failure')
    with pool.transfer('spill'):
        assert sum(pool.snapshot()['active_transfers'].values()) == 1
    assert sum(pool.snapshot()['active_transfers'].values()) == 0


def test_tool_concurrency_counts_overlapping_intervals_not_agents():
    sessions = [{'timeline': {'recorded_time_spans': [
        {'name': 'tool.operation', 'start_unix_s': start, 'end_unix_s': end}]}}
        for start, end in [(1, 3), (2, 4), (4, 5)]]
    result = observed_tool_concurrency(sessions)
    assert result['peak'] == 2
    assert result['mean_over_tool_window'] == 1.25
    assert result['interval_count'] == 3
    assert observed_tool_concurrency([])['peak'] is None


def test_agent_tool_concurrency_excludes_backend_maintenance():
    sessions = [{'timeline': {'recorded_time_spans': [
        {'name': 'tool.operation', 'start_unix_s': 1, 'end_unix_s': 4,
         'execution_scope': 'agent-tool'},
        {'name': 'tool.operation', 'start_unix_s': 2, 'end_unix_s': 3,
         'execution_scope': 'backend-maintenance'}]}}]
    assert observed_tool_concurrency(sessions)['peak'] == 2
    filtered = observed_tool_concurrency(sessions, execution_scope='agent-tool')
    assert filtered['peak'] == 1
    assert filtered['interval_count'] == 1
