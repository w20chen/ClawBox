"""Cube's configured-VM quota is separate from measured ClawBox admission."""
from __future__ import annotations

import json
import math
from pathlib import Path
import sys
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

CONFIG = Path('/usr/local/services/cubetoolbox/CubeMaster/conf.yaml')


def scheduler_settings() -> dict:
    from .host import _run
    # Return scheduler fields only: the same file also contains credentials.
    script = """
import json,sys,yaml
s=yaml.safe_load(open(sys.argv[1])).get('scheduler',{})
print(json.dumps({k:s.get(k) for k in ('overcommit_ratio','overcommit_ratio_conf','ignore_redis_allocation')}))
"""
    return json.loads(_run('sudo', '-n', sys.executable, '-c', script, str(CONFIG)))


def node_capacity(node: str) -> dict:
    url = 'http://127.0.0.1:8089/internal/node?' + urlencode({'host_id': node})
    with build_opener(ProxyHandler({})).open(url, timeout=10) as response:
        data = json.load(response)
    if data.get('ret', {}).get('ret_code') != 200:
        raise RuntimeError('Cube node capacity query failed; check CubeMaster before launching')
    matches = [n for n in data.get('data', []) if node in (n.get('IP'), n.get('InstanceID'), n.get('uuid'))]
    if len(matches) != 1:
        raise RuntimeError(f'Cube node capacity query did not identify exactly one node: {node}')
    return matches[0]


def capacity_report(node: dict, settings: dict, demand: dict) -> dict:
    ratios = (settings.get('overcommit_ratio_conf') or {}).get(node['InstanceType'])
    ratios = ratios if ratios is not None else settings.get('overcommit_ratio') or {}
    def ratio(key, default):
        value = float(ratios.get(key) or default)
        return value if math.isfinite(value) and value > 0 else default
    cpu_ratio, mem_ratio = ratio('cpu_ratio', 3), ratio('mem_ratio', 2)
    ignored = settings.get('ignore_redis_allocation') is True
    # Cube's Go response marks these integer counters omitempty: absent means 0.
    cpu_free = int(node['QuotaCpu'] * cpu_ratio) - (0 if ignored else int(node.get('QuotaCpuUsage', 0)))
    mem_free = int(node['QuotaMem'] * mem_ratio) - (0 if ignored else int(node.get('QuotaMemUsage', 0)))
    # Cube rejects equality too. Ignore-allocation mode tests each individual VM.
    cpu_need = demand['max_vm_cpu_milli'] if ignored else demand['cpu_milli']
    mem_need = demand['max_vm_memory_mib'] if ignored else demand['memory_mib']
    return dict(ready=cpu_free > cpu_need and mem_free > mem_need,
                cpu_ratio=cpu_ratio, mem_ratio=mem_ratio, instance_type=node['InstanceType'],
                free_cpu_milli=cpu_free, free_memory_mib=mem_free,
                required_cpu_milli=cpu_need, required_memory_mib=mem_need,
                ignore_redis_allocation=ignored,
                accounting='configured VM quotas, not actual CPU or RAM consumption')


def require_capacity(profile: dict) -> bool:
    report = capacity_report(node_capacity(profile['node']), scheduler_settings(), profile['cube_capacity_demand'])
    print('Cube configured-capacity check: ' + json.dumps(report), flush=True)
    if not report['ready']:
        raise RuntimeError(
            'Cube configured-VM quota would limit this experiment before ClawBox admission. '
            'Review host.yaml cube_scheduler_overcommit for the reported instance_type, '
            'run host apply on an idle host, then launch with a new run ID. '
            'Increasing this quota does not increase the experiment cpuset or memory.max.'
        )
    return True


def apply_ratios(ratios: dict) -> bool:
    from .host import _run
    script = """
import json,pathlib,shutil,sys,time,yaml
p=pathlib.Path(sys.argv[1]); desired=json.loads(sys.argv[2])
data=yaml.safe_load(p.read_text()); scheduler=data.setdefault('scheduler',{})
current=scheduler.setdefault('overcommit_ratio_conf',{})
changed=any(current.get(k)!=v for k,v in desired.items())
if changed:
    shutil.copy2(p,p.with_name(p.name+'.pre-clawbox-quota-'+str(time.time_ns())))
    current.update(desired)
    p.write_text(yaml.safe_dump(data,sort_keys=False))
print(json.dumps({'changed':changed}))
"""
    result = json.loads(_run('sudo', '-n', sys.executable, '-c', script, str(CONFIG), json.dumps(ratios)))
    if any((scheduler_settings().get('overcommit_ratio_conf') or {}).get(k) != v for k,v in ratios.items()):
        raise RuntimeError('Cube scheduler ratios were not saved; inspect CubeMaster configuration permissions')
    return result['changed']
