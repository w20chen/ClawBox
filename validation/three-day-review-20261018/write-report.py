import collections,json
from pathlib import Path
P=Path(__file__).parent
t=json.loads((P/'tables.json').read_text(encoding='utf-8'));a=t['Experiments'];cfg={r['id']:r for r in t['Configurations']}
def fmt(v,n=2):
 if v is None:return '—'
 if isinstance(v,bool):return str(v)
 if isinstance(v,int):return str(v)
 if isinstance(v,float):return f'{v:.{n}f}'
 return str(v).replace('|','／').replace('\n',' ')
def table(headers,rows):return '| '+' | '.join(headers)+' |\n|'+'|'.join(['---']*len(headers))+'|\n'+''.join('| '+' | '.join(fmt(v) for v in row)+' |\n' for row in rows)+'\n'
def choose(names):return [r for r in a if r['run_id'] in names]
aliases={'contention-command-table-c28-v2-01':'c28 / 80 GiB','contention-command-table-c28-v2-m64-01':'c28 / 64 GiB','contention-command-table-c24-v2-m48-01':'c24 / 48 GiB','c1280-screen-c320-os-06':'c320 v3 OS','c1280-screen-c320-predicted-06':'c320 v3 预测','c1280-primary-c640-08-v6':'c640 v6','real-trace-p0-c320-remaining-l160-01':'c320 A-off L160','real-trace-p0-c640-paced-full-l352-02':'c640 Full L352','real-trace-p0-c640-relaxed-full-l640-02':'c640 Full L640 / scale1','real-trace-p0-c640-main-full-l896-01':'c640 Full L896','real-trace-p0-c640-balanced-full-s075-01':'c640 Full / scale0.75','real-trace-p0-c640-balanced-aoff-s075-01':'c640 A-off / scale0.75','real-trace-p0-c640-balanced-os-s075-01':'c640 OS / scale0.75'}
main=choose(set(aliases))
text='''# ClawBox 最近三天实验审计（2026-10-16—18，北京时间）

这批实验保留了有价值的正、负和边界证据，但尚未形成“真实 trace 下整套机制提高成功吞吐”的完整证明。较强的正证据来自合成异质内存工作负载；真实 trace 的规模实验主要揭示排队、基础设施过载、旧 idle 策略门控和操作成本问题。所有原结果均保留，不把未完成点伪装成成功，也不因整臂失败而删除已经发生的工具执行和局部成功。

本次只读检查找到 220 个有冻结配置的运行目录，342 个已启动 arm/attempt：73 个实验/探索点、20 个训练/校准臂、11 个功能/激活检查臂、238 个资格检查臂。另有 132 个未启动的配置臂，**不计作已跑实验**；独立保存了 819 个探针结果文件，其中可能有同一资格流程的父/子结果文件，不能当作 819 次独立重复实验。没有启动、重跑或修改任何正式实验。

工作簿 [ClawBox-三天实验审计.xlsx](ClawBox-三天实验审计.xlsx) 可筛选全部记录；[ALL-EXPERIMENTS.md](ALL-EXPERIMENTS.md) 按每个实验臂列出数据、配置和远端日志路径。机器可读主表为 [Experiments.csv](Experiments.csv)，全部资格/校准/验证在 [Other_runs.csv](Other_runs.csv)。

**指标口径**

- Agents/s = 窗口内 `valid=true` 的 `session_complete` / 窗口秒；计入创建和排队时间。结束事件在清理后发出，因此截止线附近正在清理的 Agent 仍未计入。
- 本报告的 Tool Steps/s = `execution_scope=agent-tool` 的 `tool_completed` / 窗口秒；后台维护单列。它包含非零退出，并可能包含一条高层 trace 工具调用的多次底层执行，因此不能称为“成功逻辑步骤/s”。模型响应/s 及 Tool+Model 合计均另列。早期 c160 没有 execution_scope：应用 Tool 吞吐留空，替代的全部未分类命令吞吐分别为 r2 的 1.769/s、r3 的 1.678/s（default-resident），不是没有执行进展。
- 原结果 `agents_per_minute` / `steps_per_minute` 除以 60 单独保留。这些值使用原 `workload_window_seconds`，不是本报告包含启动阶段的统一窗口；原 Steps 可能只汇总正常返回会话。两种口径不要混用。
- P90执行 = Agent 执行开始到结束；P90到达 = 保存的 `offered_start_unix_s` 到 `final_agent_completion`。只代表已有时延样本的完成子集，不包含未完成请求。无成功时留空，另给 admission/create queue 等诊断延迟。只有清理结束数据时，明确命名 request-to-cleanup，绝不冒充成功时延。
- CPU 均值来自累计 cgroup CPU 时间差，按采样首末时间归一化；CPU peak 是采样区间峰值。LOCAL 均值用有效样本间梯形积分估计；有大采样空洞时不保证准确。所有内存 peak 都是采样峰值下界。
- **OS-default 的 `local_used_bytes` 实际是整个 VM cgroup 的内存。其 NUMA LOCAL 均值/峰值留空，使用 VM cgroup 总内存作替代。** Full 的 NUMA sampler 将 LOCAL 节点驻留与 unattributed 计入 LOCAL，shared live 与 WARM snapshot 另计。
- observed 窗口从 supervisor arm start 到最早的执行期限、终止、arm failure 或末事件。手工取消而没有准确 stop 事件的臂可能包含取消收敛阶段；固定前 300/600/1200 s 和 300 s 分箱也全部提供。不能保证“运行几分钟就已稳态”。
- borrow/checkpoint/restore 次数在主表只计 `status=ok`；操作失败及全量尝试数另存。checkpoint 按 VM 计，Runtime+Tool 一对通常是 2 次，不是 1 次。

**主要性能结果**

窗口为本次统一重算。吞吐不舍弃失败/未完成 Agent 已执行的工具操作。P90 无数据写“—”。

'''
text+=table(['实验','策略','窗口 s','成功/目标','Agents/s','Tool Steps/s','P90执行 s','P90到达 s'],[[aliases[r['run_id']],r['policy'],r['window_s'],f"{r['success_agents']}/{r['c']}",fmt(r['Agents_per_s'],4),fmt(r['Tool_Steps_per_s'],3),r['P90_agent_s'],r['P90_offered_to_agent_end_s']] for r in main])
text+='''c24/48 GiB：预测相对 fixed-256 的统一窗口成功吞吐提升约 **64.0%**，P90到达由 471.54 s 降至 274.24 s。原结果的工作负载窗口口径提升 **66.5%**，两者不是矛盾。c28/80 GiB 和 c28/64 GiB 的预测吞吐分别比 fixed 低约 **11.0%** 和 **10.4%**，也必须保留；准入并非普遍提升吞吐。

c320 v3 预测完成 320/320，c640 v6 预测完成 640/640；对应 OS 在 3600 s 窗口内均 0 成功。这支持这些**特定合成负载与实现**中包含预测与准入的配置组合优于无控制部署。但 Full/OS 还改变部署与 NUMA 放置，未隔离它们的作用，不能单独归因于预测+准入。也不能算一个有限倍数的加速比，或归因于 OOM 避免：没有对应 OOM 证据，c640 OS 还存在 164 个独立失败，含创建/envd 初始化问题。c320 是十阶段 v3，c640 是四阶段 v6，不能将二者直接画为同一负载的扩展曲线。

这些合成 trace 的每次 LLM 等待是 0.01 s：v3 合计 0.11 s、v6 合计 0.05 s。真实 sqlglot trace 有 25 个 LLM span，总等待 177.503573177 s，配置 time_scale=1；真实 replay 重新执行工具，不能把原应用约 568 s 的时间轴当成 replay 必然完成时长。对应内容 SHA256 及可核验副本在 Traces 表。

真实 trace 的 c640 Full/A-off/OS 1200 s 对照均为 0 成功，因此目前不能说 Full 改善成功 Agent 吞吐。A-off 仅关闭工具准入，OS 则关闭全部 ClawBox 策略。Full 的独立失败较少，但它同时让较少会话进入执行，不能只凭失败计数认定成功率更高。

**CPU 与内存实测**

单位：CPU %；内存 GiB。avg 是上述时间加权口径。OS 的总 cgroup 内存是替代指标，不能当成 LOCAL。

'''
text+=table(['实验/策略','CPU avg / peak %','LOCAL avg / peak GiB','VM cgroup avg / peak GiB','WARM peak GiB','borrow / checkpoint / restore'],[[aliases[r['run_id']]+' / '+r['policy'],f"{fmt(r['CPU_avg_pct'])} / {fmt(r['CPU_peak_pct'])}",f"{fmt(r['LOCAL_avg_GiB'])} / {fmt(r['LOCAL_peak_GiB'])}",f"{fmt(r['VM_cgroup_avg_GiB'])} / {fmt(r['VM_cgroup_peak_GiB'])}",r['WARM_allocated_peak_GiB'],f"{r['borrow_n']} / {r['checkpoint_VM_n']} / {r['restore_VM_n']}"] for r in main])
text+='''c640 合成 OS 的 CPU peak 超过记录的 160 CPU 容量（112.04%）；这里保留采样异常，不将它解释为可信瞬时利用率。CPU 是 VM 父 cgroup 口径，不能代表 socket 上 Cube 服务及其他宿主进程的总利用率。WARM allocated 是挂载点物理用量，未必等于本臂净新增或 pool reserved/committed；这三列已分开保存。

**真实 trace 的 5 分钟分段吞吐**

三臂所有区段 Agents/s 都是 0；下表是 Tool Steps/s。

'''
bnames=['real-trace-p0-c640-balanced-full-s075-01','real-trace-p0-c640-balanced-aoff-s075-01','real-trace-p0-c640-balanced-os-s075-01']
bins={n:[w for w in t['Windows'] if w['run_id']==n and w['label'].startswith('bin_')] for n in bnames}
text+=table(['策略','0–5 min','5–10 min','10–15 min','15–20 min'],[[aliases[n]]+[fmt(w['agent_tool_completions_per_s'],3) for w in bins[n]] for n in bnames])
text+='''Full 从 5.55 降至 1.37 Steps/s，而 OS 从 4.46 升至 6.82 Steps/s。这些曲线本身否定了把最初若干分钟直接称为“稳定吞吐”的假设；早期吞吐仍有用，但要报告区间和趋势。

**内存配置与历史状态**

完整逐臂配置在 [Configurations.csv](Configurations.csv)，所有恢复字段在 [all-config-fields.csv](all-config-fields.csv)。不仅列一个总内存数：包括每 NUMA 节点 capacity/LOW/HIGH、全局 capacity/LOW/HIGH、pool budget、physical live limit、WARM、borrow cap、emergency free、Runtime/Tool guest memory、startup/admission/restore headroom、static/noncommand reservations、prediction scale、pressure-control 参数、snapshot storage/mechanism 与 transfer concurrency/timeout。原 MiB 字段保留在 JSON/YAML；下表转换为 GiB。

'''
text+=table(['实验/策略','LOCAL容量','LOW/HIGH','准入pool','WARM','borrow cap','Runtime/Tool','到达间隔 s','预测scale'],[[aliases[r['run_id']]+' / '+r['policy'],cfg[r['id']]['local_capacity_GiB'],f"{fmt(cfg[r['id']]['local_LOW_GiB'])}/{fmt(cfg[r['id']]['local_HIGH_GiB'])}",cfg[r['id']]['admission_pool_GiB'],cfg[r['id']]['warm_capacity_GiB'],cfg[r['id']]['borrow_cap_GiB'],f"{fmt(cfg[r['id']]['runtime_guest_GiB'])}/{fmt(cfg[r['id']]['tool_guest_GiB'])}",cfg[r['id']]['stagger_s'],cfg[r['id']]['prediction_scale']] for r in main])
text+='''规模实验一般使用 NUMA 0/1 的 CPU 0–159，WARM 在 NUMA 2。Full 通常是 928 GiB LOCAL 容量、192 GiB live borrow 上限，合计 live limit 1120 GiB；384 GiB WARM 不是再额外增加 384 GiB 可运行内存。OS 的 effective LOCAL/pool 字段变为 1120 GiB，语义是统一预算；配置里保留的 WARM=384 GiB 不意味着 OS 启用了 checkpoint。表中 `—` 是未配置/不适用/缺测，绝不是默认为 0 或无限。

后期真实 trace 常见 headroom：startup=256 MiB、admission margin=256 MiB、restore=1024 MiB、static tool=468 MiB、noncommand=128 MiB；具体以每臂冻结值为准，不能回填早期运行。许多历史结果把 Cube 有效容量配置、代码 commit 标为 unverified/unknown，报告原样保留；未记录的创建并发进程环境也不伪装成已冻结配置。

历史 idle 的关键异常是 2.7181621785 s forecast 过期门控：模型仍在等待时，也可能失去候选资格。因此 WARM 很空、没有 checkpoint，并不自动意味着负载太轻。后期修复后的 actual-idle 只有功能/资格验证，未完成新的正式规模性能对照。旧机制结果不回溯改标签。

**沙箱基本操作实测**

以下均为观察窗口内 `status=ok`，秒；统计完整操作服务时延，不含 VM 创建 gate 排队。失败操作另见 Operations 表，不能混入成功操作分位数。

'''
opnames={'real-trace-p0-c320-remaining-l160-01','real-trace-p0-c640-paced-full-l352-02','real-trace-p0-c640-balanced-full-s075-01'}
selected=[o for o in t['Operations'] if o['run_id'] in opnames and o['scope']=='observation_window' and o['status']=='ok' and o['operation'] in ['checkpoint','restore','borrow','return']]
text+=table(['实验','操作','VM角色','n','avg s','P90 s','max s'],[[aliases[o['run_id']],o['operation'],o['role'],o['n'],o['mean'],o['p90'],o['max']] for o in selected])
text+='''c320 A-off L160 的 236 次成功 checkpoint 是 118 Tool + 118 Runtime，说明配对 checkpoint 确实执行过，不能说所有历史实验都没有生效。但其成功吞吐依然为 0。c640 L352 的恢复平均约 37–43 s、P90 约 78 s，这类成本必须与收益一起报告。

Full scale0.75 的两个 borrow 实测 65.03/20.48 s，return 61.50 s；配置借用成本约 0.06576 s/GiB，4 GiB 即约 0.263 s（另有 runtime penalty），远低于当时实测服务时延。激活 borrowing 本身不是性能改善证据。

单独的纯内存拷贝延迟没有可支持的规模运行计时，留空。快照日志有 logical/dirty/transferred/allocated bytes 和整体 checkpoint 服务时间，不能用 guest 配置内存/总服务时间冒充 memcpy 带宽。独立探针在 Probes 与 [probe-operations.csv](probe-operations.csv)，与高并发服务时延分开。所有 71,363 条生命周期操作的角色、状态、起止时间、服务时延、源文件行号和快照字节在 [operation-details.csv](operation-details.csv)；操作汇总在 [Operations.csv](Operations.csv)。

运行时保护/boost、poll 次数与耗时、回滚、cache reclaim 事件见 [Runtime-control.csv](Runtime-control.csv)；原结果中已有的工具执行 mean/P90/P95/P99、预测误差和资源统计见 [Reported-metrics.csv](Reported-metrics.csv)。已扫描事件没有发现 protection/boost 动作事件；有结果汇总的规模点也报告为 0，不能声称这些机制已证明收益。cache reclaim 有独立事件，不等于 protection/boost。poll_seconds 是已记录控制循环耗时，不能直接当 CPU 开销百分比。创建排队分位数来自已留下 timing 的会话（含失败/取消），并不覆盖尚未留下 timing 的请求。

**仍值得保留的局部成功与异常数据**

'''
partial=[r for r in a if (r['success_agents'] or 0)>0 and r['success_agents']!=r['c']]
text+=table(['run_id','策略','成功/目标','窗口 s','Agents/s','Tool Steps/s','P90执行 s'],[[r['run_id'],r['policy'],f"{r['success_agents']}/{r['c']}",r['window_s'],fmt(r['Agents_per_s'],4),fmt(r['Tool_Steps_per_s'],3),r['P90_agent_s']] for r in partial])
text+='''这些局部完成是真实观测，但没有与之匹配的无异常对照，不能据此拼接一个完整比较。无准入 c24 的 drain/cleanup 故障、v5 completion marker、FD 耗尽、TAP=500、不完整 VM 供给、S3lvol missing chunks、policy 部署不一致均按对应 run 标注。早期 `final-primary-c320-01/03` 已有 98/83 个完成，不能因整组超时/失败写成“什么也没有得到”。

同一 run 的旧实时摘要和本次数字有差异时，优先核对窗口与成功/失败状态。例如 c320 pair-l160-06 的旧 120 pauses / 118 restores 是更长的保留日志范围；本次 2400 s 窗口内成功操作是 104 / 98。pair-l160-05 有 423 次 restore 尝试，其中成功 396。没有覆盖或删除旧日志。

**可供后续 agent 审查的证据位置与重算**

- [Sources.csv](Sources.csv)：每臂的 `ssh kunpeng` 绝对路径；包括 `run-state.json` 所在目录、`experiment.yaml`、`arms/<id>.json`、`attempts/<attempt>/<arm>/events/<arm>.jsonl`、`model-gateway/`、`worker.stderr.log`。
- [source-file-manifest.csv](source-file-manifest.csv)：已读取原始文件的路径、大小、修改时间。原始事件日志仍在远端，没有为了报告移动或修改。
- `clawbox-three-day-review-20261018/configs/`：220 份冻结 YAML 与逐臂保存的 effective 配置；`resolved-configs.json` 给出恢复方法。289 臂直接来自结果，51 臂通过历史 policy SHA256 身份与 arm_id 一致性验证，2 臂来自唯一声明配置。
- `clawbox-three-day-review-20261018/{arms,windows,supplement,result-timelines,probes,sources}.json`：原记录摘取；`{operations,sessions,samples,model-requests}.jsonl.gz`：压缩结构化证据，保留 per-session/per-request 时间戳及 admission 字段。
- `collect.py → collect-supplement.py` 是只读原始提取脚本，需在 Kunpeng 运行并安装 PyYAML；下载采集目录后，`resolve-configs.py → analyze.py → write-report.py` 重建本地 CSV/报告，`node build-workbook.mjs` 生成 XLSX。补充文件 `supplement.json`、`early-events.json`、`trace-current-copies.json`、`result-timelines.json` 也已打包。
- 完整报告包 [ClawBox-review-20261018-final.zip](ClawBox-review-20261018-final.zip) 保存本次派生数据、脚本、冻结配置、操作明细和 XLSX；不声称包内含远端数十 GB 的全部原始事件行。包内索引提供其位置。

本次没有扩大正式实验、修复实验代码或重跑任何 arm。对当前证据最稳妥的论文结论是：预测准入相对 fixed 在特定合成争用点有收益；包含预测与准入的配置组合在所测合成规模点优于 OS，但尚未隔离部署/放置贡献；其他内存配置可能有负收益；真实 trace 的大规模完整收益与逐机制消融尚未证明。快照/借用已经有部分激活和真实成本证据，不能与最终性能收益混为一谈。
'''
(P/'REPORT.md').write_text(text,encoding='utf-8')

alltext='# 全部实验与探索臂（73 个 arm/attempt）\n\n资格、训练和功能检查在 Other_runs.csv，不混作正式实验。每个条目的窗口吞吐和操作次数都来自统一观察窗口；已失败和取消数据仍保留。全部参数见 Configurations.csv / all-config-fields.csv。\n\n'
for r in a:
 c=cfg[r['id']];alltext+='## '+r['run_id']+' / '+str(r['policy'])+'\n\n'
 alltext+=f"开始：{r['start_CST']}；run={r['run_state']}，保存的 arm={r['arm_state_saved']}；证据 ID `{r['id']}`。\n\n"
 alltext+=table(['并发','窗口 s','成功','Agents/s','Tool Steps/s','Model响应/s','P90执行 s','P90到达 s'],[[r['c'],r['window_s'],r['success_agents'],fmt(r['Agents_per_s'],5),fmt(r['Tool_Steps_per_s'],4),fmt(r['Model_responses_per_s'],4),r['P90_agent_s'],r['P90_offered_to_agent_end_s']]])
 if r.get('Unclassified_commands_per_s'):alltext+=f"旧日志未分类命令吞吐（替代，非应用 Tool 口径）：{r['Unclassified_commands_per_s']:.4f} 次/s。\n\n"
 alltext+=table(['CPU avg/peak %','LOCAL avg/peak GiB','VM cgroup avg/peak GiB','WARM peak GiB','borrow/checkpoint/restore','独立失败','采样最大间隔 s'],[[f"{fmt(r['CPU_avg_pct'])}/{fmt(r['CPU_peak_pct'])}",f"{fmt(r['LOCAL_avg_GiB'])}/{fmt(r['LOCAL_peak_GiB'])}",f"{fmt(r['VM_cgroup_avg_GiB'])}/{fmt(r['VM_cgroup_peak_GiB'])}",r['WARM_allocated_peak_GiB'],f"{r['borrow_n']}/{r['checkpoint_VM_n']}/{r['restore_VM_n']}",r['independent_failures'],r['sample_max_gap_s']]])
 alltext+=table(['LOCAL容量GiB','LOW/HIGH GiB','pool GiB','WARM GiB','borrow cap GiB','Runtime/Tool GiB','到达间隔 s','scale'],[[c['local_capacity_GiB'],f"{fmt(c['local_LOW_GiB'])}/{fmt(c['local_HIGH_GiB'])}",c['admission_pool_GiB'],c['warm_capacity_GiB'],c['borrow_cap_GiB'],f"{fmt(c['runtime_guest_GiB'])}/{fmt(c['tool_guest_GiB'])}",c['stagger_s'],c['prediction_scale']]])
 alltext+=f"机制：admission={c['admission']}，idle={c['idle_policy']}/{c['idle_scope']}，borrow={c['borrow_policy']}，snapshot={c['snapshot_mechanism']}/{c['snapshot_storage']}，placement={c['placement_policy']}，runtime={c['runtime_control']}。\n\n"
 alltext+=f"解释：{r['notes'] or '没有已确认的专项异常注释；不代表已证明不存在其他混杂。'}\n\n"
 if r['failure']:alltext+='保存的失败原因：`'+r['failure'].replace('`','\'')+'`\n\n'
 alltext+=f"配置：`{c['frozen_config_path']}`\n\n事件：`{r['event_path'] or '缺失'}`\n\n结果：`{r['result_path'] or '缺失'}`\n\n"
(P/'ALL-EXPERIMENTS.md').write_text(alltext,encoding='utf-8')
print('reports written',len(text),len(alltext))
