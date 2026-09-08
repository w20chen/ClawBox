import json
from pathlib import Path
from clawbox.cube.client import CubeSandboxClient

CubeSandboxClient().kill_owned_sandboxes('short-c40-20260908-v4-00')
base = Path('/home/weitianc/clawbox-tiered-study-20260907')
for path in (base / 'raw-standalone/gate-tiered-policy-c4-v1/arms').glob('*.json'):
    row = json.loads(path.read_text())
    print(json.dumps({'policy': row['arm']['policy']['name'], 'memory': row['memory'],
                      'correctness': {k: v for k, v in row['correctness'].items() if k != 'session_output_hashes'}}))
print('CLEANUP_OK')
