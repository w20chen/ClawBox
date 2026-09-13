from pathlib import Path
from clawbox.experiments.spec import load_experiment
from clawbox.experiments.inputs import validate_inputs
for n in ('15five-full-copy.yaml','15five-incremental-cow.yaml'):
 s=load_experiment(Path(n)); print(n,len(s.policies),validate_inputs(s)['traces'][0]['actions'])
