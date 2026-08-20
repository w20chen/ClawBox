# 未完成工作（代码核实版）

> 依据：仓库代码 + 真机探测 + 交接文档交叉验证。每项区分：
> **🔴 未实现**（代码里没有）· **🟠 被环境阻断**（代码思路有，但真机 guest/权限做不到）· **🟡 已实现但未全量验证** · **⚪ 设计上明确跳过**（论文范围外）。

## G0. 核心主线：ClawTune 的 eBPF + cgroup 在 Tool VM 里**部分跑通**

这是"让 ClawTune 那一套全跑通"的最大缺口。**真机已实测**（`scripts/probe-kata-guest.sh` + tool pod 诊断）：

| 子项 | 状态 | 代码/探测证据 |
|---|---|---|
| 工具命令真实资源观测（进程树） | 🟡 主体已实现，**CPU 捕获待真机验证** | `toolbridge/collector.go`：每执行写 `tool-resource/cgroup-resource-<id>.json`（`cgroup_resource_v1`）；`source=process-tree` 零特权。真机**抓到真实 RSS**（1.5MB），但 CPU 密集命令未验证（20ms 采样已提交 `9e84ce6` 未上线） |
| per-execution 独占 cgroup | 🟠 **tool 容器被 Kata guest cgroup 委托/nsdelegate 限制** | probe pod（带 CAP_SYS_ADMIN）`remount,rw` + mkdir + `cgroup.procs` 写入**成功**（cpu.stat 读出真实值）；但 **tool 容器里 `+cpu > subtree_control` 和 `echo $$ > cgroup.procs` 都失败**（exit 1，errno 未取）→ `tryPerExecCgroup` 自动回退进程树 |
| BCC eBPF（ClawTune 的 `net_accounting.py`） | 🟠 被阻断 | guest 无内核头、无 clang、无 `/lib/modules` → BCC 运行期编译不可用 |
| CO-RE eBPF（cilium/ebpf） | 🟠 被阻断 | guest 无 `/sys/kernel/btf/vmlinux`（无 BTF）——需重建 Kata guest kernel 开 `CONFIG_DEBUG_INFO_BTF` |
| 非 CO-RE eBPF（kretprobe） | 🟠 被阻断 | guest 无 `/sys/kernel/tracing/kprobe_events`（无 tracefs）→ kprobe attach 点缺失；`mount -t tracefs` 是否可行待测 |
| cgroup 读（容器级）+ 进程树 `/proc` 归因 | 🟢 已实现（RSS 已验证） | `collector.go` 按 shell pid **递归 `children` 遍历**（不依赖 pgrp——guest pgrp=0）；20ms 采样+立即基线；`kb-flush.py` 已消费真实值 |

**含义**：KB/预测已可喂**真实 per-execution RSS**；CPU 需 20ms 镜像上线后验证；cgroup v2 在 tool 容器被 Kata 委托限制（probe pod 可但 tool 容器不可，差异待定向诊断）；eBPF 无 BTF/tracefs，**需重建 guest kernel 才能 CO-RE**（论文 eBPF 核心贡献的最干净路径）。详见 `docs/AGENT_HANDOFF_2026-08-20-g0-real-machine.md`。

## G1. 采集数据接入管线：已完成，但未接真机数据

- ✅ `clawbox/tuning/{schema,join,dataset}.py` 已支持三方 join（span+bridge+cgroup 工件），`tests/test_cgroup_join.py` 6 个测试绿。
- 🔴 但没有真实 cgroup/eBPF 数据可喂（见 G0），所以管线是"空转就绪"。

## G2. 控制面 / CRD / 部署

| 项 | 状态 | 证据 |
|---|---|---|
| SandboxTask **v1alpha2 未翻转**（runRef/desiredState） | 🟡 | `clawbox/cell/sandboxtask_v1alpha2.py` 有 draft，但 dispatcher 仍写 v1alpha1（`CLAWBOX_CR_VERSION`）；需 conversion webhook + controller 翻转 |
| Postgres 路径代码就绪但未真机验证 | 🟡 | M1 真机用 SQLite（Docker Hub 代理阻断）；`docker-compose.yml` 有 PG，未在真机跑过 |
| 单副本控制器、2s 全量轮询、无 leader election/work queue | 🔴 | `clawbox/cell/controller.py`、`clawbox/api/dispatcher.py`（无 leader election/持久 work queue） |
| 多节点 HA / 公平调度 / 跨节点放置 | 🔴 | `clawbox/cell/capacity.py`（`SingleNodePlacementPolicy` 只支持单节点） |

## G3. ClawTune 功能栈中"超出论文范围"的未做项（⚪）

| 项 | 说明 |
|---|---|
| Admission/lease **强制执行**（allow/block、配额） | `policy=observe-only` + `failOpen=true`：只算影子决策，不拦截；cell 控制器不咨询 sidecar |
| Placement / affinity / NUMA / cpuset | `enableAffinity/Numa=false`；需要 tool VM 内 cgroup/cpuset 能力（被 G0 阻断） |
| Active tuning（有界执行：cgroup/affinity/NUMA 执行后端） | `executionBackend=hook-only`，未启用；论文明确只做影子预测 |

## G4. 稳定性 / 已知问题

| 项 | 状态 | 证据 |
|---|---|---|
| patch 提取：agent 若 `git commit`，`git diff` 工作区收集会得空 patch | 🟡 | 已加 committed-patch 兜底（commit `b715be3`），但指标口径仍需修 |
| SSH EOF（255）瞬态 | 🟡 | runtime 重试可恢复，但 finalize 拖慢 1-3 分钟；`scripts/ssh-255-probe*.sh` 是诊断 |
| kata shim `RLIMIT_NOFILE=1024` → ~19 cells 并发上限 | 🟠 | `docs/FINDING_2026-08-18-scale32-fd-exhaustion.md`；P3 shim wrapper 代码就绪但需 sudo 未真机执行 |
| 真机 devmapper 陈旧快照（曾阻塞 P0） | 🟠 | 当前 pool 健康无陈旧快照；免密 sudo（dmsetup/systemctl）已可处理 |

## G5. 立即待办（按论文主线排序）

1. **重建任务镜像（含 20ms 采样）并验证 CPU 捕获**：远端同步到 `24d5ad8` → `TAG=g0-fix2`（`rebuild-swe-rebench-tool-overlay.sh`，`BASE_IMAGE`/`TAG` 必须 export）→ 用新 digest 跑短时限任务 → 等 agent 跑 pytest 或经 runtime pod ssh 驱动 CPU burn（`dd if=/dev/urandom of=/dev/null bs=1M count=50`）→ 确认 `cpu_time_s>0`。
2. **cgroup 定向诊断**：tool 容器里取 `+cpu > subtree_control` / `echo $$ > cgroup.procs` 的真实 errno，对比 probe pod 的 cgroup ns → 判断 Kata 配置能否委托子树；不可解则以进程树为准并文档化。
3. **eBPF 定案**：guest 试 `mount -t tracefs`；宿主查 `/sys/kernel/btf/vmlinux`；查 Kata kernel config 的 `CONFIG_DEBUG_INFO_BTF` → 选"重建 guest kernel 开 BTF + cilium/ebpf CO-RE"（论文核心贡献路径）或"宿主侧 eBPF + 时间线 join"。
4. 并发扩到 6/8（`scripts/m1-concurrent.sh` 改 `N=`）+ scale 表出图。
5. patch 提取指标修复（成功率的正确口径）。

> 诚实的边界：**G0 进程树采集已落地并抓到真实 RSS**（工件→runtime 收集→kb-flush 消费真实值）；剩余是**CPU 捕获真机验证**、**cgroup 委托限制定向诊断**、**eBPF 落地**（需重建 guest kernel 开 BTF，是论文 eBPF 核心贡献的最干净路径）。完整状态与下一步见 `docs/AGENT_HANDOFF_2026-08-20-g0-real-machine.md`。
