import fs from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';
const data = JSON.parse(await fs.readFile(new URL('./tables.json', import.meta.url), 'utf8'));
const workbook = Workbook.create();
const out = new URL('./', import.meta.url);
const safe = v => v == null ? null : typeof v === 'object' ? JSON.stringify(v) : typeof v === 'string' && /^\d{4}-\d\d-\d\dT.*\+08:00$/.test(v) ? 'CST '+v.replace('T',' ').replace('+08:00','') : typeof v === 'string' && v.startsWith('=') ? "'"+v : v;
const configs = new Map(data.Configurations.map(r=>[r.id,r]));
const keyRuns = new Set(['contention-command-table-c28-v2-01','contention-command-table-c28-v2-m64-01','contention-command-table-c24-v2-m48-01','c1280-screen-c320-os-06','c1280-screen-c320-predicted-06','c1280-primary-c640-08-v6','real-trace-p0-c320-remaining-l160-01','real-trace-p0-c640-paced-full-l352-02','real-trace-p0-c640-relaxed-full-l640-02','real-trace-p0-c640-main-full-l896-01','real-trace-p0-c640-balanced-full-s075-01','real-trace-p0-c640-balanced-aoff-s075-01','real-trace-p0-c640-balanced-os-s075-01']);
const highlights=data.Experiments.filter(r=>keyRuns.has(r.run_id)).map(r=>({'实验':r.run_id,'策略':r.policy,'并发':r.c,'窗口秒':r.window_s,'成功Agent':r.success_agents,'Agents/s':r.Agents_per_s,'Tool Steps/s':r.Tool_Steps_per_s,'P90执行秒':r.P90_agent_s,'P90到达至结束秒':r.P90_offered_to_agent_end_s,'CPU均值%':r.CPU_avg_pct,'CPU峰值%':r.CPU_peak_pct,'LOCAL均值GiB':r.LOCAL_avg_GiB,'LOCAL峰值GiB':r.LOCAL_peak_GiB,'借用次数':r.borrow_n,'VM快照次数':r.checkpoint_VM_n,'VM恢复次数':r.restore_VM_n,'LOW/HIGH GiB':`${configs.get(r.id).local_LOW_GiB??'—'} / ${configs.get(r.id).local_HIGH_GiB??'—'}`,'解释':r.notes,'证据ID':r.id}));
const guide=[
 ['范围','北京时间 2026-10-16 至 2026-10-18；只读审计，没有运行新实验。220 个目录、342 个已启动 arm/attempt，73 个实验/探索点，269 个训练/校准/资格/功能检查。'],
 ['阅读顺序','重点结果 → Experiments → Configurations → Operations / Windows → Sources。完整长字段及单次操作见相邻 CSV 和 evidence 数据目录。'],
 ['Agents/s','窗口内 valid=true 的 session_complete 数量 / 窗口秒。包含启动、排队；未完成和失败 Agent 不算成功。'],
 ['Tool Steps/s','agent-tool scope 的 tool_completed / 窗口秒；包括非零退出，且可能包含高层 trace 工具的子操作。后台维护另列，不混入。'],
 ['Model 与合计 Steps/s','Model_responses_per_s 为交付的模型响应；Tool_plus_Model_per_s 是这两个事件计数之和。reported_Steps_per_s 是原结果自己的成功会话/工作负载窗口口径，不能直接混用。'],
 ['P90','P90_agent_s 从 Agent 执行开始到结束，排除创建/排队；P90_offered_to_agent_end_s 使用保存的 offered_start 和 final_agent_completion。后者缺失时给 request-to-validation / cleanup 替代值，定义不同。'],
 ['时延缺失','没有成功 Agent 时成功时延留空。完成子集的 P90 不是全部请求 P90；失败和未完成请求被删失。'],
 ['CPU','累计 cgroup usage_usec 的首末差 / 采样覆盖时长 / 记录的 CPU 容量。峰值是日志采样区间最大值，不是真正瞬时峰值。异常 >100% 保留并注明。'],
 ['LOCAL','均值为首末有效样本间梯形积分估计。OS-default 的 local_used_bytes 实为整个 VM cgroup，LOCAL 留空并给 VM_cgroup 替代值；不能与 Full 的 NUMA LOCAL 直接比较。'],
 ['内存单位','GiB=2^30 bytes；原始配置 MiB=2^20 bytes。LOCAL 容量、LOW/HIGH、水池准入预算、WARM 容量、borrow cap 分列。'],
 ['窗口与稳定性','observed 截到执行期限/失败/末事件；无法精确恢复手工停止时刻的取消臂可能包含取消收敛。Windows 提供固定前 300/600/1200 秒和每 300 秒分箱，不宣称早期吞吐已经稳定。'],
 ['操作时延','主表动作次数只计 status=ok；失败/尝试另存。Operations 按角色、操作、状态统计 n/mean/P50/P90/P95/P99/max；窗口与全日志分开。单次操作时间戳见 operation-details.csv。'],
 ['快照与拷贝','checkpoint service_seconds 包含整个快照操作，不能冒充纯 memcpy/copy 时间。日志有 transferred/dirty/allocated bytes；没有独立 copy 计时则留空。'],
 ['历史配置','289 臂有保存的 effective arm；51 臂以历史 policy 身份 SHA256 匹配 arm_id；2 臂仅有唯一声明配置。绝不使用今天的默认值重编译历史配置。'],
 ['旧 idle 策略','real-trace-p0 系列包含 2.7181621785 秒 forecast 过期门控。checkpoint 未触发不能评价修复后的 actual-idle 策略；已触发也不自动证明性能收益。'],
 ['结果边界','训练和资格检查不计作正式消融；借用/快照激活不等于正收益。合成 c320 v3 和 c640 v6 trace 不同。未启动的 132 个配置臂只在 unstarted-arms.csv 中列出。'],
 ['原始日志','Sources 给出 ssh kunpeng 上每臂 event/result/experiment.yaml/model-gateway/stderr 的绝对路径；所有原文件保持不变。source-file-manifest.csv 保存大小和修改时间。'],
 ['完整性限制','只覆盖已找到并保留 run-state + experiment.yaml 的运行及探针。未记录的进程环境变量、历史有效 Cube 配额、独立 copy 延迟不能逆推。'],
];
function col(n){let s='';for(n++;n;n=Math.floor((n-1)/26))s=String.fromCharCode(65+(n-1)%26)+s;return s;}
let tableNumber=0;
function add(name,rows,columns){
 const sheet=workbook.worksheets.add(name);sheet.showGridLines=false;
 const keys=columns||[...new Set(rows.flatMap(r=>Object.keys(r)))];
 const values=[keys,...rows.map(r=>keys.map(k=>safe(r[k])))];
 const range=sheet.getRangeByIndexes(0,0,values.length,keys.length);range.values=values;
 range.format.font.name='Microsoft YaHei';range.format.font.size=10;range.format.rowHeight=20;range.format.columnWidth=17;
 const header=sheet.getRangeByIndexes(0,0,1,keys.length);header.format.fill='#17365D';header.format.font.color='#FFFFFF';header.format.font.bold=true;header.format.wrapText=true;header.format.rowHeight=44;
 sheet.freezePanes.freezeRows(1);sheet.freezePanes.freezeColumns(name==='重点结果'?2:1);
 if(rows.length)sheet.tables.add(`A1:${col(keys.length-1)}${rows.length+1}`,true,`ReviewTable${++tableNumber}`);
 keys.forEach((k,j)=>{const rr=sheet.getRangeByIndexes(1,j,Math.max(1,rows.length),1);if(rows.some(r=>typeof r[k]==='number'))rr.setNumberFormat(k.includes('per_s')||k.includes('Steps/s')||k==='Agents/s'?'0.0000':k==='c'||k==='n'||k==='concurrency'||k.includes('_n')||k.includes('次数')||k==='并发'||k==='成功Agent'?'0':'0.00');if(k.includes('run_id')||k==='实验'||k==='path')sheet.getRangeByIndexes(0,j,values.length,1).format.columnWidth=48;if(k.endsWith('_path')||k.endsWith('_dir'))sheet.getRangeByIndexes(0,j,values.length,1).format.columnWidth=54;if(k.includes('notes')||k==='解释')sheet.getRangeByIndexes(0,j,values.length,1).format.columnWidth=70;});
 return sheet;
}
const readme=add('说明',guide.map(([项目,说明])=>({项目,说明})),['项目','说明']);readme.getRange('A:A').format.columnWidth=24;readme.getRange('B:B').format.columnWidth=115;readme.getRange(`A2:B${guide.length+1}`).format.wrapText=true;readme.getRange(`A2:B${guide.length+1}`).format.rowHeight=49;
add('重点结果',highlights);
const perfcols=['run_id','policy','c','window_s','success_agents','Agents_per_s','Tool_Steps_per_s','Model_responses_per_s','Tool_plus_Model_per_s','P90_agent_s','P90_offered_to_agent_end_s','CPU_avg_pct','CPU_peak_pct','LOCAL_avg_GiB','LOCAL_peak_GiB','borrow_n','checkpoint_VM_n','restore_VM_n','independent_failures','WARM_allocated_peak_GiB','combined_live_peak_GiB','sample_max_gap_s','P90_request_to_validation_s','request_validation_n','P90_request_to_cleanup_event_s','admitted_wait_P90_s','admitted_wait_P99_s','create_queue_P90_s','session_pair_create_P90_s','reported_Agents_per_s','reported_Steps_per_s','reported_workload_window_s','Backend_commands_per_s','OOM_delta','OOM_kill_delta','policy_failure_events','controller_failure_events','cleanup_verified','start_CST','run_state','arm_state_saved','failure','notes','event_path','id'];
perfcols.splice(15,0,'VM_cgroup_avg_GiB','VM_cgroup_peak_GiB','LOCAL_memory_scope');
perfcols.splice(9,0,'Unclassified_commands_per_s','All_commands_per_s');
add('Experiments',data.Experiments,perfcols);add('Other_runs',data.Other_runs,perfcols);
add('Configurations',data.Configurations,['run_id','policy','concurrency','case_id','local_capacity_GiB','local_LOW_GiB','local_HIGH_GiB','admission_pool_GiB','physical_live_limit_GiB','warm_capacity_GiB','borrow_cap_GiB','runtime_guest_GiB','tool_guest_GiB','stagger_s','prediction_scale','recorded_llm_wait_sum_s','legacy_llm_forecast_s',...Object.keys(data.Configurations[0]).filter(k=>!['run_id','policy','concurrency','case_id','local_capacity_GiB','local_LOW_GiB','local_HIGH_GiB','admission_pool_GiB','physical_live_limit_GiB','warm_capacity_GiB','borrow_cap_GiB','runtime_guest_GiB','tool_guest_GiB','stagger_s','prediction_scale','recorded_llm_wait_sum_s','legacy_llm_forecast_s'].includes(k))]);
add('Operations',data.Operations,['run_id','policy','operation','role','status','scope','n','mean','p50','p90','p95','p99','max','transferred_bytes_avg_MiB','allocated_bytes_avg_MiB','dirty_bytes_avg_MiB','logical_bytes_avg_MiB','id']);
add('Windows',data.Windows,['run_id','policy','c','label','from_s','to_s','seconds','successful_agents','successful_agents_per_s','agent_tool_completions_per_s','model_responses_delivered_per_s','steps_agent_tool_plus_model_per_s','backend_completions_per_s','cpu_time_avg_pct','cpu_peak_pct','local_used_gib_time_avg','local_used_gib_peak','warm_allocated_gib_peak','sample_count','sample_max_gap_s','independent_failures','operation_counts','id']);
add('Probes',data.Probes);add('Sources',data.Sources,['run_id','policy','attempt_id','arm_id','run_path','event_path','result_path','config_path','gateway_dir','worker_stderr','local_config_file','qualification_path','id']);add('Traces',data.Traces);
workbook.recalculate();
await fs.writeFile(new URL('workbook-inspect.json',out),JSON.stringify(await workbook.inspect({kind:'sheet,table',maxChars:4500,tableMaxRows:2,tableMaxCols:5}),null,2));
await fs.writeFile(new URL('workbook-check.json',out),JSON.stringify(await workbook.inspect({kind:'region',sheetId:'重点结果',range:'A1:I8',maxChars:3500,tableMaxRows:8,tableMaxCols:9}),null,2));
const xlsx=await SpreadsheetFile.exportXlsx(workbook);await xlsx.save(fileURLToPath(new URL('ClawBox-三天实验审计.xlsx',out)));
console.log('xlsx exported');
for(const name of ['说明','重点结果','Experiments','Other_runs','Configurations','Operations','Windows','Probes','Sources','Traces']){
 try {const img=await workbook.render({sheetName:name,range:name==='说明'?'A1:B6':'A1:I7',scale:1,format:'png'});await fs.writeFile(new URL(`preview-${name}.png`,out),new Uint8Array(await img.arrayBuffer()));console.log('rendered',name);}catch(e){console.log('render issue',name,e.message);}
}
