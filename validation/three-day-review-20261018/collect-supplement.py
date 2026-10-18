"""Run on kunpeng after collect.py; read-only supplement, no runner imports."""
import hashlib,json,sys
from pathlib import Path
P=Path(sys.argv[1] if len(sys.argv)>1 else '/tmp/clawbox-three-day-review-20261018')
arms=json.loads((P/'arms.json').read_text());sup=[];early=[];traces={};timelines=[]
for x in arms:
 run=Path(x['run_path']);state=json.loads((run/'run-state.json').read_text());qp=run/'qualification.json'
 q=json.loads(qp.read_text()) if qp.is_file() else {}
 rp=Path(x['result_path']) if x.get('result_path') else None
 result=json.loads(rp.read_text()) if rp and rp.is_file() else {};prov=result.get('provenance',{})
 sup.append({'id':x['id'],'spec_digest':state.get('spec_digest'),'state_failure':state.get('failure'),'qualification_path':str(qp) if q else None,'qualification_run_root':q.get('run_root'),'qualification_manifest':q.get('input_manifest'),'implementation_digest':q.get('implementation_digest'),'provenance_keys':list(prov),'provenance':prov,'result_status':result.get('status'),'result_failure':result.get('failure')})
 timelines.extend({'id':x['id'],**{k:v for k,v in t.items() if not isinstance(v,(dict,list))}} for t in result.get('performance',{}).get('session_timelines',[]))
 rec={'id':x['id'],'early_events':[]};names=set()
 if x.get('event_path'):
  with open(x['event_path']) as stream:
   for n,line in enumerate(stream):
    if n>600:break
    try:e=json.loads(line)
    except ValueError:continue
    name=e.get('event')
    if name in ['sandbox_create_gate_acquired','sandbox_create_gate_wait_started','runtime_memory_started','session_placement','arm_started'] and name not in names:rec['early_events'].append(e);names.add(name)
    if len(names)>=4:break
 early.append(rec)
 for t in (q.get('input_manifest') or {}).get('traces',[])+prov.get('configured_traces',[]):
  path=t.get('path') or t.get('reference')
  if not path or path in traces:continue
  f=Path(path)
  if not f.is_file():traces[path]={'path':path,'exists':False};continue
  b=f.read_bytes();items=[]
  try:items=[json.loads(line) for line in b.decode().splitlines() if line.strip()]
  except ValueError:pass
  traces[path]={'path':path,'exists':True,'sha256_current':hashlib.sha256(b).hexdigest(),'items':items}
for name,data in [('supplement',sup),('early-events',early),('trace-current-copies',list(traces.values())),('result-timelines',timelines)]:
 (P/(name+'.json')).write_text(json.dumps(data,indent=2))
print(json.dumps({'arms':len(arms),'traces':len(traces),'timelines':len(timelines)}))
