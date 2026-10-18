"""Build auditable retrospective tables from collected evidence, no experiment imports."""
import collections, csv, gzip, hashlib, json, math, statistics
from pathlib import Path, PurePosixPath

ROOT=Path(__file__).parent; P=ROOT/'clawbox-three-day-review-20261018'
def read(name):return json.loads((P/name).read_text(encoding='utf-8'))
def pct(v,q):
 v=sorted(x for x in v if isinstance(x,(int,float)) and math.isfinite(x))
 if not v:return None
 n=(len(v)-1)*q;lo=int(n);return v[lo]+(v[math.ceil(n)]-v[lo])*(n-lo)
def stats(v):
 v=[x for x in v if isinstance(x,(int,float)) and math.isfinite(x)]
 return dict(n=len(v),mean=statistics.fmean(v) if v else None,p50=pct(v,.5),p90=pct(v,.9),p95=pct(v,.95),p99=pct(v,.99),max=max(v,default=None))
def gj(name):
 with gzip.open(P/name,'rt',encoding='utf-8') as f:
  for line in f:yield json.loads(line)
def div(x,y):return x/y if x is not None and y else None
def flat(x,prefix=''):
 out={}
 if isinstance(x,dict):
  for k,v in x.items():out.update(flat(v,prefix+'.'+k if prefix else k))
 elif isinstance(x,list):out[prefix]=json.dumps(x,ensure_ascii=False,separators=(',',':'))
 else:out[prefix]=x
 return out
def csvout(name,rows,cols=None):
 cols=cols or list(dict.fromkeys(k for r in rows for k in r))
 with (ROOT/name).open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore');w.writeheader()
  for r in rows:w.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in r.items()})

arms=read('arms.json');byid={x['id']:x for x in arms};configs={x['id']:x for x in read('resolved-configs.json')};supp={x['id']:x for x in read('supplement.json')}
early={x['id']:x['early_events'] for x in read('early-events.json')}
tls=collections.defaultdict(list)
for x in read('result-timelines.json'):tls[x['id']].append(x)
sessions=collections.defaultdict(list)
for x in gj('sessions.jsonl.gz'):sessions[x['id']].append(x)
trace_byhash={x.get('sha256_current'):x for x in read('trace-current-copies.json') if x.get('exists')}
trace_rows=[]
for h,t in trace_byhash.items():
 ends=[x for x in t.get('items',[]) if x.get('record_type')=='span_end'];llm=[x for x in ends if x.get('kind')=='llm'];times=[int(x['wall_time_ns'])/1e9 for x in t.get('items',[]) if x.get('wall_time_ns')]
 trace_rows.append({'sha256':h,'current_path':t['path'],'llm_spans':len(llm),'llm_wait_sum_s':sum(int(x['duration_ns'])/1e9 for x in llm if x.get('duration_ns')),'llm_wait_p90_s':pct([int(x['duration_ns'])/1e9 for x in llm if x.get('duration_ns')],.9),'original_wall_extent_s':max(times)-min(times) if times else None,'records':len(t.get('items',[]))})
traceinfo={x['sha256']:x for x in trace_rows}

NOTES={
 'contention-no-admission-c24-v2-m48-01':'旧 drain/cleanup 失败传播；不能据此声称胜过无准入。',
 'contention-no-admission-c24-v2-m48-02':'旧 drain/cleanup 失败传播；保留局部完成数，不能作无准入公平对照。',
 'c1280-screen-c320-01':'旧 VM 创建路径瓶颈；局部完成数据可保留，排除正式策略比较。',
 'c1280-screen-c320-02':'旧 VM 创建路径瓶颈；部分成功可见，未完成完整对照。',
 'c1280-screen-c320-03':'旧 VM 创建路径/创建 gate 过高；排除正式策略比较。',
 'c1280-priority-2h-01':'供应 VM 阶段超时；c1280 未达到完整供给，不能代表稳态 c1280。',
 'c1280-screen-c320-04':'文件描述符耗尽，内存观测可靠性受影响。',
 'c1280-screen-c320-05':'shared-return 生命周期竞争导致策略控制器失败。',
 'c1280-screen-c320-os-06':'合成十阶段 trace；3600 s 超时边界，0 成功，无 OOM；不是无限时长的最终成功率。',
 'c1280-screen-c320-predicted-06':'合成十阶段 trace；完整完成；borrow/checkpoint 未激活，不能归功于它们。',
 'c1280-primary-c640-07':'v5 trace 缺少验证要求的 completion marker；性能对照无效。',
 'c1280-primary-c640-08-v6':'合成四阶段 v6，和 c320 v3 不同，不能直接当作同一工作负载的扩展曲线。',
 'c320-admission-ablation-01':'用户终止的合成负载实验；308 个已验证完成仍保留，非完整消融。',
 'real-trace-p0-c320-01':'原始日志不支持旧 112 完成说法；窗口仅 0 成功。',
 'real-trace-p0-c320-l96-01':'极紧水位造成 pause/restore churn；诊断边界。',
 'real-trace-p0-c320-pair-l160-01':'NUMA migration 隐含 30 s 限制导致 controller 失败。',
 'real-trace-p0-c320-pair-l160-02':'并发创建 16 与 trace/checkpoint 叠加出现创建失败；随后改为 8。',
 'real-trace-p0-c320-pair-l160-03':'Cube tombstone 残留与 borrow 预留记账问题。',
 'real-trace-p0-c320-pair-l160-04':'暂停后清理 populated cgroup 破坏 tombstone 恢复；后续已撤销。',
 'real-trace-p0-c320-pair-l160-05':'失败 Agent 的 model-gateway drain 异常终止整臂；大量动作仅证明激活。',
 'real-trace-p0-c320-pair-l160-06':'Full 的远端 policy.py 过旧，两个 controller failure；A-off 提前终止。',
 'real-trace-p0-c320-remaining-l160-01':'A-off 到达 2400 s；无成功，存在独立失败；高 checkpoint 活跃度，非收益证据。',
 'real-trace-p0-c640-mild-l640-01':'绕过 wrapper 继承 FD=1024，EMFILE，基础设施无效点。',
 'real-trace-p0-c640-mild-l640-02':'残留 qualification VM 导致 preflight 失败，未开始负载。',
 'real-trace-p0-c640-mild-l640-03':'0.5 s 到达，Cube 生命周期/I/O/创建排队过载；统计截到配置执行期限，最终化另计。',
 'real-trace-p0-c640-balanced-full-l512-01':'1.0 s 到达仍有 HTTP 408/502/envd 错误；未完成 Agent。',
 'real-trace-p0-c640-paced-full-l352-01':'首次主机重启；恢复后 TAP 池 500 不足；两个 attempt 分开保留。',
 'real-trace-p0-c640-paced-full-l352-02':'过于保守的准入边界；50 次 VM checkpoint，可用于机制激活与排队诊断。',
 'real-trace-p0-c640-relaxed-full-l640-01':'S3lvol missing chunks/flush 失败；不能作策略对照。',
 'real-trace-p0-c640-relaxed-full-l640-02':'执行期间 0 成功；最终化 model-gateway 未 drain；保留窗口数据，不认定完整结果。',
 'real-trace-p0-c640-main-full-l896-01':'到达爬坡期提前终止；不能推断全部 640 到达后的争用。',
 'real-trace-p0-c640-balanced-full-s075-01':'1200 s 窗口内按计划约 600/640 到达；0 成功；旧 forecast gate，borrow 实测成本远高于估计。',
 'real-trace-p0-c640-balanced-aoff-s075-01':'仅关闭工具准入，其他机制保留；不等于 OS-default；0 成功。',
 'real-trace-p0-c640-balanced-os-s075-01':'全部 ClawBox 策略关闭；0 成功，失败含创建/envd 初始化超时；最终化异常。',
}

metrics=[];config_rows=[];config_long=[];index=[]
for row in arms:
 key=row['id'];c=configs[key];a=c['resolved_for_review'];f=c['frozen_spec'];r=a.get('resources') or {};x=a.get('execution') or {};pol=a.get('policy') or {};m=pol.get('mechanisms',f.get('mechanisms',{}));w=row.get('observed') or {};perf=row['reported_performance'];sup=supp[key];prov=sup.get('provenance') or {};inf=a.get('inference') or {};ic=inf.get('configuration',{});ev=row['events_all_counts'];ops=w.get('operation_counts',{})
 row['policy']=pol.get('name');row['concurrency']=a.get('concurrency',row['concurrency']);name=row['run_id'];notes=[NOTES.get(name,'')]
 if row['kind']=='qualification':notes.append('资格/功能验证，不混入正式性能比较。')
 if row['kind']=='training/calibration':notes.append('训练/校准，不混入测试性能比较。')
 if row['kind']=='smoke':notes.append('小规模功能/激活检查，不等于规模性能实验。')
 if row['run_state'] in ['cancelled','failed','timed_out'] and row['state']=='running':notes.append('arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。')
 if name.startswith('real-trace-p0'):notes.append('历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。')
 if (w.get('cpu_peak_pct') or 0)>100:notes.append('CPU 采样峰值超过记录 CPU 容量，保留异常值，不能当作可信瞬时利用率。')
 if (w.get('sample_max_gap_s') or 0)>30:notes.append('采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。')
 if any(ev.get(k) for k in ['policy_event_failed','memory_watermark_controller_failed']):notes.append('有 policy/controller failure，机制归因受影响。')
 if not row['event_path']:notes.append('缺少原始 event 文件，缺失指标未补零。')
 if c['resolution']!='saved effective arm':notes.append('配置恢复方法：'+c['resolution'])
 notes=' '.join(z for z in notes if z)
 # Saved timelines represent sessions returned by the worker; report numerator alongside quantiles.
 tt=tls[key]; offered=[t['final_agent_completion']-t['offered_start_unix_s'] for t in tt if t.get('final_agent_completion') and t.get('offered_start_unix_s')]
 offeredstats=stats(offered)
 endw=row['window_end_unix_s'];startw=row['start_unix_s']
 ss=[s for s in sessions[key] if s.get('valid_session_complete') and (s.get('session') or {}).get('end_unix_s',float('inf'))<=endw]
 requestvalidate=[s['validation']['end_unix_s']-s['sandbox.create.queue']['start_unix_s'] for s in ss if s.get('validation') and s.get('sandbox.create.queue')]
 queues=stats([(s.get('sandbox.create.queue') or {}).get('duration_seconds') for s in sessions[key]])
 spancreate=stats([(s.get('sandbox.create') or {}).get('duration_seconds') for s in sessions[key]])
 trace=prov.get('configured_trace_sha256');manifest=(sup.get('qualification_manifest') or {}).get('traces',[])
 if not trace and len(manifest)==1:trace=manifest[0].get('sha256')
 ti=traceinfo.get(trace,{})
 forecast=ic.get('model_wait_prediction_seconds',f.get('evidence',{}).get('model_wait_prediction_seconds'))
 cfg={'id':key,'run_id':name,'policy':row['policy'],'concurrency':row['concurrency'],'case_id':(a.get('case') or {}).get('case_id'),'config_resolution':c['resolution'],'trace_path':(a.get('case') or {}).get('replay_trace_reference'),'trace_sha256_recorded':trace,'trace_copy_hash_verified':bool(ti) if trace else None,'recorded_llm_spans':ti.get('llm_spans'),'recorded_llm_wait_sum_s':ti.get('llm_wait_sum_s'),'replay_time_scale':ic.get('time_scale'),'legacy_llm_forecast_s':forecast,'local_capacity_GiB':div(r.get('local_memory_capacity_mib'),1024),'local_LOW_GiB':div(r.get('local_memory_low_watermark_mib'),1024),'local_HIGH_GiB':div(r.get('local_memory_high_watermark_mib'),1024),'admission_pool_GiB':div(r.get('pool_memory_budget_mib'),1024),'physical_live_limit_GiB':div(r.get('physical_live_limit_mib'),1024),'warm_capacity_GiB':div(r.get('warm_memory_capacity_mib'),1024),'borrow_cap_GiB':div(r.get('shared_memory_borrow_limit_mib'),1024),'emergency_free_GiB':div(r.get('emergency_free_memory_mib'),1024),'runtime_guest_GiB':div((a.get('runtime') or {}).get('memory_mib'),1024),'tool_guest_GiB':div((a.get('sandbox') or {}).get('memory_mib'),1024),'runtime_vCPU':(a.get('runtime') or {}).get('vcpu'),'tool_vCPU':(a.get('sandbox') or {}).get('vcpu'),'runtime_template':(a.get('runtime') or {}).get('template_id'),'tool_template':(a.get('sandbox') or {}).get('template_id'),'compute_nodes':r.get('compute_nodes'),'warm_numa_node':r.get('warm_numa_node'),'sample_interval_s':x.get('memory_sample_interval_seconds',f.get('measurement',{}).get('memory_sample_interval_seconds')),'stagger_s':x.get('stagger_interval_seconds'),'arrival_schedule':x.get('arrival_schedule'),'timeout_s':x.get('arm_timeout_seconds'),'finalization_timeout_s':x.get('arm_finalization_timeout_seconds'),'tool_timeout_s':x.get('command_timeout_seconds'),'admission':pol.get('admission',m.get('admission',{}).get('tool')),'prediction_scale':r.get('prediction_reservation_scale',m.get('admission',{}).get('prediction_reservation_scale')),'static_tool_MiB':r.get('static_tool_memory_mib',m.get('admission',{}).get('static_tool_memory_mib')),'admission_margin_MiB':r.get('admission_margin_mib',m.get('admission',{}).get('admission_margin_mib')),'startup_headroom_MiB':r.get('startup_headroom_mib',m.get('admission',{}).get('startup_headroom_mib')),'restore_headroom_MiB':r.get('checkpoint_restore_headroom_mib',m.get('idle_reclaim',{}).get('checkpoint_restore_headroom_mib')),'idle_policy':m.get('idle_reclaim',{}).get('policy',pol.get('eviction')),'idle_scope':m.get('idle_reclaim',{}).get('scope'),'runtime_snapshot':pol.get('runtime_snapshot'),'borrow_policy':m.get('shared_memory',{}).get('borrow'),'memory_scheduling':r.get('memory_scheduling'),'lifecycle_admission':r.get('lifecycle_admission'),'snapshot_mechanism':r.get('snapshot_mechanism',m.get('checkpoint',{}).get('mechanism')),'snapshot_storage':r.get('snapshot_storage',m.get('checkpoint',{}).get('storage')),'transfer_concurrency':r.get('pool_transfer_concurrency',m.get('checkpoint',{}).get('pool_transfer_concurrency')),'transfer_timeout_s':r.get('pool_transfer_timeout_seconds',m.get('checkpoint',{}).get('pool_transfer_timeout_seconds')),'placement_policy':x.get('placement_policy',m.get('placement',{}).get('policy')),'runtime_control':pol.get('runtime_control'),'protection_enabled':pol.get('memory_protection',m.get('runtime',{}).get('memory_protection')),'boost_enabled':pol.get('cpu_release_boost',m.get('runtime',{}).get('cpu_release_boost')),'proactive_reclaim':r.get('proactive_cache_reclaim',m.get('runtime',{}).get('proactive_memory_reclaim')),'prediction_artifact':r.get('prediction_artifact',f.get('evidence',{}).get('prediction_artifact')),'prediction_provenance':prov.get('prediction_artifact'),'cube_effective_capacity_settings':prov.get('cube_runtime_settings'),'recorded_commit':prov.get('clawbox_commit'),'qualification_implementation_digest':sup.get('implementation_digest'),'frozen_config_path':row['frozen_spec_path'],'local_config_file':row['config_file']}
 config_rows.append(cfg)
 for k,v in flat(a).items():config_long.append({'id':key,'run_id':name,'policy':row['policy'],'field':k,'value':v,'resolution':c['resolution']})
 met={'id':key,'start_CST':row['start_cst'],'run_id':name,'policy':row['policy'],'c':row['concurrency'],'kind':row['kind'],'run_state':row['run_state'],'arm_state_saved':row['state'],'result_status':sup.get('result_status'),'window_s':w.get('seconds'),'success_agents':w.get('successful_agents'),'Agents_per_s':w.get('successful_agents_per_s'),'Tool_Steps_per_s':w.get('agent_tool_completions_per_s'),'Model_responses_per_s':w.get('model_responses_delivered_per_s'),'Tool_plus_Model_per_s':w.get('steps_agent_tool_plus_model_per_s'),'Backend_commands_per_s':w.get('backend_completions_per_s'),'reported_Agents_per_s':div(perf.get('agents_per_minute'),60),'reported_Steps_per_s':div(perf.get('steps_per_minute'),60),'reported_workload_window_s':perf.get('workload_window_seconds'),'P90_agent_s':(w.get('agent_latency_s') or {}).get('p90'),'P90_agent_n':(w.get('agent_latency_s') or {}).get('n'),'P90_offered_to_agent_end_s':offeredstats['p90'],'offered_latency_n':offeredstats['n'],'P95_offered_reported_s':perf.get('offered_to_completion_p95_seconds'),'P90_request_to_validation_s':pct(requestvalidate,.9),'request_validation_n':len(requestvalidate),'P90_request_to_cleanup_event_s':(w.get('offered_to_completion_event_s') or {}).get('p90'),'CPU_avg_pct':w.get('cpu_time_avg_pct'),'CPU_peak_pct':w.get('cpu_peak_pct'),'CPU_avg_cores':w.get('cpu_time_avg_cores'),'CPU_peak_cores':w.get('cpu_peak_cores'),'CPU_capacity_cores':w.get('cpu_capacity_cores'),'LOCAL_avg_GiB':w.get('local_used_gib_time_avg'),'LOCAL_sample_mean_GiB':w.get('local_used_gib_sample_avg'),'LOCAL_peak_GiB':w.get('local_used_gib_peak'),'combined_live_peak_GiB':w.get('combined_live_used_gib_peak'),'shared_live_peak_GiB':w.get('shared_live_used_gib_peak'),'WARM_allocated_peak_GiB':w.get('warm_allocated_gib_peak'),'WARM_reserved_peak_GiB':w.get('warm_reserved_gib_peak'),'WARM_committed_peak_GiB':w.get('warm_committed_gib_peak'),'borrow_reserved_peak_GiB':w.get('shared_borrow_reserved_gib_peak'),'borrow_n':ops.get('borrow',0) if row['event_path'] else None,'return_n':ops.get('return',0) if row['event_path'] else None,'checkpoint_VM_n':ops.get('checkpoint',0) if row['event_path'] else None,'restore_VM_n':ops.get('restore',0) if row['event_path'] else None,'create_VM_n':ops.get('create',0) if row['event_path'] else None,'independent_failures':w.get('independent_failures'),'tool_completions':w.get('agent_tool_completions'),'backend_completions':w.get('backend_completions'),'unclassified_tool_completions':w.get('other_tool_completions'),'nonzero_exit_commands_all_scopes':w.get('tool_exit_nonzero'),'OOM_delta':w.get('oom_delta'),'OOM_kill_delta':w.get('oom_kill_delta'),'memory_PSI_some_avg10_peak':w.get('mem_psi_some_avg10_peak'),'memory_PSI_full_avg10_peak':w.get('mem_psi_full_avg10_peak'),'sample_n':w.get('sample_count'),'sample_max_gap_s':w.get('sample_max_gap_s'),'admitted_wait_P90_s':(w.get('admission_wait_s') or {}).get('p90'),'admitted_wait_P99_s':(w.get('admission_wait_s') or {}).get('p99'),'create_queue_P90_s':queues['p90'],'session_pair_create_P90_s':spancreate['p90'],'policy_failure_events':ev.get('policy_event_failed',0),'controller_failure_events':ev.get('memory_watermark_controller_failed',0),'runtime_event_counts':{k:v for k,v in ev.items() if any(s in k for s in ['boost','protect','reclaim','pressure_'])},'cleanup_verified':row['cleanup_verified'],'failure':row['failure'],'failure_examples':row['failure_examples'],'notes':notes,'event_path':row['event_path'],'result_path':row['result_path'],'config_file':row['config_file']}
 metrics.append(met)
 met['Unclassified_commands_per_s']=div(w.get('other_tool_completions'),w.get('seconds'))
 met['All_commands_per_s']=div(sum(w.get(k) or 0 for k in ['agent_tool_completions','backend_completions','other_tool_completions']),w.get('seconds')) if row['event_path'] else None
 if (w.get('other_tool_completions') or 0)>0:
  met['notes']+=' 存在未标注 execution_scope 的旧 tool_completed；未分类命令吞吐作为替代，不能全算应用 Tool。'
  if w.get('agent_tool_completions')==0:met['Tool_Steps_per_s']=None;met['Tool_plus_Model_per_s']=None
 met['VM_cgroup_avg_GiB']=w.get('combined_live_used_gib_time_avg')
 met['VM_cgroup_peak_GiB']=w.get('combined_live_used_gib_peak')
 met['LOCAL_memory_scope']='logged LOCAL sampler (see effective configuration); NUMA tiers where available'
 if pol.get('deployment')=='os-default' or m.get('deployment',{}).get('mode')=='os-default':
  met['logged_local_avg_GiB']=met['LOCAL_avg_GiB'];met['logged_local_peak_GiB']=met['LOCAL_peak_GiB']
  met['LOCAL_avg_GiB']=None;met['LOCAL_sample_mean_GiB']=None;met['LOCAL_peak_GiB']=None
  met['LOCAL_memory_scope']='unavailable: OS-default local_used_bytes is whole VM cgroup charge, not NUMA LOCAL'
  met['notes']+=' OS-default 的 local_used_bytes 实为整个 VM cgroup，不能解释为 NUMA 0/1 LOCAL；移入 VM_cgroup 列。'
 index.append({'id':key,'run_id':name,'policy':row['policy'],'attempt_id':row['attempt_id'],'arm_id':row['arm_id'],'run_path':row['run_path'],'event_path':row['event_path'],'result_path':row['result_path'],'config_path':row['frozen_spec_path'],'gateway_dir':str(PurePosixPath(row['artifact_root'])/'model-gateway') if row['artifact_root'] else None,'worker_stderr':str(PurePosixPath(row['artifact_root'])/'worker.stderr.log') if row['artifact_root'] else None,'local_config_file':row['config_file'],'qualification_path':sup.get('qualification_path')})

# Operation-level export retains exact timestamps and bytes, with separate window/all scope.
opgroups=collections.defaultdict(list);opdetail=[]
for o in gj('operations.jsonl.gz'):
 row=byid[o['id']];stop=row['window_end_unix_s'];start=row['start_unix_s'];inside=isinstance(o.get('completed_unix_s'),(int,float)) and start<=o['completed_unix_s']<=stop
 detail={**o,'run_id':row['run_id'],'policy':row['policy'],'within_observation_window':inside};opdetail.append(detail)
 for label in ['all_retained']+(['observation_window'] if inside else []):opgroups[(o['id'],o['operation'],o['role'],o['status'],label)].append(o)
operation_rows=[]
for (key,op,role,status,scope),vv in opgroups.items():
 d={'id':key,'run_id':byid[key]['run_id'],'policy':byid[key]['policy'],'operation':op,'role':role,'status':status,'scope':scope,**stats([o['service_seconds'] for o in vv])}
 for field in ['allocated_bytes','transferred_bytes','dirty_bytes','logical_bytes']:
  v=[(o.get('snapshot_metrics') or {}).get(field) for o in vv];st=stats(v);d[field+'_n']=st['n'];d[field+'_avg_MiB']=div(st['mean'],1024**2);d[field+'_peak_MiB']=div(st['max'],1024**2)
 operation_rows.append(d)
for met in metrics:
 for op,field in [('borrow','borrow_n'),('return','return_n'),('checkpoint','checkpoint_VM_n'),('restore','restore_VM_n'),('create','create_VM_n')]:
  attempted=met[field];met[field+'_attempts']=attempted
  if attempted is not None:
   successful=sum(z['n'] for z in operation_rows if z['id']==met['id'] and z['scope']=='observation_window' and z['operation']==op and z['status']=='ok')
   met[field]=successful;met[field+'_failed_or_other']=attempted-successful
csvout('operation-details.csv',opdetail)
probe_rows=[];probe_operations=[]
for pr in read('probes.json'):
 r=pr['result'];path=pr['path'];probe_rows.append({'path':path,'passed':r.get('passed'),'required':r.get('required'),'failure':r.get('failure'),'mechanism':r.get('mechanism'),'storage':r.get('storage'),'pair_checkpoint_verified':r.get('pair_checkpoint_verified'),'incremental_delta_verified':r.get('incremental_delta_verified'),'cleanup_errors':r.get('cleanup_errors')})
 for field,role in [('timings','tool'),('runtime_timings','runtime')]:
  for t in r.get(field,[]) or []:
   if isinstance(t,dict) and t.get('operation'):probe_operations.append({'path':path,'role':role,**flat(t)})
 for op in ['borrow','return','reborrow','second_return']:
  if isinstance(r.get(op),dict):probe_operations.append({'path':path,'role':'tool','operation':op,**flat(r[op])})
csvout('probe-operations.csv',probe_operations)
windows=[]
for w in read('windows.json'):
 rec={'run_id':byid[w['id']]['run_id'],'policy':byid[w['id']]['policy'],'c':byid[w['id']]['concurrency'],**flat(w)}
 rec['unclassified_commands_per_s']=div(w.get('other_tool_completions'),w.get('seconds'))
 if (w.get('other_tool_completions') or 0)>0 and w.get('agent_tool_completions')==0:rec['agent_tool_completions_per_s']=None;rec['steps_agent_tool_plus_model_per_s']=None
 windows.append(rec)
tables={'Experiments':[x for x in metrics if x['kind']=='experiment/pilot'],'Other_runs':[x for x in metrics if x['kind']!='experiment/pilot'],'Configurations':config_rows,'Operations':operation_rows,'Windows':windows,'Probes':probe_rows,'Sources':index,'Traces':trace_rows}
for name,rows in tables.items():csvout(name+'.csv',rows)
csvout('all-config-fields.csv',config_long)
csvout('source-file-manifest.csv',read('sources.json'))
csvout('unstarted-arms.csv',read('pending-arms.json'))
csvout('Runtime-control.csv',[{'id':r['id'],'run_id':r['run_id'],'policy':r['policy'],**r['runtime_control'],'observed_runtime_events':{k:v for k,v in r['events_all_counts'].items() if any(s in k for s in ['boost','protect','reclaim','pressure_'])}} for r in arms])
csvout('Reported-metrics.csv',[{'id':r['id'],'run_id':r['run_id'],'policy':r['policy'],**r['reported_performance'],**{'memory.'+k:v for k,v in r['reported_memory'].items()},**{'cpu.'+k:v for k,v in (r['reported_cpu'] or {}).items() if not isinstance(v,(dict,list))}} for r in arms])
(ROOT/'tables.json').write_text(json.dumps(tables,ensure_ascii=False,indent=2),encoding='utf-8')
(ROOT/'metrics.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'tables':{k:len(v) for k,v in tables.items()},'operation_detail_rows':len(opdetail),'probe_operation_rows':len(probe_operations),'config_fields':len(config_long)},ensure_ascii=False))
