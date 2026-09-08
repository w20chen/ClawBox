from pathlib import Path
from clawbox.experiments.spec import load_experiment

spec = load_experiment(Path('examples/experiments/tiered-oracle-rec-a-c40.yaml'))
assert spec.execution.concurrency_levels == (40,)
assert len(spec.policies) == 13
assert spec.workload.repetitions == 1
assert 'tool-static-time-oracle-reactive' in [p.name for p in spec.policies]
print('PASS: c40, one repetition, all 13 policies, legacy time oracle preserved')
