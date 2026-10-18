import collections, copy, hashlib, json
from pathlib import Path
import yaml

P=Path(__file__).parent/'clawbox-three-day-review-20261018'
arms=json.loads((P/'arms.json').read_text(encoding='utf-8'))
sup={x['id']:x for x in json.loads((P/'supplement.json').read_text(encoding='utf-8'))}
configs={x['id']:json.loads((P/x['config_file']).read_text(encoding='utf-8')) for x in arms}
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False)
candidates={canonical(c['effective_arm']['policy']):c['effective_arm']['policy'] for c in configs.values() if c.get('effective_arm')}
resolved=[]
for row in arms:
 c=configs[row['id']]; frozen=yaml.safe_load((P/c['frozen_spec_file']).read_text(encoding='utf-8')); effective=c.get('effective_arm'); how='saved effective arm' if effective else None
 if effective is None:
  workload=frozen.get('workload',{});cases=workload.get('cases',[]);assignment=workload.get('session_assignment','single_case');levels=frozen.get('execution',{}).get('concurrency_levels',[])
  for pol in candidates.values():
   for concurrency in levels:
    identity={'spec_digest':sup[row['id']]['spec_digest'],'case_ids':[x['case_id'] for x in (cases if assignment=='round_robin' else cases[:1])],'session_assignment':assignment,'repetition':0,'concurrency':concurrency,'policy':pol}
    if hashlib.sha256(canonical(identity).encode()).hexdigest()[:24]==row['arm_id']:
     effective={'policy':pol,'resources':pol.get('resource_overrides') or frozen.get('resources') or frozen.get('environment',{}).get('resources'),'execution':{**frozen.get('execution',{}),**pol.get('execution_overrides',{})},'concurrency':concurrency,'case':cases[0] if cases else None,'runtime':frozen.get('runtime') or frozen.get('environment',{}).get('runtime'),'sandbox':frozen.get('sandbox') or frozen.get('environment',{}).get('sandbox'),'inference':frozen.get('inference')}
     how='policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec';break
   if effective:break
 if effective is None:
  policies=frozen.get('policies',[]);compare=frozen.get('comparison',{}).get('arms',[])
  effective={'resources':frozen.get('resources') or frozen.get('environment',{}).get('resources',{}),'execution':frozen.get('execution',{}),'runtime':frozen.get('runtime') or frozen.get('environment',{}).get('runtime'),'sandbox':frozen.get('sandbox') or frozen.get('environment',{}).get('sandbox'),'inference':frozen.get('inference'),'concurrency':row['concurrency'],'case':frozen.get('workload',{}).get('cases',[None])[0]}
  if len(compare)==1:
   m=copy.deepcopy(frozen.get('mechanisms',{}))
   for key,value in compare[0].get('override',{}).items():
    path=key.split('.');d=m
    for k in path[:-1]:d=d.setdefault(k,{})
    d[path[-1]]=value
   effective['policy']={'name':compare[0]['name'],'mechanisms':m};how='single declared frozen arm; public overrides applied without modern defaults'
  elif len(policies)==1:effective['policy']=policies[0];how='single declared frozen policy'
  else:how='common frozen fields only; multiple arms unresolved'
 c['resolved_for_review']=effective;c['resolution']=how;c['frozen_spec']=frozen
 resolved.append(c)
(P/'resolved-configs.json').write_text(json.dumps(resolved,indent=2,ensure_ascii=False),encoding='utf-8')
print(collections.Counter(x['resolution'] for x in resolved))
for c in resolved:
 if 'unresolved' in c['resolution']:print(c['run_id'],c['arm_id'])
