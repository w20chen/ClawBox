# 实验配置、命令与结果参考

完整的运行顺序见[入门流程](self-service.md)；主机字段见[超节点配置](supernode.md)。
本页用于修改已有实验和解释输出，不重复安装、训练及评估命令链。

## 配置来源与单位

| 文件 | 用途 | 修改方法 |
| --- | --- | --- |
| `~/.config/clawbox/machine.env` | API 地址、Guest 可达地址、ClawTune/CubeSandbox 路径等进程环境 | 编辑后在当前终端 `source`，文件不自动加载 |
| `host.yaml` | 主机期望拓扑、容量、目录和模板 | 编辑后 `host check`、`host apply` |
| `~/.config/clawbox/host.json` | 成功应用的主机 profile | 由 `host apply` 或 `images` 生成，供 `configure` 读取 |
| 实验 YAML | 工作负载、VM 身份、策略、并发及运行限制 | `configure BASE OUTPUT`，或编辑后 `validate --inputs` |
| `RUN/experiment.yaml` | 已启动 run 的冻结配置 | 保留；修改实验时使用新 run ID |

`configure` 先读取 BASE，再采用已有主机 profile 的资源和模板，最后采用显式 CLI 覆盖项。
`--profile FILE` 选择其他 profile；该文件不存在时不会自动配置主机，会保留 BASE 中的相关值。
因此“生成 YAML 成功”不代表目标主机已就绪。`--force` 只允许覆盖输出 YAML，不会重置已有 run。

主机 YAML 的容量用整数 GiB；实验 `resources`、VM 容量和预留字段用 MiB；
结果中以 `_bytes` 结尾的字段用字节。1 GiB = 1024 MiB。带 `--*-gib` 的实验参数必须换算成整数 MiB。
文件路径相对**命令的工作目录**解析，建议给 trace、预测文件和结果目录使用绝对路径。
`--output-root` 是顶层选项，放在 `experiment` 前；它决定公共 CLI 的结果目录，
不是实验 YAML 的 `output.directory`。

## 正式支持的策略

| 对照 | `--baseline` | 行为与额外输入 |
| --- | --- | --- |
| A | `tool-static-resident` | 固定 Tool 增量内存预留，VM 常驻；需要 `static_tool_memory_mib` |
| A+B | `tool-p50-resident` | 使用冻结的命令级 P50，VM 常驻；需要 `prediction_artifact` |
| A+B+C | `tool-p50-wait-reactive` | P50 准入，加压力触发的等待期快照和按需恢复；另需模型等待预测及来源 |

重复 `--baseline` 生成对照矩阵。`--reserve-during`、`--estimate`、`--idle`、`--resume`
只筛选已有方案，不会创建任意策略组合。`baselines --all` 中标为 `DEPRECATED` 的方案，
以及名称与策略元组不匹配的手写方案，不能进入正式运行。

`--static-tool-memory-mib auto` 从已验证训练的额外内存 P90 校准 A；
`--model-wait-prediction-seconds auto` 从当前 trace 的模型耗时中位数乘 `time_scale` 得到等待预测，
并保存来源摘要。这两个 `auto` 都在配置时冻结，正式运行期间不在线训练。

## 常用实验字段

未列 CLI 快捷项的字段直接编辑 YAML。模式为 `schema_version: 2`，未知字段会报错；
完整结构见 [spec.py](../clawbox/experiments/spec.py)。下表是使用含义，最终值以 `describe --json` 为准，
不能把类默认值当作每个 BASE 文件的值。

| YAML 字段 | CLI 或使用要求 |
| --- | --- |
| `workload.repetitions` | `--repetitions`；每个 case/并发/策略的重复数 |
| `workload.cases[]` | 各 case 的 `case_id`、`prompt`、`source`、`source_reference`、`repository`、`base_commit`、`replay_trace_reference`、`validation` |
| `workload.session_assignment` | `--session-assignment single_case` 为每个 case 分别建 arm；`round_robin` 在同一 arm 中轮流分配多个 case，要求显式提供至少两个 case |
| `execution.concurrency_levels` | `--concurrency 1,2,4`；提供的会话数，每会话两个 VM，不是准入后实际同时执行数 |
| `execution.placement_policy`, `session_compute_nodes` | `--placement-policy`、`--session-compute-nodes`；计算节点放置，独立于上面的 case 分配 |
| `execution.randomized_order`, `random_seed` | 默认随机排列 arm；`--random-seed` 固定排列，顺序开关在 YAML 中编辑 |
| `execution.arrival_schedule`, `stagger_interval_seconds` | `--arrival-schedule burst` 或 `fixed_stagger`，后者用 `--stagger-seconds` 设置正间隔 |
| `execution.arm_timeout_seconds` | `--arm-timeout-seconds`；整个 arm 的正整数秒硬截止，不能设 `null` |
| `execution.command_timeout_seconds` | `--command-timeout-seconds`；命令执行超时，准入等待与执行计时分离，但仍受 arm 截止约束 |
| `execution.memory_sample_interval_seconds`, `stabilization_seconds` | `--memory-sample-interval-seconds`、`--stabilization-seconds`；采样间隔与清理后的稳定等待 |
| `runtime`, `sandbox` | Runtime/Tool 模板、镜像摘要、`vcpu`、`memory_mib`、`workspace`、`preflight_command`、`allow_internet_access` |
| `resources.pool_memory_budget_mib` | `--pool-memory-gib`；多节点模式必须等于各节点 HIGH 之和，正常由主机 profile 填入 |
| `resources.emergency_free_memory_mib` | `--emergency-free-memory-gib`；整台主机的可用内存安全底线 |
| `resources.checkpoint_restore_headroom_mib` | `--checkpoint-headroom-gib`；每节点快照操作余量，仅快照策略计入准入 |
| `resources.static_tool_memory_mib` | `--static-tool-memory-mib`；A 的每命令预留；不是 VM RAM |
| `resources.non_command_tool_memory_mib` | 文件等非命令工具的固定预留，未设置时使用静态 Tool 预留 |
| `resources.prediction_artifact` | `--prediction-artifact`；独立训练产生的 `clawbox_p50_v1` 文件 |
| `resources.snapshot_mechanism` | `--snapshot-mechanism incremental-cow` 或 `full-copy` |
| `resources.snapshot_storage` | `--snapshot-storage warm-only` 或 `tiered`；后者允许磁盘层，路径须与服务一致 |
| `validation.command` | `--validation-command`；在 Tool 中执行的最终结果校验，训练必须显式提供 |

改变 vCPU、Guest RAM 或可写盘大小，先按[安装指南](installation.md#4-register-templates-and-verify-ssh)
注册新模板，再更新主机配置。`configure --runtime-vcpu/--tool-vcpu/--*-memory-gib` 不会调整已有模板；
形状变更要求新的模板身份。NUMA、CPU 子集、共享池和磁盘目录也必须先应用到主机，不能只改实验文件。

## 工作负载和 trace

正式自助链路目前使用 `agent.driver: openclaw`、`inference.backend: replay`。
配置和 Worker 虽然识别 `api` 后端，但 `qualification_spec` 当前只接受 replay；
不能把 `--inference-backend api` 当作已经贯通的 `launch` 流程。没有独立的 `record` 子命令。

OpenClaw 重放要求非空任务 prompt、模型名称，以及包含模型请求和响应的 JSONL。
以 [smoke.jsonl](../examples/traces/smoke.jsonl) 或
[memory-smoke.jsonl](../examples/traces/memory-smoke.jsonl) 为格式参考：schema 6 中，
同一 `span_id` 的 `span_start` 保存 `input`，`span_end` 保存 `output`、`duration_ns`、`status`；
`kind: llm`、`sequence_no` 标识模型步骤，响应的 `tool_calls` 决定真实执行的工具。
`function.arguments` 是 JSON 字符串；使用当前 OpenClaw 支持的工具名称。

```bash
clawbox experiment import-trace /data/source-trace.jsonl --output /data/replay.jsonl
clawbox experiment trace /data/replay.jsonl
clawbox experiment validate /data/eval.yaml --inputs
```

`import-trace` 只转换它识别的研究 trace，不安装任务依赖、不创建初始工作区。
新任务的源码、基线版本和依赖必须在 Tool 模板中准备好。
单 case 可用 `configure --trace --prompt --repository --base-commit` 替换输入；多个 case 在 YAML 中填写，
`source` 必须与 `workload.source` 一致。显式 `cases` 优先于 `workload.input`，
不提供时后者按 source 类型读取任务文件。

完整 replay 最后必须有不再请求 Tool 的模型响应。`inference.configuration.max_model_steps`
为正整数时只运行前缀，不能将资格验证前缀用作完整训练数据。
模型等待按 `--time-scale` 缩放；策略造成的响应延迟单独测量。
实际 Tool 输出保留，不会按录制文本改写来制造正确结果。

P50 训练只接受终态成功的 A run、完整数据、通过的最终校验和清理标记，并且 repository 一致。
ClawBox 优先选择 LatticeKB，有效结果缺失时尝试 ToolKB；缺失观测不会填零。
加载预测时检查 Tool 的镜像摘要、vCPU、内存和架构，`validate --inputs` 还检查录制命令覆盖。
`unavailable` 非空或无有效 Guest 内存标签时，应补采有效数据。

## 命令与资格验证

以下命令均在 `clawbox experiment` 下；`--help` 列出完整参数。

| 命令 | 含义 |
| --- | --- |
| `trace FILE`、`validate SPEC --inputs`、`describe SPEC`、`plan SPEC` | 不启动 VM；分别检查 trace、输入、资源概览和展开后的 arm |
| `host inspect/init/check/apply` | 查看拓扑、生成主机 YAML、只读前提检查、应用设置并探测 VM |
| `setup` | 单计算节点的底层设置入口；不会代替 `host apply` 完成全部服务存储配置；多节点用主机 YAML |
| `images` | 重建 Guest 集成、注册模板并更新主机 profile；见安装页 |
| `doctor SPEC [--probe-vm]` | 检查服务、资源、模板和版本；可能刷新快照存储状态，带 probe 时创建/执行/删除 VM |
| `qualify SPEC` | 单独执行真实资格验证，默认保存 `SPEC.qualification.json` |
| `launch SPEC --run-id ID [--detach]` | 输入检查、主机检查、按需资格验证，再运行或恢复；也可通过 `configure --launch` 调用 |
| `run SPEC`、`resume ID` | 底层启动/恢复，要求已有有效 receipt，不自动补资格验证 |
| `status ID` | 查看监督器存活、当前 arm、进展和终态 |
| `report ID`、`collect ID` | 前者根据已完成 arm 生成报告和时间序列；后者读取已有 `summary.json`，不重新汇总或运行负载 |
| `abort ID`、`destroy ID` | 停止或清理该 run 拥有的 VM；保留结果，不删除模板和其他 run |

资格验证对每个计算节点执行所需快照和借还探测，再以所选策略运行首个模型步骤的前缀；
该步骤必须请求 Tool。前缀采用 `true` 校验，只证明执行链路，正式 arm 仍执行任务自己的校验。
默认资格并发等于实验最大并发；降低 `--qualification-concurrency` 不能获得覆盖更高并发的 receipt。
receipt 绑定完整 spec、Python 实现摘要、模板/镜像、节点、策略和并发。改动后 `launch` 自动重验，
`--force-qualify` 可强制重验。

`--detach` 只让正式运行进入后台，资格验证仍在当前命令中完成。
同一 run ID 必须对应同一冻结 spec：正在运行时返回状态，成功时返回已有报告；失败或失联时恢复，
只跳过 spec 匹配、成功且清理已验证的 arm。修改参数应选择新 run ID。
重启后先恢复主机配置，再对原 YAML/run ID 执行 `launch`；不要删除 ownership 或完成标记来重试。

## 结果解释

| 路径（相对 run 根目录） | 内容 |
| --- | --- |
| `run-state.json` | 监督器状态、PID 身份、arm 进度和清理状态；进程已死的 running 会报告为 orphaned |
| `experiment.yaml`, `qualification.json` | 冻结配置与 receipt |
| `arms/*.json`, `arms/*.complete` | 每 arm 完整结果和经验证的成功标记 |
| `summary.json`, `summary.csv`, `summary.md` | 已完成 arm 的汇总 |
| `report.md`, `memory-timeseries.csv` | `report` 生成的比较表和内存时间序列 |
| `attempts/ATTEMPT/ARM/` | 原始事件、日志、模型 gateway、遥测、所有权记录和生命周期证据 |

先检查 session 完成数、最终校验、执行 ID 与遥测关联、丢失事件、OOM 和 cleanup，再比较性能。
`starting` 只表示后台启动已发出，`succeeded` 才是成功终态；未完成或失败的 arm 不能作为成功性能样本。

JCT 是会话完成时间；准入等待为多次等待的累计值，可以超过墙钟时长。
LOCAL 包含 VM RAM、宿主开销及保留缓存，VM 已清空不要求 cgroup 页缓存立即变零。
配置容量、命令预留、P50 预测、实际驻留、WARM 物理分配和共享池账本是不同的量。
多节点报告的峰值、水位和 NUMA 列见[超节点测量说明](supernode.md#检查结果)。

HIGH 超调是控制指标，不直接使 arm 失败，也不能单凭它认定是预测误差。
OOM、共享池超容量、最终校验失败、遥测缺失或无法确认清理会使比较失效。
预测误差/覆盖的 `n/a` 表示缺少相应观测，不等于误差为零或预测未被使用。
单次结果没有运行间不确定性；正式比较应保持拓扑、工作负载、VM 身份、到达方式和随机种子等条件一致，
使用独立训练、多个重复，并保留失败样本和原始数据。
