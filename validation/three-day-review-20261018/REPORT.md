# ClawBox 最近三天实验审计（2026-10-16—18，北京时间）

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

| 实验 | 策略 | 窗口 s | 成功/目标 | Agents/s | Tool Steps/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| c28 / 80 GiB | fixed-256 | 238.84 | 28/28 | 0.1172 | 0.821 | 206.30 | 223.56 |
| c28 / 80 GiB | full-predicted | 268.44 | 28/28 | 0.1043 | 0.730 | 221.75 | 251.61 |
| c28 / 64 GiB | fixed-256 | 186.08 | 28/28 | 0.1505 | 1.053 | 161.77 | 182.37 |
| c28 / 64 GiB | full-predicted | 207.74 | 28/28 | 0.1348 | 0.943 | 186.96 | 200.13 |
| c24 / 48 GiB | fixed-256 | 485.14 | 24/24 | 0.0495 | 0.346 | 451.62 | 471.54 |
| c24 / 48 GiB | full-predicted | 295.87 | 24/24 | 0.0811 | 0.568 | 253.99 | 274.24 |
| c320 v3 OS | os-default-no-admission | 3600.00 | 0/320 | 0.0000 | 0.420 | — | — |
| c320 v3 预测 | predicted-shared | 993.18 | 320/320 | 0.3222 | 3.222 | 486.44 | 923.31 |
| c640 v6 | os-default-no-admission | 3600.00 | 0/640 | 0.0000 | 0.138 | — | — |
| c640 v6 | predicted-shared | 1811.07 | 640/640 | 0.3534 | 1.414 | 524.84 | 1737.01 |
| c320 A-off L160 | a-off | 2400.00 | 0/320 | 0.0000 | 3.802 | — | — |
| c640 Full L352 | f-s384 | 1179.40 | 0/640 | 0.0000 | 2.956 | — | — |
| c640 Full L640 / scale1 | f-s384 | 2400.00 | 0/640 | 0.0000 | 1.350 | — | — |
| c640 Full L896 | f-s384 | 697.26 | 0/640 | 0.0000 | 5.259 | — | — |
| c640 Full / scale0.75 | f-s384 | 1200.00 | 0/640 | 0.0000 | 3.901 | — | — |
| c640 A-off / scale0.75 | a-off | 1200.00 | 0/640 | 0.0000 | 4.061 | — | — |
| c640 OS / scale0.75 | d-os-default | 1200.00 | 0/640 | 0.0000 | 5.401 | — | — |

c24/48 GiB：预测相对 fixed-256 的统一窗口成功吞吐提升约 **64.0%**，P90到达由 471.54 s 降至 274.24 s。原结果的工作负载窗口口径提升 **66.5%**，两者不是矛盾。c28/80 GiB 和 c28/64 GiB 的预测吞吐分别比 fixed 低约 **11.0%** 和 **10.4%**，也必须保留；准入并非普遍提升吞吐。

c320 v3 预测完成 320/320，c640 v6 预测完成 640/640；对应 OS 在 3600 s 窗口内均 0 成功。这支持这些**特定合成负载与实现**中包含预测与准入的配置组合优于无控制部署。但 Full/OS 还改变部署与 NUMA 放置，未隔离它们的作用，不能单独归因于预测+准入。也不能算一个有限倍数的加速比，或归因于 OOM 避免：没有对应 OOM 证据，c640 OS 还存在 164 个独立失败，含创建/envd 初始化问题。c320 是十阶段 v3，c640 是四阶段 v6，不能将二者直接画为同一负载的扩展曲线。

这些合成 trace 的每次 LLM 等待是 0.01 s：v3 合计 0.11 s、v6 合计 0.05 s。真实 sqlglot trace 有 25 个 LLM span，总等待 177.503573177 s，配置 time_scale=1；真实 replay 重新执行工具，不能把原应用约 568 s 的时间轴当成 replay 必然完成时长。对应内容 SHA256 及可核验副本在 Traces 表。

真实 trace 的 c640 Full/A-off/OS 1200 s 对照均为 0 成功，因此目前不能说 Full 改善成功 Agent 吞吐。A-off 仅关闭工具准入，OS 则关闭全部 ClawBox 策略。Full 的独立失败较少，但它同时让较少会话进入执行，不能只凭失败计数认定成功率更高。

**CPU 与内存实测**

单位：CPU %；内存 GiB。avg 是上述时间加权口径。OS 的总 cgroup 内存是替代指标，不能当成 LOCAL。

| 实验/策略 | CPU avg / peak % | LOCAL avg / peak GiB | VM cgroup avg / peak GiB | WARM peak GiB | borrow / checkpoint / restore |
|---|---|---|---|---|---|
| c28 / 80 GiB / fixed-256 | 33.75 / 63.96 | 39.72 / 80.00 | 39.75 / 80.00 | 0.00 | 0 / 0 / 0 |
| c28 / 80 GiB / full-predicted | 32.85 / 63.85 | 37.48 / 70.58 | 37.51 / 70.58 | 0.00 | 0 / 0 / 0 |
| c28 / 64 GiB / fixed-256 | 28.17 / 62.61 | 38.32 / 64.00 | 38.33 / 64.00 | 0.00 | 0 / 0 / 0 |
| c28 / 64 GiB / full-predicted | 25.30 / 65.25 | 35.37 / 57.02 | 35.38 / 57.02 | 0.00 | 0 / 0 / 0 |
| c24 / 48 GiB / fixed-256 | 14.10 / 55.85 | 25.92 / 46.66 | 25.92 / 46.66 | 0.00 | 0 / 0 / 0 |
| c24 / 48 GiB / full-predicted | 18.98 / 56.41 | 28.66 / 42.07 | 28.67 / 42.07 | 0.00 | 0 / 0 / 0 |
| c320 v3 OS / os-default-no-admission | 26.28 / 98.41 | — / — | 791.45 / 1051.31 | 0.00 | 0 / 0 / 0 |
| c320 v3 预测 / predicted-shared | 31.03 / 64.15 | 321.51 / 599.90 | 322.66 / 601.11 | 0.00 | 0 / 0 / 0 |
| c640 v6 / os-default-no-admission | 37.24 / 112.04 | — / — | 683.71 / 813.55 | 0.00 | 0 / 0 / 0 |
| c640 v6 / predicted-shared | 32.86 / 81.04 | 275.07 / 453.63 | 276.20 / 455.14 | 0.00 | 0 / 0 / 0 |
| c320 A-off L160 / a-off | 34.32 / 67.91 | 161.36 / 249.72 | 194.15 / 302.53 | 111.62 | 72 / 236 / 225 |
| c640 Full L352 / f-s384 | 23.24 / 42.11 | 225.59 / 291.00 | 226.47 / 291.01 | 28.39 | 10 / 50 / 43 |
| c640 Full L640 / scale1 / f-s384 | 23.29 / 50.83 | 405.16 / 485.31 | 405.21 / 485.31 | 0.00 | 0 / 0 / 0 |
| c640 Full L896 / f-s384 | 38.65 / 52.50 | 216.69 / 413.30 | 216.74 / 413.71 | 0.00 | 0 / 0 / 0 |
| c640 Full / scale0.75 / f-s384 | 36.32 / 54.93 | 338.48 / 510.07 | 338.92 / 510.68 | 0.00 | 2 / 0 / 0 |
| c640 A-off / scale0.75 / a-off | 35.25 / 53.36 | 337.86 / 535.18 | 338.74 / 537.90 | 0.00 | 6 / 0 / 0 |
| c640 OS / scale0.75 / d-os-default | 47.28 / 82.87 | — / — | 345.00 / 580.23 | 0.00 | 0 / 0 / 0 |

c640 合成 OS 的 CPU peak 超过记录的 160 CPU 容量（112.04%）；这里保留采样异常，不将它解释为可信瞬时利用率。CPU 是 VM 父 cgroup 口径，不能代表 socket 上 Cube 服务及其他宿主进程的总利用率。WARM allocated 是挂载点物理用量，未必等于本臂净新增或 pool reserved/committed；这三列已分开保存。

**真实 trace 的 5 分钟分段吞吐**

三臂所有区段 Agents/s 都是 0；下表是 Tool Steps/s。

| 策略 | 0–5 min | 5–10 min | 10–15 min | 15–20 min |
|---|---|---|---|---|
| c640 Full / scale0.75 | 5.037 | 5.553 | 3.643 | 1.370 |
| c640 A-off / scale0.75 | 5.050 | 4.917 | 4.053 | 2.223 |
| c640 OS / scale0.75 | 4.463 | 4.810 | 5.510 | 6.820 |

Full 从 5.55 降至 1.37 Steps/s，而 OS 从 4.46 升至 6.82 Steps/s。这些曲线本身否定了把最初若干分钟直接称为“稳定吞吐”的假设；早期吞吐仍有用，但要报告区间和趋势。

**内存配置与历史状态**

完整逐臂配置在 [Configurations.csv](Configurations.csv)，所有恢复字段在 [all-config-fields.csv](all-config-fields.csv)。不仅列一个总内存数：包括每 NUMA 节点 capacity/LOW/HIGH、全局 capacity/LOW/HIGH、pool budget、physical live limit、WARM、borrow cap、emergency free、Runtime/Tool guest memory、startup/admission/restore headroom、static/noncommand reservations、prediction scale、pressure-control 参数、snapshot storage/mechanism 与 transfer concurrency/timeout。原 MiB 字段保留在 JSON/YAML；下表转换为 GiB。

| 实验/策略 | LOCAL容量 | LOW/HIGH | 准入pool | WARM | borrow cap | Runtime/Tool | 到达间隔 s | 预测scale |
|---|---|---|---|---|---|---|---|---|
| c28 / 80 GiB / fixed-256 | 80.00 | 64.00/76.00 | 76.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |
| c28 / 80 GiB / full-predicted | 80.00 | 64.00/76.00 | 76.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |
| c28 / 64 GiB / fixed-256 | 64.00 | 48.00/60.00 | 60.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |
| c28 / 64 GiB / full-predicted | 64.00 | 48.00/60.00 | 60.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |
| c24 / 48 GiB / fixed-256 | 48.00 | 36.00/44.00 | 44.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |
| c24 / 48 GiB / full-predicted | 48.00 | 36.00/44.00 | 44.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |
| c320 v3 OS / os-default-no-admission | 1120.00 | —/— | 1120.00 | 384.00 | — | 2.00/4.00 | 0.05 | 1.00 |
| c320 v3 预测 / predicted-shared | 928.00 | 800.00/864.00 | 864.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |
| c640 v6 / os-default-no-admission | 1120.00 | —/— | 1120.00 | 384.00 | — | 2.00/4.00 | 0.05 | 1.00 |
| c640 v6 / predicted-shared | 928.00 | 800.00/864.00 | 864.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |
| c320 A-off L160 / a-off | 928.00 | 128.00/160.00 | 160.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |
| c640 Full L352 / f-s384 | 928.00 | 320.00/352.00 | 352.00 | 384.00 | 192.00 | 2.00/4.00 | 2.00 | 1.00 |
| c640 Full L640 / scale1 / f-s384 | 928.00 | 512.00/640.00 | 640.00 | 384.00 | 192.00 | 2.00/4.00 | 2.00 | 1.00 |
| c640 Full L896 / f-s384 | 928.00 | 768.00/896.00 | 896.00 | 384.00 | 192.00 | 2.00/4.00 | 2.00 | 1.00 |
| c640 Full / scale0.75 / f-s384 | 928.00 | 512.00/640.00 | 640.00 | 384.00 | 192.00 | 2.00/4.00 | 2.00 | 0.75 |
| c640 A-off / scale0.75 / a-off | 928.00 | 512.00/640.00 | 640.00 | 384.00 | 192.00 | 2.00/4.00 | 2.00 | 0.75 |
| c640 OS / scale0.75 / d-os-default | 1120.00 | —/— | 1120.00 | 384.00 | — | 2.00/4.00 | 2.00 | 0.75 |

规模实验一般使用 NUMA 0/1 的 CPU 0–159，WARM 在 NUMA 2。Full 通常是 928 GiB LOCAL 容量、192 GiB live borrow 上限，合计 live limit 1120 GiB；384 GiB WARM 不是再额外增加 384 GiB 可运行内存。OS 的 effective LOCAL/pool 字段变为 1120 GiB，语义是统一预算；配置里保留的 WARM=384 GiB 不意味着 OS 启用了 checkpoint。表中 `—` 是未配置/不适用/缺测，绝不是默认为 0 或无限。

后期真实 trace 常见 headroom：startup=256 MiB、admission margin=256 MiB、restore=1024 MiB、static tool=468 MiB、noncommand=128 MiB；具体以每臂冻结值为准，不能回填早期运行。许多历史结果把 Cube 有效容量配置、代码 commit 标为 unverified/unknown，报告原样保留；未记录的创建并发进程环境也不伪装成已冻结配置。

历史 idle 的关键异常是 2.7181621785 s forecast 过期门控：模型仍在等待时，也可能失去候选资格。因此 WARM 很空、没有 checkpoint，并不自动意味着负载太轻。后期修复后的 actual-idle 只有功能/资格验证，未完成新的正式规模性能对照。旧机制结果不回溯改标签。

**沙箱基本操作实测**

以下均为观察窗口内 `status=ok`，秒；统计完整操作服务时延，不含 VM 创建 gate 排队。失败操作另见 Operations 表，不能混入成功操作分位数。

| 实验 | 操作 | VM角色 | n | avg s | P90 s | max s |
|---|---|---|---|---|---|---|
| c320 A-off L160 | borrow | tool | 32 | 9.87 | 25.07 | 31.08 |
| c320 A-off L160 | checkpoint | tool | 118 | 6.16 | 16.01 | 60.36 |
| c320 A-off L160 | checkpoint | runtime | 118 | 4.18 | 8.49 | 54.55 |
| c320 A-off L160 | restore | runtime | 110 | 13.33 | 29.30 | 70.28 |
| c320 A-off L160 | return | tool | 2 | 19.25 | 23.23 | 24.22 |
| c320 A-off L160 | restore | tool | 115 | 14.38 | 41.43 | 84.27 |
| c320 A-off L160 | borrow | runtime | 40 | 9.34 | 32.23 | 39.14 |
| c320 A-off L160 | return | runtime | 3 | 42.23 | 57.25 | 60.26 |
| c640 Full L352 | checkpoint | tool | 25 | 11.61 | 25.03 | 37.66 |
| c640 Full L352 | checkpoint | runtime | 25 | 11.89 | 32.25 | 57.17 |
| c640 Full L352 | restore | runtime | 21 | 36.92 | 78.42 | 108.89 |
| c640 Full L352 | restore | tool | 22 | 42.58 | 78.02 | 85.46 |
| c640 Full L352 | borrow | runtime | 2 | 36.76 | 42.87 | 44.39 |
| c640 Full L352 | borrow | tool | 8 | 26.70 | 39.77 | 48.97 |
| c640 Full L352 | return | tool | 8 | 10.51 | 29.76 | 30.20 |
| c640 Full L352 | return | runtime | 2 | 12.92 | 21.06 | 23.10 |
| c640 Full / scale0.75 | borrow | tool | 2 | 42.76 | 60.58 | 65.03 |
| c640 Full / scale0.75 | return | tool | 1 | 61.50 | 61.50 | 61.50 |

c320 A-off L160 的 236 次成功 checkpoint 是 118 Tool + 118 Runtime，说明配对 checkpoint 确实执行过，不能说所有历史实验都没有生效。但其成功吞吐依然为 0。c640 L352 的恢复平均约 37–43 s、P90 约 78 s，这类成本必须与收益一起报告。

Full scale0.75 的两个 borrow 实测 65.03/20.48 s，return 61.50 s；配置借用成本约 0.06576 s/GiB，4 GiB 即约 0.263 s（另有 runtime penalty），远低于当时实测服务时延。激活 borrowing 本身不是性能改善证据。

单独的纯内存拷贝延迟没有可支持的规模运行计时，留空。快照日志有 logical/dirty/transferred/allocated bytes 和整体 checkpoint 服务时间，不能用 guest 配置内存/总服务时间冒充 memcpy 带宽。独立探针在 Probes 与 [probe-operations.csv](probe-operations.csv)，与高并发服务时延分开。所有 71,363 条生命周期操作的角色、状态、起止时间、服务时延、源文件行号和快照字节在 [operation-details.csv](operation-details.csv)；操作汇总在 [Operations.csv](Operations.csv)。

运行时保护/boost、poll 次数与耗时、回滚、cache reclaim 事件见 [Runtime-control.csv](Runtime-control.csv)；原结果中已有的工具执行 mean/P90/P95/P99、预测误差和资源统计见 [Reported-metrics.csv](Reported-metrics.csv)。已扫描事件没有发现 protection/boost 动作事件；有结果汇总的规模点也报告为 0，不能声称这些机制已证明收益。cache reclaim 有独立事件，不等于 protection/boost。poll_seconds 是已记录控制循环耗时，不能直接当 CPU 开销百分比。创建排队分位数来自已留下 timing 的会话（含失败/取消），并不覆盖尚未留下 timing 的请求。

**仍值得保留的局部成功与异常数据**

| run_id | 策略 | 成功/目标 | 窗口 s | Agents/s | Tool Steps/s | P90执行 s |
|---|---|---|---|---|---|---|
| final-primary-c320-short-fixed-only | fixed | 5/320 | 1500.00 | 0.0033 | 3.793 | 1475.09 |
| final-primary-c320-01 | full | 98/320 | 1800.00 | 0.0544 | 3.995 | 1606.57 |
| final-primary-c320-03 | full | 83/320 | 1800.00 | 0.0461 | 3.992 | 1677.75 |
| contention-no-admission-c24-v2-m48-02 | no-admission | 4/24 | 524.18 | 0.0076 | 0.282 | 203.69 |
| c1280-screen-c320-01 | no-tool-admission-shared | 174/320 | 1200.00 | 0.1450 | 1.064 | 356.59 |
| c1280-screen-c320-02 | no-tool-admission-shared | 166/320 | 695.54 | 0.2387 | 2.555 | 232.63 |
| c1280-screen-c320-03 | no-tool-admission-shared | 6/320 | 560.15 | 0.0107 | 1.107 | 213.12 |
| c320-admission-ablation-01 | admission-predicted | 308/320 | 875.45 | 0.3518 | 3.652 | 354.42 |

这些局部完成是真实观测，但没有与之匹配的无异常对照，不能据此拼接一个完整比较。无准入 c24 的 drain/cleanup 故障、v5 completion marker、FD 耗尽、TAP=500、不完整 VM 供给、S3lvol missing chunks、policy 部署不一致均按对应 run 标注。早期 `final-primary-c320-01/03` 已有 98/83 个完成，不能因整组超时/失败写成“什么也没有得到”。

同一 run 的旧实时摘要和本次数字有差异时，优先核对窗口与成功/失败状态。例如 c320 pair-l160-06 的旧 120 pauses / 118 restores 是更长的保留日志范围；本次 2400 s 窗口内成功操作是 104 / 98。pair-l160-05 有 423 次 restore 尝试，其中成功 396。没有覆盖或删除旧日志。

**可供后续 agent 审查的证据位置与重算**

- [Sources.csv](Sources.csv)：每臂的 `ssh kunpeng` 绝对路径；包括 `run-state.json` 所在目录、`experiment.yaml`、`arms/<id>.json`、`attempts/<attempt>/<arm>/events/<arm>.jsonl`、`model-gateway/`、`worker.stderr.log`。
- [source-file-manifest.csv](source-file-manifest.csv)：已读取原始文件的路径、大小、修改时间。原始事件日志仍在远端，没有为了报告移动或修改。
- `clawbox-three-day-review-20261018/configs/`：220 份冻结 YAML 与逐臂保存的 effective 配置；`resolved-configs.json` 给出恢复方法。289 臂直接来自结果，51 臂通过历史 policy SHA256 身份与 arm_id 一致性验证，2 臂来自唯一声明配置。
- `clawbox-three-day-review-20261018/{arms,windows,supplement,result-timelines,probes,sources}.json`：原记录摘取；`{operations,sessions,samples,model-requests}.jsonl.gz`：压缩结构化证据，保留 per-session/per-request 时间戳及 admission 字段。
- `collect.py → collect-supplement.py` 是只读原始提取脚本，需在 Kunpeng 运行并安装 PyYAML；下载采集目录后，`resolve-configs.py → analyze.py → write-report.py` 重建本地 CSV/报告，`node build-workbook.mjs` 生成 XLSX。补充文件 `supplement.json`、`early-events.json`、`trace-current-copies.json`、`result-timelines.json` 也已打包。
- 完整报告包 [ClawBox-review-20261018-final.zip](ClawBox-review-20261018-final.zip) 保存本次派生数据、脚本、冻结配置、操作明细和 XLSX；不声称包内含远端数十 GB 的全部原始事件行。包内索引提供其位置。

本次没有扩大正式实验、修复实验代码或重跑任何 arm。对当前证据最稳妥的论文结论是：预测准入相对 fixed 在特定合成争用点有收益；包含预测与准入的配置组合在所测合成规模点优于 OS，但尚未隔离部署/放置贡献；其他内存配置可能有负收益；真实 trace 的大规模完整收益与逐机制消融尚未证明。快照/借用已经有部分激活和真实成本证据，不能与最终性能收益混为一谈。
