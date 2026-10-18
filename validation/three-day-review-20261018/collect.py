"""Read-only historical evidence extraction. Never imports the experiment runner."""
import collections, datetime, gzip, hashlib, json, math, os, statistics, sys, tarfile
from pathlib import Path
import yaml

START = 1792080000.0  # 2026-10-16 00:00 Asia/Shanghai
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/clawbox-three-day-review-20261018')
OUT.mkdir(parents=True, exist_ok=True)
(OUT/'configs').mkdir(exist_ok=True)
TZ = datetime.timezone(datetime.timedelta(hours=8))
GIB = 1024**3

def read(p):
    try:return json.loads(Path(p).read_text())
    except (ValueError,OSError):return None

def scalar(d):return {k:v for k,v in (d or {}).items() if not isinstance(v,(dict,list))}
def pct(v,q):
    if not v:return None
    v=sorted(v);pos=(len(v)-1)*q;lo=int(pos);hi=math.ceil(pos)
    return v[lo]+(v[hi]-v[lo])*(pos-lo)
def stats(v):
    v=[x for x in v if isinstance(x,(int,float)) and math.isfinite(x)]
    return dict(n=len(v),mean=statistics.fmean(v) if v else None,p50=pct(v,.5),p90=pct(v,.9),p95=pct(v,.95),p99=pct(v,.99),max=max(v) if v else None)
def ts(e):
    if e.get('wall_time_ns'):return int(e['wall_time_ns'])/1e9
    return e.get('completed_unix_s') or e.get('started_unix_s')
def iso(t):return datetime.datetime.fromtimestamp(t,TZ).isoformat() if t else None
def compact_error(s):return str(s or '')[:1600]
def cpucount(s):
    try:return sum(int(b)-int(a)+1 if '-' in v else 1 for v in s.split(',') for a,b in [v.split('-') if '-' in v else (v,v)])
    except:return None

base=Path('/home/weitianc'); inventories=[]; probe_files=[]; excluded=[]
roots=[p for p in base.iterdir() if p.is_dir() and 'clawbox' in p.name.lower()]
skip={'.git','.venv','venv','node_modules','site-packages','__pycache__','.pytest_cache','objects','volumes','storage','model-gateway','sessions','tool-artifacts','runtime-traces','openclaw','traces'}
for top in roots:
    for root,ds,fs in os.walk(top):
        p=Path(root);ds[:]=[n for n in ds if n not in skip]
        if len(p.relative_to(top).parts)>9:ds[:]=[];continue
        if 'run-state.json' in fs:
            s=read(p/'run-state.json')
            if not isinstance(s,dict):continue
            created=s.get('created_unix_s');completed=s.get('completed_unix_s',created)
            if not isinstance(created,(int,float)):continue
            if (completed or created)<START:continue
            if not (p/'experiment.yaml').exists():
                excluded.append({'path':str(p),'reason':'no frozen experiment.yaml'});continue
            inventories.append({'path':str(p),'run_id':s.get('run_id'),'state':s.get('state'),'created':created,'crosses_start':created<START,'arms':len(s.get('arms',{}))})
        if 'result.json' in fs and any(v in str(p).lower() for v in ['probe','snapshot','borrow','qualification']):
            f=p/'result.json'
            if f.stat().st_mtime>=START:probe_files.append(str(f))
inventories.sort(key=lambda x:(x['created'],x['path']))
(OUT/'inventory.json').write_text(json.dumps(inventories,indent=2))
rows=[];windows=[];operations=[];configs=[];pending=[];issues=[];source_files=[];probe_rows=[]
rawops=gzip.open(OUT/'operations.jsonl.gz','wt'); rawmodels=gzip.open(OUT/'model-requests.jsonl.gz','wt'); rawsamples=gzip.open(OUT/'samples.jsonl.gz','wt'); rawsessions=gzip.open(OUT/'sessions.jsonl.gz','wt')

def source(path,kind):
    p=Path(path)
    if p.is_file():source_files.append({'path':str(p),'kind':kind,'bytes':p.stat().st_size,'mtime':p.stat().st_mtime})

for ni,inv in enumerate(inventories):
    run=Path(inv['path']);state=read(run/'run-state.json')
    source(run/'run-state.json','run-state');source(run/'experiment.yaml','frozen-spec')
    try:frozen=yaml.safe_load((run/'experiment.yaml').read_text())
    except Exception as e:issues.append({'run':str(run),'error':str(e)});continue
    runkey=hashlib.sha256(str(run).encode()).hexdigest()[:10]
    (OUT/'configs'/f'{runkey}-experiment.yaml').write_bytes((run/'experiment.yaml').read_bytes())
    started_ids=set()
    for aid,ast in state.get('arms',{}).items():
        if ast.get('started_unix_s') is None:
            pending.append({'run_path':str(run),'run_id':inv['run_id'],'arm_id':aid,'state':ast.get('status')});continue
        started_ids.add(aid)
    # Include any retained earlier attempts as separate observations.
    work=[]
    for aid in started_ids:
        ast=state['arms'][aid];art=Path(ast['artifact_root']) if ast.get('artifact_root') else None
        work.append((aid,ast,art,False))
    for ef in run.glob('attempts/*/*/events/*.jsonl'):
        aid=ef.stem;art=ef.parent.parent
        if any(aid==a and art==b for a,_,b,_ in work):continue
        work.append((aid,{},art,True))
    for aid,ast,art,historical in work:
        result_path=run/'arms'/f'{aid}.json'
        result=read(result_path) or {}
        if historical or (result.get('attempt_id') and ast.get('attempt_id') and result['attempt_id']!=ast['attempt_id']):
            result=read(art/'arms'/f'{aid}.json') or {};result_path=art/'arms'/f'{aid}.json'
        if not result and art:
            alt=read(art/'arms'/f'{aid}.json')
            if alt:result=alt;result_path=art/'arms'/f'{aid}.json'
        arm=result.get('arm') or {};perf=result.get('performance') or {};correct=result.get('correctness') or {}
        resources=arm.get('resources') or frozen.get('resources') or (frozen.get('environment') or {}).get('resources') or {}
        execution=arm.get('execution') or frozen.get('execution') or {}
        inf=arm.get('inference') or frozen.get('inference') or {}
        policy=arm.get('policy') or {}
        concurrency=arm.get('concurrency')
        if concurrency is None and len(execution.get('concurrency_levels',[]))==1:concurrency=execution['concurrency_levels'][0]
        attempt=ast.get('attempt_id') or (art.parent.name if art else result.get('attempt_id'))
        key=f'{runkey}-{attempt}-{aid}'
        source(result_path,'arm-result')
        config={'id':key,'run_path':str(run),'run_id':inv['run_id'],'arm_id':aid,'attempt_id':attempt,'effective_arm':arm or None,'frozen_spec_file':f'configs/{runkey}-experiment.yaml','effective_config_source':str(result_path) if arm else None}
        (OUT/'configs'/f'{key}.json').write_text(json.dumps(config,indent=2));configs.append(config)
        ef=art/'events'/f'{aid}.jsonl' if art else Path('/nonexistent')
        if not ef.exists() and result.get('artifacts',{}).get('events'):ef=Path(result['artifacts']['events'])
        source(ef,'events')
        event_counts=collections.Counter();completion=[];failed=[];tool_done=[];samples=[];sessions={};ops={};scope={};admissions=[];first=None;last=None;first_work=None;arm_error_time=None;malformed=0;created_sessions=set();model_records=[]
        def addop(e,kind,role,session,line=None):
            timing=e.get('lifecycle_timing') or e
            op=timing.get('operation') or kind
            end=timing.get('completed_unix_s') or timing.get('end_unix_s') or ts(e)
            start=timing.get('started_unix_s') or timing.get('start_unix_s')
            service=timing.get('service_seconds',timing.get('duration_seconds'))
            if not isinstance(service,(int,float)):return
            ident=(session,role,op,start,end)
            if ident in ops:return
            row={'id':key,'session_id':session,'role':role,'operation':op,'started_unix_s':start,'completed_unix_s':end,'service_seconds':service,'status':timing.get('status','ok'),'source_event':kind,'source_line':line,'source_path':str(ef),'snapshot_generation':timing.get('snapshot_generation'),'tier_from':timing.get('tier_from'),'tier_to':timing.get('tier_to'),'snapshot_metrics':timing.get('snapshot_metrics'),'host_observed_reclaimed_bytes':timing.get('host_observed_reclaimed_bytes'),'error_type':timing.get('error_type')}
            if kind.startswith('shared_memory_'):row.update({k:v for k,v in scalar(e).items() if k.endswith('_bytes') or k in ['physical_effect','reservation_reused','reservation_released']})
            ops[ident]=row
        if ef.is_file():
            with ef.open() as stream:
                for line_no,line in enumerate(stream,1):
                    try:e=json.loads(line)
                    except ValueError:malformed+=1;continue
                    t=ts(e);kind=e.get('event','unknown');sid=e.get('session_id');eid=e.get('execution_id')
                    event_counts[kind]+=1
                    if t is not None:first=t if first is None else min(first,t);last=t if last is None else max(last,t)
                    if kind=='sandbox_create_gate_wait_started' and first_work is None:first_work=t
                    if kind=='arm_failed' and arm_error_time is None:arm_error_time=t
                    if kind=='session_complete':completion.append((t,sid,e.get('valid')))
                    if kind in ['session_failed','policy_event_failed','memory_watermark_controller_failed','tool_admission_failed']:
                        if kind=='session_failed':failed.append((t,sid,compact_error(e.get('error')),e.get('error_type')))
                    if kind=='sandbox_created':created_sessions.add(sid)
                    if kind=='tool_admitted':
                        scope[(sid,eid)]=e.get('execution_scope','unspecified')
                        admissions.append((t,e.get('admission_blocked_seconds')))
                    if kind=='tool_completed':tool_done.append((t,scope.get((sid,eid),'unmatched'),e.get('exit_code'),sid,eid))
                    if kind=='memory_sample':samples.append(e)
                    if kind in ['sandbox_created','sandbox_paused','sandbox_restored','sandbox_destroyed','shared_memory_borrowed','shared_memory_returned']:
                        mapped={'sandbox_created':'create','sandbox_paused':'checkpoint','sandbox_restored':'restore','sandbox_destroyed':'destroy','shared_memory_borrowed':'borrow','shared_memory_returned':'return'}[kind]
                        addop(e,mapped,e.get('role'),sid or e.get('sandbox_id'),line_no)
                    if kind=='session_timing':
                        spans=e.get('time_spans') or []
                        row=sessions.setdefault(sid,{'session_id':sid})
                        for sp in spans:
                            name=sp.get('name','')
                            if name in ['agent','session','sandbox.create','sandbox.create.queue','validation','sandbox.cleanup']:row[name]=scalar(sp)
                            if sp.get('operation') in ['create','checkpoint','restore','destroy','copy','spill','promote']:
                                addop(sp,'session_timing',sp.get('role'),sid,line_no)
        timelines=perf.get('session_timelines') or []
        for tl in timelines:
            sid=tl.get('session_id');row=sessions.setdefault(sid,{'session_id':sid}) if sid else None
            if row is not None:row.update(scalar(tl))
        # Gateway records count completed logical responses even for unfinished Agents.
        gateway_files=list((art/'model-gateway').glob('*.json')) if art else []
        gateway_errors=0
        for f in gateway_files:
            source(f,'model-gateway');items=read(f)
            if not isinstance(items,list):gateway_errors+=1;continue
            for m in items:
                mr={k:v for k,v in scalar(m).items() if k not in ['content_type','request_fingerprint']}
                mr.update(id=key,session_id=f.stem,source_path=str(f),admission=m.get('admission'))
                model_records.append(mr)
        begin=ast.get('started_unix_s') or first
        if begin is None:begin=inv['created']
        end_candidates=[x for x in [last,ast.get('completed_unix_s'),arm_error_time] if isinstance(x,(int,float)) and x>=begin]
        end=min(end_candidates) if end_candidates else begin
        limit=execution.get('arm_timeout_seconds')
        if isinstance(limit,(int,float)) and limit>0:end=min(end,begin+limit)
        duration=max(0,end-begin)
        run_kind='qualification' if inv['run_id'].startswith('qualification-') or 'qualification' in inv['run_id'] else 'smoke' if any(x in inv['run_id'] for x in ['smoke','activation','gate']) else 'training/calibration' if any(x in inv['run_id'] for x in ['training','calibration']) else 'experiment/pilot'
        row={'id':key,'run_id':inv['run_id'],'run_path':str(run),'arm_id':aid,'attempt_id':attempt,'kind':run_kind,'historical_attempt':historical,'state':ast.get('status') or result.get('status'),'run_state':state.get('state'),'policy':policy.get('name'),'concurrency':concurrency,'start_cst':iso(begin),'start_unix_s':begin,'window_end_unix_s':end,'window_seconds':duration,'window_basis':'supervisor arm start to earliest last event/terminal/arm_failed/deadline','event_first_unix_s':first,'event_last_unix_s':last,'cleanup_verified':ast.get('cleanup_verified',correct.get('cleanup_verified')),'failure':compact_error(correct.get('failure') or ast.get('cleanup_error')),'failure_category':result.get('failure_category'),'event_path':str(ef) if ef.is_file() else None,'result_path':str(result_path) if result else None,'frozen_spec_path':str(run/'experiment.yaml'),'artifact_root':str(art) if art else None,'config_file':f'configs/{key}.json','malformed_event_lines':malformed,'events_all_counts':dict(event_counts),'created_sessions_all':len(created_sessions),'gateway_files':len(gateway_files),'gateway_parse_errors':gateway_errors,'reported_performance':scalar(perf),'reported_correctness':scalar(correct),'reported_memory':scalar(result.get('memory')),'reported_cpu':perf.get('experiment_cpu'),'runtime_control':scalar(perf.get('runtime_memory_control')),'provenance':{k:v for k,v in result.get('provenance',{}).items() if k in ['clawbox_commit','kernel','measurement_scope','configured_traces','configured_trace_sha256','configured_trace_reference','prediction_artifact','model_provenance','vm_creation_concurrency','sandbox_create_concurrency','cube_runtime_settings','cgroup_cpu_placement']}}
        if art:
            stderr=art/'worker.stderr.log';source(stderr,'stderr')
            row['stderr_tail']=stderr.read_text(errors='replace')[-1800:] if stderr.exists() else None
        successful_ids={sid for t,sid,valid in completion if valid is True}
        for sid,sr in sessions.items():
            sr.update(id=key,valid_session_complete=sid in successful_ids);rawsessions.write(json.dumps(sr)+'\n')
        for op in ops.values():rawops.write(json.dumps(op)+'\n')
        for m in model_records:rawmodels.write(json.dumps(m)+'\n')
        for e in samples:
            rawsamples.write(json.dumps({'id':key,**e})+'\n')
        def summarize_window(lo,hi,label):
            w={'id':key,'label':label,'from_s':lo-begin,'to_s':hi-begin,'seconds':hi-lo}
            valid=[(t,sid) for t,sid,v in completion if v is True and lo<=t<=hi]
            tools=[t for t in tool_done if lo<=t[0]<=hi];sc=collections.Counter(t[1] for t in tools)
            md=[m for m in model_records if m.get('delivered') is True and isinstance(m.get('delivered_unix_s'),(float,int)) and lo<=m['delivered_unix_s']<=hi]
            w.update(successful_agents=len(valid) if ef.is_file() else None,agent_tool_completions=sc.get('agent-tool',0) if tools or ef.is_file() else None,backend_completions=sc.get('backend-maintenance',0) if ef.is_file() else None,other_tool_completions=sum(n for k,n in sc.items() if k not in ['agent-tool','backend-maintenance']) if ef.is_file() else None,tool_exit_nonzero=sum(t[2] not in [0,None] for t in tools),model_responses_delivered=len(md) if gateway_files else None,independent_failures=sum(lo<=t<=hi for t,*_ in failed))
            for name in ['successful_agents','agent_tool_completions','backend_completions','model_responses_delivered']:
                w[name+'_per_s']=w[name]/(hi-lo) if w[name] is not None and hi>lo else None
            w['steps_agent_tool_plus_model_per_s']=((w['agent_tool_completions']+len(md))/(hi-lo) if gateway_files and w['agent_tool_completions'] is not None and hi>lo else None)
            elapsed=[];offered=[]
            stagger=execution.get('stagger_interval_seconds',0) if execution.get('arrival_schedule')=='fixed_stagger' else 0
            for t,sid in valid:
                sr=sessions.get(sid,{})
                a=sr.get('agent',{});d=a.get('duration_seconds')
                if d is None and sr.get('agent_execution_start') and sr.get('final_agent_completion'):d=sr['final_agent_completion']-sr['agent_execution_start']
                if d is not None:elapsed.append(d)
                try:index=int(sid.rsplit('-',1)[1]);offered.append(t-(first_work or begin)-index*stagger)
                except (ValueError,AttributeError):pass
            w['agent_latency_s']=stats(elapsed);w['offered_to_completion_event_s']=stats(offered)
            ss=sorted([e for e in samples if lo<=ts(e)<=hi],key=ts)
            w['sample_count']=len(ss);w['sample_max_gap_s']=max([ts(b)-ts(a) for a,b in zip(ss,ss[1:])],default=None)
            for field in ['local_used_bytes','combined_live_used_bytes','shared_live_used_bytes','warm_allocated_bytes','warm_reserved_bytes','warm_committed_bytes','shared_borrow_reserved_bytes']:
                v=[(ts(e),e[field]/GIB) for e in ss if isinstance(e.get(field),(int,float))]
                w[field.replace('_bytes','_gib')+'_peak']=max((x for _,x in v),default=None)
                w[field.replace('_bytes','_gib')+'_sample_avg']=statistics.fmean(x for _,x in v) if v else None
                dt=v[-1][0]-v[0][0] if len(v)>1 else 0
                w[field.replace('_bytes','_gib')+'_time_avg']=sum((b[0]-a[0])*(a[1]+b[1])/2 for a,b in zip(v,v[1:]))/dt if dt>0 else None
            cp=[(ts(e),e.get('cgroup_cpu') or {}) for e in ss]
            cp=[(t,c) for t,c in cp if isinstance(c.get('usage_usec'),(int,float))]
            cv=[c['average_cores'] for t,c in cp if isinstance(c.get('average_cores'),(int,float))]
            w['cpu_peak_cores']=max(cv,default=None);w['cpu_sample_avg_cores']=statistics.fmean(cv) if cv else None
            w['cpu_time_avg_cores']=(cp[-1][1]['usage_usec']-cp[0][1]['usage_usec'])/1e6/(cp[-1][0]-cp[0][0]) if len(cp)>1 and cp[-1][0]>cp[0][0] else None
            caps={c.get('capacity_cores') for _,c in cp if c.get('capacity_cores')};w['cpu_capacity_cores']=next(iter(caps)) if len(caps)==1 else None
            for field in ['cpu_time_avg_cores','cpu_peak_cores']:
                w[field.replace('_cores','_pct')]=100*w[field]/w['cpu_capacity_cores'] if w[field] is not None and w['cpu_capacity_cores'] else None
            cg=[e.get('cgroup_memory') or {} for e in ss]
            ev=[x['events'] for x in cg if isinstance(x.get('events'),dict)]
            w['oom_delta']=ev[-1].get('oom',0)-ev[0].get('oom',0) if len(ev)>1 else None
            w['oom_kill_delta']=ev[-1].get('oom_kill',0)-ev[0].get('oom_kill',0) if len(ev)>1 else None
            for category in ['some','full']:
                values=[((x.get('pressure') or {}).get(category) or {}).get('avg10') for x in cg]
                w[f'mem_psi_{category}_avg10_peak']=max((v for v in values if isinstance(v,(float,int))),default=None)
            selected=[o for o in ops.values() if isinstance(o.get('completed_unix_s'),(int,float)) and lo<=o['completed_unix_s']<=hi]
            w['operation_counts']=dict(collections.Counter(o['operation'] for o in selected))
            w['admission_wait_s']=stats([v for t,v in admissions if lo<=t<=hi])
            return w
        if duration>0:
            row['observed']=summarize_window(begin,end,'observed')
            for cap in [300,600,1200]:
                if duration>=cap:windows.append(summarize_window(begin,begin+cap,f'first_{cap}s'))
            for offset in range(0,math.ceil(duration),300):
                windows.append(summarize_window(begin+offset,min(end,begin+offset+300),f'bin_{offset}_{offset+300}'))
        else:row['observed']=None
        row['failure_examples']=[{'time':iso(t),'session_id':sid,'error':err,'type':typ} for t,sid,err,typ in failed[:8]]
        row['all_operation_stats']={}
        group=collections.defaultdict(list)
        for op in ops.values():group[(op['operation'],op['role'],op['status'])].append(op)
        for (operation,role,status),oset in group.items():
            rec={'id':key,'operation':operation,'role':role,'status':status,**stats([o['service_seconds'] for o in oset]),'scope':'all retained lifecycle observations, includes cleanup'}
            operations.append(rec)
        rows.append(row)
    if ni%10==0:print(f'processed {ni+1}/{len(inventories)} runs, {len(rows)} arm attempts',flush=True)
for filename in probe_files:
    r=read(filename)
    if not isinstance(r,dict):continue
    probe_rows.append({'path':filename,'result':r});source(filename,'capability-probe')
for f in [rawops,rawmodels,rawsamples,rawsessions]:f.close()
for name,data in [('arms',rows),('windows',windows),('operation-summary',operations),('pending-arms',pending),('probes',probe_rows),('sources',source_files),('exclusions',excluded),('issues',issues)]:
    (OUT/(name+'.json')).write_text(json.dumps(data,indent=2,ensure_ascii=False))
print(json.dumps({'runs':len(inventories),'attempts':len(rows),'unstarted_arms':len(pending),'probes':len(probe_rows),'issues':len(issues),'out':str(OUT)}),flush=True)
