# ClawBox 实验有效性与处置规则

## 结论

整体研究问题成立：在固定的物理内存上限下，比较不同 host incremental-memory
admission 与 idle reclamation 策略对正确吞吐、尾延迟和 memory-time 的影响。
但旧结果只能视为 pilot。正式结果必须经过本文的 fail-closed gate；缺失关键证据
不能记为 0，也不能用文字 limitation 代替。

## 哪些问题必须修，哪些可以如实报告

以下属于系统或实验设计错误。发生时该 arm 无效，不能只“诚实报告数值”：

- Full snapshot 未预留创建期间的瞬时内存，或实测峰值突破预留；
- 用 guest capacity 减 Firecracker RSS 预测 host growth；
- P90、Static、Full 和 Oracle 使用不同 prediction target；
- Tool budget 和 parent budget 分两次获取，存在 hold-and-wait；
- response-critical restore 被普通 boot/Tool 工作无限期阻塞；
- 以 `max(sum RSS, memory.current)` 作为主内存指标；
- 发生 cgroup/host OOM、swap、NUMA 越界或结果不正确后仍将 run 计入性能统计；
- 把 replay 中的真实 response duration 称为 deployable predictor；
- 把 Tool-only balloon 与 whole-pair checkpoint 称为 mechanism-level 对比；
- 缥缈的 100 ms sampled peak 被当作安全峰值，未读取 `memory.peak`；
- static baseline 在 idle 阶段释放 capacity，却声称代表 naive static sandbox。
- 仅按请求正文永久去重，导致两个合法的相同连续 model step 被合并。

以下不是设计错误，可以保留并如实报告，但必须限制 claim：

- Full snapshot 的磁盘写放大、page-cache 增长和较高峰值；这是机制成本；
- checkpoint 在短 wait workload 上收益为负；这是适用边界；
- balloon target 偶尔延迟达到但不引起 OOM/服务失败；报告成功率和尾延迟；
- realistic predictor 有早/晚恢复错误；报告 sensitivity，不把 oracle 上界当产品结果；
- Tool-only checkpoint 与 pair checkpoint 结果不同；分别解释 mechanism 与 system effect；
- 某一固定 budget 下策略没有饱和；结果有效但不能支撑 density 优势，应补 saturation sweep。
- TCP 写成功但客户端未收到响应的 ACK 模糊窗口；没有 guest 生成的稳定 idempotency key
  时无法证明端到端 exactly-once，只能报告 gateway production-at-most-once、重连次数与
  final-state equality。

## 当前实现的安全语义

- 主指标是 VM parent cgroup 的 `memory.current`；RSS 仅作 Firecracker 诊断。
- `memory.peak` 提供 arm-level kernel peak；checkpoint 前重置 session child
  `memory.peak`，获得 operation-local 短峰值。
- Full active-call、global Static、per-tool P90 和 Oracle 都预留同一种量：从 admit
  时刻开始的 host incremental growth。Full 不再计算 `4096 MiB - RSS`。
- `static_lifetime` 按 Runtime+Tool declared capacity 持有整个 session lifetime，作为
  naive static sandbox baseline。
- parent 与 Tool 预留由 `AtomicMemoryAdmission` 一次获取；优先级为 restore、
  checkpoint、continuation、lifetime、boot。只有 checkpoint/restore 可进入 high 与
  hard 之间的 emergency interval。
- checkpoint/hybrid arm 没有校准后的 parent 与 Tool transient reservation 时拒绝启动。
- `wait_estimator=fixed|heldout|oracle` 明确区分可部署估计与 oracle 上界；所谓 prefetch
  已改名为 proactive restore，因为系统没有 snapshot-page working-set prefetch。
- 每个 VM 在启动前进入带 `cpuset.cpus`、`cpuset.mems` 和 `memory.swap.max=0` 的
  session cgroup。
- model gateway 仅复用尚未成功交付的同指纹请求；成功交付后的相同正文被视为新的
  model step，并记录每个逻辑请求的 `production_attempts`。因此正式 claim 应写成
  “观测到的 gateway execution 无重复”，不要写成不可证明的网络端到端 exactly-once。

## 公平实验矩阵

不要用一个笛卡尔积回答所有问题。每项研究只改变一个主要因素：

| Study | Arms | 固定条件 |
| --- | --- | --- |
| Admission | static lifetime、Full active-call、global Static、per-tool P90、Oracle | resident，无 reclamation |
| Mechanism | Tool resident、Tool balloon、Tool-only checkpoint | 相同 scope、相同 idle trigger |
| System reclamation | pair resident、Tool balloon、pair checkpoint、hybrid | 相同 admission |
| Decision | eager、fixed delay、wait-aware pressure；reactive/proactive restore | 相同 scope 与 admission |
| End-to-end | naive static、最佳 non-predictive、完整 proposed、Oracle upper bound | 相同 budget/load/trace |

Balloon 与 pair checkpoint 可以出现在 system-level 表中，但不能据此宣称纯机制优劣。
机制消融使用 `paper_experiment.dimension=mechanism`，并强制
`checkpoint_scope=tool`；runner 会让 balloon inflation 与 Tool-only checkpoint 使用
同一个 `fixed_delay_s`，且在 response 先到时取消尚未开始的 reclaim。
论文还应提供至少三档 memory budget、围绕 capacity knee 的 `0.5C/C/1.5C/2C`
offered load、staggered 与 synchronized arrival、以及 wait-time `0.5x/1x/2x/4x`。

## 校准与 gate 顺序

正式 suite 前先在同一 host/kernel/Firecracker/filesystem 配置上跑独立 pilot：

1. boot、idle、full-touch、Tool command 的 memory accounting；
2. low/high resident Tool 和 pair 的 full-checkpoint transient；
3. restore API、service-ready、first Tool start/completion；
4. 数百轮 balloon inflate/deflate；
5. pair checkpoint/restore 后的 reconnect、request dedup 与 final-state equality；
6. 1/2/4/8 concurrent checkpoint/restore；
7. `cpuset.mems.effective` 与 `memory.numa_stat`；
8. `memory.swap.current=0`、无 `memory.events.local` max/OOM。

从独立 pilot 汇总 P99 reservation：

```bash
python scripts/calibrate-replay-memory.py \
  /data/pilot-*/results/summary.json \
  --quantile 0.99 --output /data/calibration/replay-memory-p99.json
```

`formal_ready=false` 表示样本缺少 boot、restore、checkpoint 或 aggregate residual
证据，不得填写猜测值继续跑。

每个正式 arm 完成后运行：

```bash
python scripts/validate-replay-gates.py /data/arm/results/summary.json \
  --mode formal --output /data/arm/results/validity-gates.json
```

非零退出码表示该 arm 不进入汇总。`pilot` 模式也不会放过 correctness、OOM、swap、
NUMA、checkpoint I/O 和 transient coverage，只是少检查 prediction coverage 与
`memory.events.local max`。
paper study runner 已自动执行同一 formal gate 并把失败写为
`FormalValidityGateFailure`；独立命令仍适合检查已有结果。

## 必报指标

- Outcome：correct tasks/min、steps/min、task P50/P95/P99、step P50/P95/P99；
- Memory：mean/P95/kernel peak `memory.current`、memory-time、anon/file/kernel、
  Tool/Runtime、off-NUMA、swap、high/max/OOM；
- Admission：各 class queue time、completed/timed-out lease、projected/actual peak、
  underprediction；
- Checkpoint：queue/create+fsync/process-stop 时间、logical/allocated/write bytes、
  page-cache growth、minor/major faults、transient peak、cancelled attempts；
- Restore：queue、spawn/load/service-ready、response-ready-to-delivery、first Tool；
- Interference：CPU/memory/I/O PSI；
- Correctness：final-state equality、model dedup、Tool execution count、reconnect、
  restore failure、guest/host OOM、timeout。

## 对历史结果的处理

不要删除旧结果。将其标记为 pilot，并保留作为发现问题的证据。凡是缺少 checkpoint
transient reservation、以 RSS 为主指标、使用 oracle wait 却未标上界、或 baseline
scope 不公平的历史对比，不应进入论文主表或用于因果 claim。修复后的实验必须使用新
output directory、固定 source/config hash，并与旧结果完全分开。
