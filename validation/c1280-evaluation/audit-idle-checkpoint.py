"""Bounded policy fault injection; no host/VM operations or performance claims."""
import json
from pathlib import Path

from clawbox.experiments.policy import PolicyCoordinator
from clawbox.experiments.snapshot_pool import WarmCapacityError
from clawbox.experiments.spec import PolicySpec


class Lifecycle:
    resident = True
    configured_memory_bytes = 4 * 1024**3
    snapshot_reservation_bytes = configured_memory_bytes

    def __init__(self, error=None):
        self.pauses = 0
        self.error = error

    def checkpoint_and_evict(self, **_):
        if self.error:
            raise self.error
        self.resident = False
        self.pauses += 1
        return 0.01


def coordinator(tool, runtime):
    policy = PolicySpec(name='wait-pair', admission='fixed', reclamation='snapshot_pause',
                        eviction='wait_aware_pressure', restore='reactive', runtime_snapshot=True)
    c = PolicyCoordinator(policy, budget_mib=32, emergency_free_mib=0,
                          operation_headroom_mib=0)
    c.register('idle', tool)
    c.register_runtime('idle', runtime)
    c.begin_model_wait('idle', 'llm', 100)
    return c


class RestoreCapacityError(Exception):
    status_code = 409


tool, runtime = Lifecycle(), Lifecycle()
c = coordinator(tool, runtime)
attempts = 0


def restore():
    global attempts
    attempts += 1
    if attempts == 1:
        raise RestoreCapacityError('injected Cube restore capacity rejection')
    return 0.01


c.restore('other', 1, restore, 1)
result = {'validation_class': 'isolated policy fault injection, not real VM validation',
          'restore_409': {'attempts': attempts, 'tool_pauses': tool.pauses,
                          'runtime_pauses': runtime.pauses}}
assert (attempts, tool.pauses, runtime.pauses) == (2, 1, 0)

c = coordinator(Lifecycle(WarmCapacityError('injected full WARM pool')), Lifecycle())
try:
    c.evict_one_for_watermark()
except WarmCapacityError as error:
    result['warm_full'] = {'propagates_from_watermark_eviction': True,
                           'error': str(error)}
else:
    raise AssertionError('Expected observed capacity-error propagation')

tool, runtime = Lifecycle(), Lifecycle(WarmCapacityError('injected pair capacity race'))
c = coordinator(tool, runtime)
try:
    c.evict_one_for_watermark()
except WarmCapacityError:
    result['pair_warm_full'] = {'tool_pauses': tool.pauses,
                                'runtime_pauses': runtime.pauses,
                                'propagates_after_partial_pair_pause': True}
else:
    raise AssertionError('Expected partial-pair capacity failure')
assert (tool.pauses, runtime.pauses) == (1, 0)

path = Path(__file__).with_name('idle-checkpoint-fault-audit.json')
path.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
