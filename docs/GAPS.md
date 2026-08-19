# 未完成工作（代码核实版）

> 依据：仓库代码 + 真机探测 + 交接文档交叉验证。每项区分：
> **🔴 未实现**（代码里没有）· **🟠 被环境阻断**（代码思路有，但真机 guest/权限做不到）· **🟡 已实现但未全量验证** · **⚪ 设计上明确跳过**（论文范围外）。

## G0. 核心主线：ClawTune 的 eBPF + cgroup 在 Tool VM 里**未跑通**

这是"让 ClawTune 那一套全跑通"的最大缺口。现状与原因（真机 guest 探测，`scripts/probe-kata-guest.sh`）：

| 子项 | 状态 | 代码/探测证据 |
|---|---|---|
| 工具命令真实资源观测（cgroup/eBPF） | 🔴 未实现 | ClawTune 采集器跑在 Runtime VM，命令在 Tool VM（两个独立 VM，不共享内核/cgroup）。`clawbox/cell/manifests.py` 未给 tool 容器部署采集器 |
| per-execution 独占 cgroup | 🟠 被阻断 | guest 里 `/sys/fs/cgroup` **只读**：即使加 `CAP_SYS_ADMIN`，`mkdir /sys/fs/cgroup/...` 仍 `Read-only file system`（探测实证） |
| BCC eBPF（ClawTune 的 `net_accounting.py` 网络记账） | 🟠 被阻断 | guest 无内核头、无 clang、无 `/lib/modules` → BCC 运行期编译不可用 |
| CO-RE eBPF（cilium/ebpf） | 🟠 被阻断 | guest 无 `/sys/kernel/btf/vmlinux`（无 BTF） |
| 非 CO-RE eBPF（kretprobe 预编译对象） | 🔴 未实现 | 需在 tool-bridge 加 loader + 预编译 BPF 对象；**attach 可行性（perf kprobe PMU）未实测** |
| cgroup 读（容器级）+ 进程树 `/proc` 归因 | 🟢 可做未接入 | 探测证明 cgroup 读可用、bridge 能读自己子进程；但 bridge 尚未实现采集 |

**含义**：KB/预测目前喂的是代理值（`kb-flush.py` 的 `SIGNED_FIELDS` 里 `cpu_time_sec/rss_peak_bytes` 来自 span 插件侧估算 + bridge 直接子进程，**不是** Tool VM 命令的真实 cgroup/eBPF 消耗）→ 预测质量存疑。

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

1. **G0 落地**：tool-bridge 加 cgroup 读 + 进程树归因（现在能做）→ 非 CO-RE eBPF loader 真机冒烟（唯一未验证点）。
2. 真机解锁后：跑真实任务 → `m1-p0-joincheck.sh` 验证 cgroup/eBPF 数据进 KB。
3. 并发扩到 6/8（`scripts/m1-concurrent.sh` 改 `N=`）+ scale 表出图。
4. patch 提取指标修复（成功率的正确口径）。

> 诚实的边界：**"让 ClawTune 那一套全跑通"目前只差一条主线**——工具命令的真实 cgroup/eBPF 资源观测在 Tool VM 内落地；其余机制层（LLM proxy / trace / join / KB / 多租户 / 控制面）都已就绪并有代码/测试/真机证据。
