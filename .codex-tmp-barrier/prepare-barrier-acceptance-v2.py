import json,pathlib,shlex,shutil,yaml
G=pathlib.Path('/data/clawbox-acceptance-20261022T102744Z')
b=json.loads((G/'current-barrier-v2.json').read_text());target=int(b['target'])
assert 1 <= target <= 1280
base=pathlib.Path('/home/weitianc/.codex-prepare-all503-concurrency-acceptance.py').read_text()
base=base.replace(
 "run_id = 'swe-all503-c1280-acceptance-' + stamp.lower()",
 "run_id = 'swe-all503-c"+str(target)+"-barrier-v2-' + stamp.lower()")
base=base.replace("'offered_sessions': 1280", "'offered_sessions': "+str(target))
base=base.replace("concurrency_levels=[1280]", "concurrency_levels=["+str(target)+"]")
exec(compile(base,'prepare-all503-concurrency-acceptance.py','exec'),{})
r=json.loads((G/'current-run.json').read_text());p=pathlib.Path(r['root'])
shutil.copyfile('/home/weitianc/.codex-openclaw-driver-startup.py',p/'source/clawbox/experiments/openclaw_driver.py')
cleanup_source=G/'repairs/cleanup-bounded-20261022T130900Z/source'
for relative in ('clawbox/cube/client.py','clawbox/experiments/supervisor.py','clawbox/cli.py','docs/guide.md'):
 shutil.copyfile(cleanup_source/relative,p/'source'/relative)
url='http://'+b['host']+':'+str(b['port'])
headers={'Authorization':b['key'],'Content-Type':'application/json'}
w=json.loads((p/'workload.json').read_text())
probe='''import http.client,time,urllib.error,urllib.request
for barrier_probe_attempt in range(12):
 try:
  with urllib.request.urlopen(urllib.request.Request(BARRIER_HEALTH,headers=BARRIER_HEADERS),timeout=15) as response:
   assert response.status==200
  break
 except urllib.error.HTTPError as exc:
  if exc.code < 500 and exc.code != 429:raise
  if barrier_probe_attempt == 11:raise
 except (urllib.error.URLError,http.client.HTTPException,OSError):
  if barrier_probe_attempt == 11:raise
 time.sleep(min(0.25*(2**barrier_probe_attempt),5.0))
'''
probe='BARRIER_HEALTH='+repr(url+'/health')+'\nBARRIER_HEADERS='+repr(headers)+'\n'+probe
for t in w['tasks']:
 script=shlex.split(t['sandbox']['preflight_command'])[2]
 t['sandbox']['preflight_command']='/usr/bin/python3 -c '+shlex.quote(probe+script)
(p/'workload.json').write_text(json.dumps(w,indent=2))
validation='''import hashlib,http.client,json,os,pathlib,time,urllib.error,urllib.request
token=pathlib.Path('/tmp/clawbox-env-acceptance-20261022-token').read_text()
sid=os.environ['TASK_ID']
payload=json.dumps({'session_id':sid,'token':token}).encode()
phase=int(hashlib.sha256(sid.encode()).hexdigest()[:8],16)/0xffffffff
deadline=time.monotonic()+7200
arrived=False
failures=0
time.sleep(phase*2.0)
while time.monotonic()<deadline:
 request=urllib.request.Request(URL+('/health' if arrived else '/arrive'),data=None if arrived else payload,headers=HEADERS)
 try:
  with urllib.request.urlopen(request,timeout=15) as response:result=json.load(response)
  arrived=True;failures=0
 except urllib.error.HTTPError as exc:
  if exc.code < 500 and exc.code != 429:raise
  result=None;failures+=1
 except (urllib.error.URLError,http.client.HTTPException,OSError,json.JSONDecodeError):
  result=None;failures+=1
 if result and result['released']:
  assert result['arrived']==TARGET
  assert pathlib.Path('/testbed/.git').exists()
  print(json.dumps({'barrier_verified':True,'session_id':sid,'token':token}));break
 delay=(4.0+phase*2.0) if result else (min(0.25*(2**min(failures,5)),8.0)+phase)
 time.sleep(min(delay,max(0.0,deadline-time.monotonic())))
else:raise TimeoutError(str(TARGET)+'-task validation barrier was not released')
'''
validation='URL='+repr(url)+'\nHEADERS='+repr(headers)+'\nTARGET='+repr(target)+'\n'+validation
spec=yaml.safe_load((p/'experiment.yaml').read_text())
profile=json.loads((G/'full-host/host.json').read_text())
res=spec['environment']['resources']
for k in ('compute_nodes','local_memory_capacity_mib','local_memory_low_watermark_mib','local_memory_high_watermark_mib','local_memory_cgroup','local_numa_node','warm_numa_node','shared_memory_borrow_limit_mib'):res[k]=profile[k]
res['pool_memory_budget_mib']=profile['local_memory_high_watermark_mib']
spec['environment']['sandbox']=w['tasks'][0]['sandbox'].copy()
spec['mechanisms']['placement']['policy']='os_default'
spec['mechanisms']['deployment']['mode']='os-default'
spec['inference']['configuration']['runtime_startup_timeout_seconds']=300
spec['measurement']['validation']['command']='/usr/bin/python3 -c '+shlex.quote(validation)
spec['execution'].update(sandbox_create_concurrency=8,command_timeout_seconds=7300,arm_timeout_seconds=10800)
(p/'experiment.yaml').write_text(yaml.safe_dump(spec,sort_keys=False))
r.update(resident_hold_seconds=None,validation_barrier_root=b['root'],startup_concurrency=8,
         runtime_startup_timeout_seconds=300,host_profile=str(G/'full-host/host.json'),
         scope='all503 fidelity and original first model round; actual '+str(target)+' simultaneous pairs with explicit validation barrier v2',
         fixed_duration_hold=False)
(p/'preparation.json').write_text(json.dumps(r,indent=2));(G/'current-run.json').write_text(json.dumps(r,indent=2))
print(json.dumps({'run_id':p.name,'root':str(p),'barrier_port':b['port'],'selected_cases':len(w['tasks'])}))
