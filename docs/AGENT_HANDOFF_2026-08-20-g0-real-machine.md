# AGENT HANDOFF 2026-08-20 — G0 真机验证进度（进程树采集已实现，cgroup/eBPF 待定）

> **TL;DR**：G0 的**进程树采集器已实现并跑通管线**（bridge 每执行写 `cgroup_resource_v1` 工件 → runtime 收集 → kb-flush 用真实值）。真机实测结论：**per-exec cgroup v2 在 tool 容器被 Kata guest 的 cgroup 委托限制卡住；eBPF 无 BTF/tracefs，需重建 guest kernel 才能 CO-RE**。**CPU 捕获尚未在 CPU 密集命令上验证通过**（agent 的命令都是 <25ms 或 sleep；20ms 采样已提交但**还没打进镜像**）。

---

## 0. 仓库状态

- 本地 HEAD：`24d5ad8`（= origin/main）
- 远端（`weitianc@193.124.7.2`，`~/ClawBox`）在 `bd719f2`，**下次需 `git -c http.proxy= -c https.proxy= fetch && reset --hard origin/main`**（git 配置硬编码 socks5 代理，必须用 `-c` 关掉；详见 §7）
- 工作区干净（本次已清理所有未跟踪临时脚本 + `.vscode/tasks.json` + 根目录临时 txt/log）

## 1. 已实现（代码核实，测试绿）

### 1.1 G0 采集器（本轮核心，`toolbridge/collector.go`）
- **进程树采样**（`source=process-tree`，零特权，默认路径）：
  - 按 **shell pid 递归遍历 `/proc/<pid>/task/<pid>/children`**（不依赖 guest pgrp——实测 Kata guest 里容器进程 pgrp=0，`Setpgid` 不生效）
  - 累计 utime/stime/VmRSS 峰值/io 字节；**20ms 采样 + 启动即采基线**（`CLAWBOX_COLLECT_INTERVAL_MS` 可调，提交 `9e84ce6`）
- **per-exec cgroup v2**（`source=cgroup-v2`，尽力而为，失败自动回退进程树）：
  - `tryPerExecCgroup` 直接写 shell pid 进 `cgroup.procs`（不依赖 pgrp 过滤，提交 `c9d65d5` 修了多返回值位置错配 bug：`_, pgrp, _, _, ok`）
  - Finish 时仅当 cgroup 有真实计数（`userUs+sysUs>0 || pids>0`）才标 `cgroup-v2`，防空 cgroup 误标
- 工件：`<logdir>/tool-resource/cgroup-resource-<execution_id>.json`（`cgroup_resource_v1` 格式，与 ClawTune schema 互通）
- `main.go` 的 `runCommand` 现在用采集器真实值替换"只统计 shell"的旧 rusage

### 1.2 数据流接线
- `scripts/runtime-entrypoint.sh`：finalize 时用 base64+分隔符从 Tool VM 拉取 `tool-resource/cgroup-resource-*.json`（不依赖远端 tar）
- `scripts/kb-flush.py`：join 后若有匹配工件，用真实 `cpu_time_s`/`rss_peak_bytes`/quality **覆盖 span 代理值**（HMAC 签名前）
- `scripts/m1-realtask.sh`：`TASK_IMAGE` 可经 env 覆盖（`24d5ad8`）

### 1.3 真机镜像（已推送 registry `127.0.0.1:5000/clawbox`）
| 镜像 | tag | digest | 说明 |
|---|---|---|---|
| `swe-rebench-arm64` | `g0-fix` | `d424fb2440bb...` | **含 descendant-walk 修复，但不含 20ms 采样**（20ms 是之后提交） |
| `runtime-arm64` | `dev` | `529c1ad85812...` | 含新 runtime-entrypoint + kb-flush（工件收集） |
| `control-plane-arm64` | `dev` | 不变 | G0 无需改 |

> **⚠️ 关键**：下次跑任务前必须用 `9e84ce6`（20ms）重建任务镜像 overlay，否则 CPU 仍抓不到短命令。

## 2. 真机实测结论（决定 G0 走向的证据）

### 2.1 cgroup（`scripts/probe-kata-guest.sh` 增强版已跑）
- probe pod（带 CAP_SYS_ADMIN）：`/sys/fs/cgroup` 挂 `ro` → **`mount -o remount,rw` 成功** → mkdir per-exec cgroup 成功 → `echo $$ > cgroup.procs` **成功** → `cpu.stat` 读出真实值
- **tool 容器**（`g0-fix` 镜像）：cgroup 挂 `rw`、mkdir 成功，但 **`echo +cpu > .../cgroup.subtree_control` 失败**、**`echo $$ > leaf/cgroup.procs` 失败**（exit 1，errno 未取到）——**Kata guest cgroup 委托/nsdelegate 限制**，容器内绕不过
- root `cgroup.controllers = cpuset cpu io memory hugetlb pids`，root `subtree_control = cpu pids`

### 2.2 eBPF
- guest 无 `/sys/kernel/btf/vmlinux`（无 BTF → 无 CO-RE）、无 `/sys/kernel/tracing/kprobe_events`（无 tracefs kprobe attach）、无内核头/clang（无 BCC）
- `unprivileged_bpf_disabled=0`，`bpf()` syscall 可用
- **结论**：guest 内 CO-RE/kprobe eBPF 被 BTF/tracefs 挡住 → 需重建 Kata guest kernel（`CONFIG_DEBUG_INFO_BTF` + kprobes/tracepoints）；宿主侧 eBPF 是另一选项（cell 级数据，per-command 要时间线 join）

### 2.3 进程树采集器
- 真机跑通：**抓到真实 RSS（1536000 bytes / 1.5MB）**，213 个工件 / 217 次执行，`sampling_point_count` 300-1200
- **CPU 未验证**：agent 命令全是 <25ms（100ms 采样抓不到）或 30-120s sleep（真实 CPU≈0）；20ms 采样已提交但未上线

### 2.4 真实任务
- `run-01m0ewd8np3zs32zzy98zmmha0-a1`：`RuntimeRunning` → **`Succeeded`**（600s 短时限，g0-fix 镜像）
- M1 API：`127.0.0.1:8085`，token `clawbox-m1-smoke-token-0001`，tenant `tenant-a`
- 集群：`clawbox-cell-controller` / `clawbox-ingester` / `clawbox-tune-kb` 都 1/1 Running
- runtime 启动时 **KB pull 失败**（`WARN: KB pull failed; keeping cold-start snapshot`）——P2 sidecar KB 拉取是已知未闭合项

## 3. 未实现 / 阻塞（按优先级）

1. **🔴 CPU 捕获真实验证**：进程树能抓 RSS，但**没在任何 CPU 密集命令上证明 `cpu_time_s>0`**（20ms 采样未上线）。这是 G0 "确实管用"的最后一块拼图。
2. **🟠 per-exec cgroup v2 在 tool 容器不可用**：被 Kata guest cgroup 委托卡住。需定向诊断（拿 errno + 查容器 cgroup ns / Kata 配置能否委托子树）。若不可解 → 以进程树为准，cgroup 标注为 guest 限制。
3. **🔴 eBPF 未落地**：无 BTF/tracefs。两条路：①重建 guest kernel 开 BTF+kprobes → guest 内 CO-RE（论文核心贡献的最干净路径）；②宿主侧 eBPF → cell 级 + 时间线 join（零重建但归因弱）。
4. **🟡 runtime KB pull 失败**（P2）：`clawbox-tune-kb` 服务可达性/格式问题，需排查。
5. **🟡 短命令（<20ms）进程树抓不到**：20ms 间隔下约 20ms+ 的命令能抓到；<20ms 的报 ~0（可接受：近零资源）。

## 4. 架构决策（已在真机验证的结论）

- **per-execution 采集必须放 Tool VM 内**（宿主只看到整个 firecracker 进程；ClawTune sidecar 在 Runtime VM 只看到 SSH 客户端）。宿主只做 VM 级交叉验证。
- **ClawTune 复用边界**：复用 plugin（span/execution_id envelope）、sidecar（LLM proxy/KB/预测）、工件格式（`cgroup_resource_v1`）；**不复用** ClawTune 的 Python 采样器（`clawtune_sidecar/telemetry/cgroup_resource.py` 在 Runtime VM 采样，够不到 Tool VM 进程）——Tool VM 采集是 bridge 内自研 Go 实现。
- ClawTune 当年"eBPF 不麻烦"是因为设计假设宿主内核；在 ClawBox 部署里它的 eBPF 从没真正跑过（GAPS G0 的由来）。

## 5. 论文主线建议

> **以进程树采集为准（真实 CPU/RSS）+ cgroup 尽力而为 + eBPF 作增强层（需重建 guest kernel）**。eBPF 若作核心贡献，走"重建 Kata guest kernel 开 BTF + bridge 内嵌 CO-RE BPF 对象（cilium/ebpf）"最干净。

## 6. 下一步（新 session 按序做）

1. **重建任务镜像**：远端同步到 `24d5ad8` → `TAG=g0-fix2`（含 20ms bridge）→ 推 → `TASK_IMAGE` env 传新 digest 跑短任务。
2. **证明 CPU 捕获**：等 agent 跑 pytest（秒级 CPU），或经 runtime pod ssh 驱动 CPU burn（`find /testbed -type f` 会卡，用 `dd if=/dev/urandom of=/dev/null bs=1M count=50` 或 `timeout 5 find /testbed -maxdepth 4 -type f`）；检查工件 `cpu_time_s>0`。
3. **cgroup 定向诊断**：tool pod 里拿 `+cpu > subtree_control` 的真实 errno（`echo +cpu > f 2>err.txt; cat err.txt`），查容器 cgroup ns（`readlink /proc/1/ns/cgroup` 与 probe pod 对比）→ 决定是否 Kata 配置可解。
4. **eBPF 定案**：guest 里试 `mount -t tracefs tracefs /sys/kernel/tracing`；宿主查 `ls /sys/kernel/btf/vmlinux`；查 Kata kernel config 的 `CONFIG_DEBUG_INFO_BTF` → 选 guest 内 CO-RE vs 宿主侧。
5. 回填 `docs/GAPS.md` / `docs/CAPABILITIES.md` 状态。

## 7. 真机操作速查 / 坑

- `ssh weitianc@193.124.7.2`（BatchMode key 免密）；**kubectl 必须** `export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12; unset http_proxy https_proxy all_proxy`；**git 必须** `git -c http.proxy= -c https.proxy= fetch`
- PowerShell→ssh：带空格的 `commit -m` 会被 `-Command` 拆成 pathspec；远端命令内 `;`/`|`/`&` 会被本地 PowerShell 执行——**一律写脚本 scp 到远端跑**
- 本机无 Go/bash/docker → **Go 只能远端 docker 构建验证**（`docker/Dockerfile.tool-bridge` 已 COPY `collector.go`）
- 远端 docker 构建走 nohup+日志（`nohup ... > /tmp/x.log 2>&1 &`），避免 ssh pty 挂起
- 本机终端会因 ssh 连接超时"假死"（网络抖动时）
- 任务镜像构建：`BASE_IMAGE`/`TAG` 必须 **export**（`rebuild-swe-rebench-tool-overlay.sh` 用 `${BASE_IMAGE:?}`）
- 远端临时文件：`/tmp/` 下 g0-*.log/txt 可清理

## 8. 文件地图

| 区域 | 路径 |
|---|---|
| G0 采集器 | `toolbridge/collector.go` + `collector_test.go` + `main.go` |
| 数据流 | `scripts/runtime-entrypoint.sh`（收集）、`scripts/kb-flush.py`（真实值） |
| 提交入口 | `scripts/m1-realtask.sh`（`TASK_IMAGE`/`DEADLINE_SECONDS`/`IDEMPOTENCY_KEY` 可 env 覆盖） |
| guest 探测 | `scripts/probe-kata-guest.sh`（已加 cgroup remount rw / per-exec / bpf 门禁 / tracefs） |
| 镜像 | `docker/Dockerfile.tool-bridge`、`rebuild-swe-rebench-tool-overlay.sh`、`rebuild-runtime-image.sh` |
| 文档 | `docs/GAPS.md`、`docs/CAPABILITIES.md`、`scripts/README.md` |
