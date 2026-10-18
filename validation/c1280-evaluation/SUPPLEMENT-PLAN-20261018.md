# 最小补充实验计划：公平比较、可接受长尾、有效机制消融

更新：2026-10-18。状态：**已执行到预设停止条件，矩阵未继续扩展**。本计划替代旧 PLAN 的待执行长矩阵；旧结果、配置和日志全部保留。执行入口仍是公开的 `clawbox experiment` 工作流，不增加第二条执行路径。计划点 Full、A-off 及三个有界 Full 调整点均为零 Agent 成功；详见 [补充实验结果](SUPPLEMENT-RESULTS-20261018.md)。由于没有可比较的成功 Full 基线，OS、I-off、B-off、c640 和其他次要消融未运行。

## 1. 本轮需要回答的问题

最重要的缺口不是再证明一次“极端过载时限流能救活系统”，而是：在 c320/c640、真实 LLM 等待、可接受端到端时延下，Full 是否比完全无 ClawBox 控制的 OS 基线提高成功吞吐；其中预测+工具准入、双 VM idle checkpoint 分别贡献多少。

本轮以 **c320 为主，四个核心臂、最多六个正式臂、总计约两小时**。c640 是预先定义的升级分支，不将两个密度的所有消融机械相乘。两个密度全消融仍是后续覆盖目标，本轮不能宣称全部完成。若实际单臂超过预算，则减少次要臂，不把长尾截断后的 Steps/s 当作成功吞吐。

本计划的配置是待验证假设，不承诺正收益。仅触发水位、产生借用或增加 checkpoint 次数，不足以证明资源优化有效。

## 2. 哪些旧结论直接保留

依据 [三天审计](../three-day-review-20261018/REPORT.md)、[逐臂结果与原始日志索引](../three-day-review-20261018/ALL-EXPERIMENTS.md)、[配置表](../three-day-review-20261018/Configurations.csv)。以下数值使用审计的统一观察窗口，包含创建/排队。

| 已有证据 | 可保留的结论 | 本轮处理 |
|---|---|---|
| c24 / 48 GiB：预测 .0811、fixed .0495 Agents/s；到达 P90 274.24 / 471.54 s | 特定异构合成负载中，预测相对固定记账有收益 | 不重跑 fixed；不外推成真实 trace 或相对 OS 的收益 |
| c28 / 80、64 GiB：预测成功吞吐分别约低 11.0%、10.4% | 准入的负收益边界确实存在 | 保留负结果，不调参洗掉 |
| 合成 c320、c640：预测配置分别 .3222、.3534 Agents/s，全部成功；OS 对应窗口零成功 | 特定部署/放置/准入组合能够改善过载结果 | 不重跑；不能分离预测准入的贡献，也不能归因于未观测到的 OOM |
| 真实 c320 LOW/HIGH=128/160 GiB：236 次 VM checkpoint、225 次 restore，2400 s 内零成功 | 成对休眠执行过，但过紧预算和迁移开销可能抵消收益 | 保留为高压力边界，不再作为消融主点 |
| 真实 c640 LOW/HIGH=512/640 GiB，scale=.75：Full/A-off/OS 在 1200 s 均零成功，Tool Steps/s=3.901/4.061/5.401 | 已有明确负面/未完成证据，不能声称成功吞吐改善 | 不重跑旧配置；保留全部窗口曲线 |
| actual-idle 修复后的功能、资格与仓库检查 | 支持已验证范围内的代码正确性 | 不重复全套测试；尚不支持规模性能收益 |

旧 c320 v3 与 c640 v6 的工具阶段数不同，LLM 等待总计只有 .11/.05 s，不能连成同负载扩展曲线。旧 c640 真实 trace 每 2 s 到达，最后请求到达在 1278 s，超过 1200 s 窗口；不能作为“640 请求全部参与完成竞争”的结果。

## 3. 内存与并发配置

### 主点 M320，以及仅在需要时执行的 M640

所有容量均为 **GiB**，配置文件用 MiB（乘 1024）。下表是新的配置族，不与旧 arm 拼成因果配对。

| 配置项 | M320 主点 | M640 升级点 | 解释 |
|---|---:|---:|---|
| 请求数 / concurrency level | 320 | 640 | 同时报告真正 resident/执行 Tool 的 VM/Agent 数；请求数不代表全部同时执行 |
| CPU / LOCAL NUMA | CPU 0–159，NUMA 0/1 | 相同 | 每节点 80 CPU；执行前核对现有主机设置与可用容量 |
| LOCAL 物理容量 | 928 | 928 | 每节点 464；不是预先分配 928 GiB |
| 全局 LOW / HIGH | **384 / 448** | **640 / 768** | 每节点分别 192/224、320/384 |
| 准入 pool budget | **448** | **768** | 当前实现必须等于 HIGH，不能独立设置 |
| WARM 容量，NUMA 2 | **256** | **256** | 包含受同一池约束的快照与共享驻留；不是额外 256 GiB Tool 执行容量 |
| live borrow 上限 | **64** | **64** | 属于 WARM 的使用额度，不是再增加一个池 |
| 所有臂的共同 live 物理上界 | **992 = 928 + 64** | 相同 | 包括 OS、I-off、B-off；核对编译后字段，不能因关闭机制而暗改上界 |
| Runtime / Tool guest 配置 | 2 vCPU / 2 GiB；2 vCPU / 4 GiB | 相同 | 声明容量不等于宿主实际驻留 |
| 每请求到达间隔 | **0.5 s** | **0.5 s** | 最后计划到达分别 159.5 / 319.5 s；创建队列延迟仍计入端到端 |
| 创建并发 gate | **8 个 Agent VM pair** | 相同 | 显式记录 `CLAWBOX_SANDBOX_CREATE_CONCURRENCY=8`；不同时发出 320/640 个创建请求 |
| 每臂观察上限 | **960 s** | **1080 s** | 从实验臂开始计时，包含创建；完成则提前结束 |
| 每臂收尾预算 | 120 s | 120 s | 收尾单列，不把它解释为工具执行；超过预算则停止后续排队 |

选择依据及限制：

- M320 的 HIGH 从已知过紧的 160 提高到 448 GiB（2.8 倍），LOW/HIGH 间保留 64 GiB 滞回。旧 c320 A-off 的 LOCAL 峰值 249.72 GiB 是受旧准入、借用、休眠影响的观测，**不是 320 个自然驻留 Agent 的容量估计**；不能按它反推 250 GiB 就足够。
- 旧 c640 Full/A-off 的 LOCAL 峰值约 510/535 GiB，但当时尚未全部创建/完成，且存在旧 idle 门控。新 M640 768 GiB 是减轻创建和准入排队的起点，不是从这些峰值推导出的充分容量保证。不得将观测峰值乘二或除二，当作可靠密度模型。
- 旧 c320 高压力点 WARM allocated/committed 峰值 111.62 GiB；c640 L352 为 28.39 GiB。故 256 GiB 作为宽松参考池，比直接默认 384 更有依据，同时给修复后的双 VM checkpoint 留余量。峰值来自不同旧策略，不能保证新点不满。
- 64 GiB borrow 是新的有限借用配置：覆盖旧 c640 L352 的 34 GiB 借用记账峰值，并高于旧 c320 52.81 GiB 实际 shared-live 峰值；但旧 c320 的**借用记账峰值曾达 192 GiB**，因此不能声称 64 GiB 在所有旧场景均不约束。必须同时报告实际驻留与预留额度。
- LOW/HIGH 是策略阈值，不是宿主物理上界。如果 928 GiB LOCAL 仍宽裕、PSI/reclaim/工具耗时没有争用证据，只有策略阈值造成排队，就不能说避免了真实物理内存争用。不能继续压低 HIGH 来制造正收益。

### 固定不扫的参数

使用同一 sqlglot-4528-workspace-replay-v2 trace、完整验证、相同镜像/template/KB。核验保存的 trace SHA256 `6998e1d3baf4d1b3e3a2ca127fdfef51771d05f945179b2f943bb3013d7c242c`。25 个 LLM span 总计 177.503573177 s，`time_scale=1`，不缩短、不补造等待、不使用未来 LLM 完成时间参与策略。

预测 reservation scale=.75；startup/admission margin 各 256 MiB；non-command=128 MiB；static tool=468 MiB（只保留字段，本轮默认不跑 fixed）；restore headroom=1024 MiB；emergency free=32768 MiB。actual-idle=`wait`、scope=`agent_and_tool`，Runtime 和 Tool 都纳入成对 checkpoint，仍需满足实际压力/容量条件；不是每次空闲无条件休眠。仅 warm-only incremental-COW、LRU；transfer concurrency=4、timeout=120 s；poll=.5 s；采样=1 s；load-aware placement；template affinity 关闭；不做 SSD 两层存储。

运行时保护/boost/主动 cache reclaim 开启，参数继承经审计的 actual-idle 配置。成本选择器和同一份已保存的 action-cost evidence 在所有匹配臂中固定；记录其 hash。旧低负载成本严重低估过部分规模运行成本，必须报告预测成本与实测操作耗时的偏差，不能据此声称选择器最优；本轮不临时修改选择算法或逐臂重新校准以改善成绩。

## 4. 最小实验矩阵与优先顺序

| 优先级 | 臂 | 相对同一主点 Full 的唯一主动改动 | 要回答的问题 |
|---|---|---|---|
| P0 | Full | 无 | 完成率、端到端长尾、机制是否真实激活；该次也是正式参考，不另跑 pilot 重复它 |
| P0 | A-off | `admission.tool=off` | 预测+工具准入这个整体是否有效；保留 managed 创建准入、放置、runtime、borrow、checkpoint |
| P0 | OS | OS deployment/placement，关闭 ClawBox 工具准入、idle、borrow、runtime，保持公共 CPU/物理内存/创建 gate | 完全无 ClawBox 控制时的应用效果；这是系统对比，不能解释为单项消融 |
| P0 | I-off | `idle_reclaim.policy=off` | 双 VM 实际空闲休眠的净收益，包括恢复延迟；仅在 Full 有有效 checkpoint 时执行 |
| P1 | B-off | `shared_memory.borrow=off` | 借用是否有净收益；保留 checkpoint 和原成本协调策略，不顺带改成 reclaim-first；仅在实际 shared-live/borrow 有效时执行 |
| P1 | 第六个名额 | 从下列规则选一个 | 不为凑矩阵而运行未激活机制 |

第六名额选择在查看对应消融胜负之前确定，并记录选择理由：

1. 若 Full 的 WARM 峰值允许合法且确实可能受限的缩池，优先 S-tight；否则，若有实际 runtime 保护/boost 或大量主动回收动作，选 R-off；否则，若两个 LOCAL 节点有持续负载不均，选 N-round-robin；否则不花该名额。
2. R-off 关闭 memory protection、CPU release boost、proactive memory reclaim，结论仅为这个 runtime 组合；如果只有 cache reclaim 激活，不宣称 CPU boost 已证明收益。
3. N-round-robin 仅改 `placement.policy=round_robin`。两者都保持 NUMA 固定放置，所以测的是负载感知放置相对轮询，**不是完整的 NUMA-aware 对 non-NUMA**。后者还需一个资源边界完全一致且实现支持的专门对照，暂不混入。
4. Full 无 checkpoint 时，不跑 I-off 并给它起“idle 消融已完成”的标签；无实际借用时同理。动作计数是决定是否值得测的依据，不是收益本身。

### WARM 缩容规则

用本轮 Full 的池实际准入所依据的 charged/committed/reserved 轨迹选择 S-tight，同时保存 mounted allocated、run 前基线和 shared-live，避免把历史文件占用当成本轮需求。初选约为相关峰值的 80%，按 16 GiB 向下取整；必须满足 `128 <= S-tight < Full 相关峰值`，且不得大于参考池 256 GiB。**borrow cap 始终 64 GiB，公共 live 上界始终 992 GiB**。

当前校验要求 `2 × borrow cap <= WARM`。若 Full 峰值只有 100 GiB，128 GiB 仍是宽松池，本轮不能假称已做有约束的 WARM 消融。要测 80 GiB，需要另外一对相同 borrow cap（例如 32 GiB）、相同公共 live 上界的宽/窄池参考与对照；不能只改窄池的借用上限，再把差异全归因于快照容量。该两臂配置族延后，不挤占 P0，也不修改实现绕过校验。

### c640 分支与一次调整上限

- M320 Full 能正常完成且激活相关压力机制：留在 c320 完成核心消融；本轮不为“两个密度都有数字”再开整个 c640 矩阵。
- M320 完成正常但无有效压力/机制：先完成 c320 Full/A-off/OS，保留低争用边界；取消无信息量的 I-off/B-off。剩余最多三个名额用于 **M640 Full/A-off/OS**，不同时降低预算或改 trace。若预算不足，先 Full/A-off，OS 标记未做。
- M320 Full 严重排队且不是实际资源争用：只允许一次配置调整，将 LOW/HIGH/pool 提至 **448/512/512 GiB**（每节点 224/256），其他参数不动；原 Full 保留为边界，调整后必须有新的匹配 Full。该调整占用正式臂名额和总时间，优先保住 A-off/OS/I-off，删次要项。
- 若主要瓶颈是 Cube 创建失败、控制器整体失效、CPU/IO 基础设施拥塞，调内存不能解决：暂停该分支，报告不能得到的结论，不花剩余时间连续修代码、反复重跑。普通 Agent 失败不连坐其他 Agent。

## 5. “合适主点”的判据与停止条件

选择点只依据预先规定的资源、进度和时延标准，不依据 Full 是否赢。完整保留所有筛选尝试。

1. c320 主点目标为 960 s 内至少 95% 请求验证成功，到达至完成 P90 不超过 900 s；c640 对应 1080 s / 1050 s。它们是本轮工程可接受性目标，不是原 trace 的理论时限。原应用约 568 s 不等于重执行 replay 必须 568 s 结束。
2. 判定“成功吞吐正收益且长尾可接受”时，另要求 Full 成功率不下降，且可比较的到达 P90 不比 A-off/OS 恶化超过 25%；若明显靠牺牲长尾换吞吐，归入 Pareto 边界，不作为主要胜利。P99 只在有足够完成样本并明确删失情况时解释；未完成请求不能删掉后声称总体 P99 很好。
3. 有压力不等于必须有 OOM。联合看 CPU/memory PSI、回收/停顿、工具执行时间、实际 LOCAL/shared-live、创建/准入排队。若只看到配额排队，明确是策略瓶颈；若大量 restore 服务时间接近旧 c640 的 P90 约 78 s，报告迁移抖动，不能只报释放了多少 GiB。
4. 前几分钟无完成并不自动异常：真实 trace 本身需要时间。每 300 s 保存进度；约第 10 分钟若仍零完成且工具推进停滞、排队明显无法在窗口收敛，就停止扩展次要消融。可到预定截止保存这个点，不再无期限等待。
5. 控制组出现可归因的自然压力、OOM/超时，仍是有效结果，不因其输掉而自动取消。控制平面端口冲突、错误模板或不公平限额导致的差异不算方法收益。次要错误若不影响指标/处理语义，不触发修复和重跑。

## 6. 两小时时间账与执行边界

- 前 8 分钟：核对 source、trace/KB/template/image 身份、saved host 配置、CPU/NUMA 与可用容量；检查公共 cgroup/池边界、旧任务和端口，使用已有公开 validate/doctor 流程。只做必要的一次 host apply（如果公共物理上界或池设置需要改变），不重建镜像、不重训、不重复完整测试。无法在预算内通过时，明确阻塞点，不谎报准备完成。
- c320 每臂最多 16 分钟观察 + 2 分钟收尾。四个核心臂最多 72 分钟；初始检查和最后 4 分钟报告后，理论上余 36 分钟供两个次要臂。任何超时、修复、调整都消耗这个余量。
- c640 每臂最多 18 + 2 分钟；如果走三 c320 + 三 c640 分支，最坏会超过两小时。因此根据第一臂实际时长决定是否能放下下一臂，**不承诺六臂必定跑完**。只在剩余时间覆盖整个观察窗、收尾和报告时开新臂。
- 不并行运行互相竞争同一 socket/池的实验臂。不预热 Full 而让控制组冷启动；不使用隐藏 barrier 从吞吐分母中扣掉 VM 创建。所有臂使用同样创建 gate、固定到达日程与相同测量定义；在 Full 确定配置后，匹配对照顺序以固定种子打乱并保存。
- 只清理本实验拥有的 VM/快照，保存原日志；不自动全局 drop cache 或影响其他任务。每臂记录开始时池驻留/缓存与 CPU 状态，无法统一的初始状态作为可能混杂因素披露。

## 7. 必须保存的数据与结论边界

每臂独立 run ID；保存公开配置、编译后逐臂有效配置、进程环境（特别是创建 gate）、source/trace/image/KB/cost evidence 的 hash、host quota/cgroup 实际值、起止时刻和终止原因。内存变更要另开 run ID，不能覆盖旧目录。

数据沿用现有 event/result/sample/lifecycle 日志，不另写 replay：

- Agents/s：窗口内验证成功 Agent / 包含创建的窗口秒数；成功率分母为全部计划请求，并列实际 offered/started/completed/failed/censored 数。各臂提前全部完成时同时给总 makespan 吞吐；未完成臂给固定 300/600/900 s 和完整窗口，不能混用窗口挑最好数字。
- Steps/s：分别给应用 Tool 底层完成操作、非零退出、可恢复的逻辑步骤和模型响应；无法可靠合并为逻辑步骤就留空，不冒充成功逻辑 Steps/s。每 300 s 的分段曲线保留，早期吞吐不自动叫稳态。
- 到达/执行起点到完成的 P50/P90/P95/P99、完成样本数、未完成等待时长下界；只有完成子集分位数时必须标注。创建 gate、创建服务、准入等待、LLM 实际等待、工具执行、恢复等待分开。
- VM cgroup CPU avg/采样 peak 和计量 CPU 数；LOCAL avg/采样 peak、shared-live、WARM allocated/committed/reserved、borrow reserved、PSI、OOM。OS 的旧 `local_used_bytes` 是总 VM cgroup 内存：若现有采样仍不能分离 NUMA，LOCAL 留空，跨臂仅比较同口径总 cgroup 内存。
- borrow/return/checkpoint/restore 的尝试、成功、失败次数，Runtime/Tool 分开；服务时延 avg/P90/P99、排队、有效 bytes，以及同时运行的 transfer 数。一次 Agent pair checkpoint 通常是两次 VM 操作。纯 memcpy 延迟缺少直接计时则留空。
- runtime protection/boost/cache reclaim 分开计数，控制循环耗时单列。idle 候选、实际 pause/restore、LOCAL 释放、恢复后完成进度必须能够关联；控制开关打开不等于生效。

单次配对仅作为补充证据，不声称统计显著。小幅提升或阶段性波动保留不确定性，后续真需要支撑论文核心结论时只重复关键配对，不扩展全矩阵。

## 8. 明确暂缓的项目

真实 trace 新 fixed、两个密度全消融、完整 NUMA-aware 对照、runtime 子机制拆分、多档 WARM 曲线、snapshot 编码/template 单项收益、额外 workload 泛化及重复试验均不塞进本轮。旧 fixed 的结论可保留，但不等于新的 actual-idle 场景已经覆盖 fixed。未激活机制只能写“在本点无作用/没有证据”，不能写已证明有效。

完成情况：计划已制定；实验尚未启动；所有新 arm 均为待执行。下一次经用户要求执行时，先兑现最重要的公平比较与长尾检查，再决定有没有条件做次要消融。

配置静态校验已通过：主点、一次放宽候选、c640 候选分别检查了上述开关组合，共 24 个配置组合，均通过现有 schema/compiler，公共 live 上界均为 992 GiB。24 是静态检查数，**不是计划运行 24 臂**。证据：[SUPPLEMENT-CONFIG-CHECK-20261018.json](SUPPLEMENT-CONFIG-CHECK-20261018.json)。未进行远端容量、输入身份、环境或性能验证，执行前仍需完成第 6 节检查。
