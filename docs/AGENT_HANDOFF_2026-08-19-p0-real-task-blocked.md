# AGENT HANDOFF 2026-08-19 —— P0 真机验证进行中：基础设施已修复，卡在 devmapper 快照冲突

> 目标不变：科研论文。核心贡献 = ClawTune 资源预测/调优。主线：`M0 ✅ → M1 ✅(3并发) → C execution_id join ✅ → A tuning 管线 ✅ → P1/P2/P4 本地代码 ✅ → P0 真机验证（进行中，卡 devmapper）→ 并发/真实 trace 出图`。

---

## 1. 本 session 已完成并推送（HEAD=`8c29e1e`，origin/main 同步，工作树干净）

| Commit | 内容 |
|---|---|
| `e5c52b3` | **P1** control-plane KB 持久化：`clawbox/tuning/{store,projector,server}.py` + 15 tests（tenant×repo、HMAC、append-only 去重、generation 快照、BEGIN IMMEDIATE 并发） |
| `79ecdc3` | **P2** runtime-entrypoint 拉/刷 KB hooks + `kb-flush.py` + in-cluster tune-kb 服务 + 3 tests |
| `c71bb73` | **P4 CLI** `python -m clawbox.tuning`（schema→validate→join→dataset→estimators→kb→ablation 离线管线）+ 2 tests |
| `af7afa0` | **P3** kata shim RLIMIT_NOFILE wrapper + FC-0 audit gate（未在真机执行，需 sudo） |
| `0dbb15b` | P0 脚本：recreate M1 smoke 栈 + 真实任务 verify + join check |
| `bdd3e1e`,`2b841fa` | **dispatcher kubeconfig 挂载修复**：`~/m1-kubeconfig/config` 文件挂载 + **644 权限**（容器非 root，600 会 PermissionError） |
| `ca9ac58` | **controller/ingester Pending 修复**：deploy 清单加 control-plane + disk-pressure toleration |
| `8c29e1e` | `m1-kb-submit.sh`（提交+watch 真实任务）、`m1-p0-joincheck.sh`（execution_id join + DeepSeek 证据）、`remote-disk-clean.sh`（磁盘清理） |

---

## 2. 真机（193.124.7.2, Kunpeng arm64/openEuler）状态

### 已修复（本 session 实锤）
1. **dispatcher 崩**：`PermissionError /mnt/kube/config` → 原因容器非 root + 文件 600；修法 kubeconfig `chmod 644` + 文件级 bind mount。dispatcher 现 Up 稳定。`run.queued` warning 是良性自事件。
2. **controller/ingester 16h Pending**：节点有 `node.kubernetes.io/disk-pressure:NoSchedule` taint（磁盘 94%，2.8T/3.0T）。加 toleration 后可调度，但**仍被 eviction**（nodefs<10%）。
3. **磁盘 94% → 85%**（464G free）：根因 docker 512GB 镜像（其中大量 `swerebench/sweb.eval.x86_64.*` 每个 3.5-6.4GB）+ containerd。`docker image prune -a -f` + 显式删 swerebench x86_64 + builder prune（`/tmp/disk-clean.sh`，setsid 分离跑）。taint 已消，controller/ingester **Running 1/1**。
4. 集群健康：node Ready、`clawbox-llm` secret = `https://api.deepseek.com`（真 DeepSeek，agent 真能跑）。

### 当前卡点（P0 真实任务未跑成）
- 提交了真实任务 **RUN=`01M0CFWWH7G5DAZ21X9N0ZRS6H`**，CR=`run-01m0cfwwh7g5daz21x9n0zrs6h-a1`，推进到 **ToolStarting**，tool pod `*-tool` 卡 **ContainerCreating**。
- **根因 = devmapper 快照冲突**（kubelet 事件）：
  `failed to save metadata for device "clawbox-fc--pool-snap-811" (parent: "clawbox-fc--pool-snap-1"): device ... is already there {DeviceID:100 ... State:Deactivating ...}: already exists: unknown`
  → 旧快照 `clawbox-fc--pool-snap-811` 卡在 `Deactivating`，snapshotter 无法复用；kubelet 重试 15+ 次全失败。与 repo 记忆里的 devmapper 陈旧状态问题同类。清理脚本的重 I/O 加剧了它。
- `sudo` 需要密码（weitianc 无 NOPASSWD）→ **无法** dmsetup remove 该快照 / 重启 containerd / 清 snapshotter 元数据，除非用户介入。
- 该任务 deadline 600s 已过，预计 **TimedOut**（controller 会 Cleaned 清理）。旧 CR（`run-01m0ceg91xax10362gakjchy2a-a1`、`run-01m0ccssb7fqpvng1f7cpb3bhd-a1`）也 8h 老，正被 admit/cleanup。

---

## 3. 下一步（按序；先解锁 P0）

1. **清 devmapper 陈旧快照**（需要 sudo 或用户）：
   - 目标：`clawbox-fc--pool-snap-811`（及任何 `State:Deactivating` 残留）。
   - 命令（在目标机）：`sudo dmsetup remove /dev/mapper/clawbox-fc--pool-snap-811`（或 `sudo lvs` / `sudo dmsetup status clawbox-fc--pool` 先看）；若不行，`sudo systemctl restart containerd`；最坏按 repo 记忆全量清 `io.containerd.metadata.v1.bolt` + devmapper 目录（保留 content store）。
   - 或：先确认 `/tmp/disk-clean.sh` 是否已跑完（I/O 消退后重试，Deactivating 可能自清）。
2. **重跑真实任务**：`bash /tmp/m1-kb-submit.sh 600`（setsid 分离，日志 `/tmp/m1-kb-submit.log`）→ 等 CR 到 `ToolRunning → RuntimeRunning → Cleaned/Succeeded`。
3. **验证**：`bash /tmp/m1-p0-joincheck.sh`（span↔bridge execution_id join rate=100%、execution_source=runtime-envelope、DeepSeek llm spans、patch_status=present）。
4. **P2 真机验证**：deploy tune-kb（`deploy/` 里的 KB 服务）+ runtime 设 `CLAWBOX_KB_ENDPOINT`/token → 第二次同 repo 任务读到 generation>0 快照。
5. **P3**（shim wrapper，需 sudo）、**P4 真实 trace 出图**、**P5 32 并发**（FD cliff 需先 P3）。

---

## 4. 本 session 新踩坑（务必看）

- **PowerShell→ssh 灾难级慢**：真机负载极高（另有用户 FAISS 任务占满 CPU），ssh 往返 1-10+ 分钟、频繁挂死。**策略**：长命令写成脚本 scp 过去 setsid/nohup 分离跑 + 轮询日志文件；一个挂死的终端就 `Ctrl+C` + `run_in_terminal(mode=async)` 开全新终端；绝不在一个终端上连续堆命令。
- **kubeconfig bind mount**：容器非 root 时宿主 600 文件 = PermissionError，必须 644；且要**文件级挂载**（目录挂载→IsADirectoryError）。
- **磁盘 eviction 链**：root fs >90% → kubelet 自动加 `disk-pressure:NoSchedule` taint + **evict 所有 pod**（toleration 挡不住 eviction）。必须先释放磁盘到 nodefs>10% free。
- **磁盘大头**：`swerebench/sweb.eval.x86_64.*` 镜像（3.5-6.4GB/个，数百 GB），`docker system df` 的 reclaimable 很准；`docker image prune -a -f` 才是正解（`-f` 只清 dangling 无用）。
- **devmapper 快照 Deactivating 冲突**：清理脚本大 I/O 时建 kata 沙箱快照易撞陈旧 `State:Deactivating` 设备 → `already exists: unknown`，kubelet 无限重试。需 dmsetup/containerd 干预（sudo）。
- 远程 git 代理死了：`git -c http.proxy= -c https.proxy= pull` 绕过（HTTPS 直连 github 可通）。

---

## 5. 关键文件/证据位置

- 本地：`clawbox/tuning/{store,projector,server}.py`、`scripts/{m1-kb-submit,m1-p0-joincheck,remote-disk-clean,remote-m1-recreate}.sh`、`deploy/{cell-controller,trace-ingester}.yaml`（已含 toleration）。
- 目标机：`/tmp/{m1-kb-submit,m1-p0-joincheck,remote-disk-clean,disk-clean.log,m1-kb-submit.log,m1-kb-run.txt}.sh`；M1 smoke 栈 = docker `clawbox-m1-api`(127.0.0.1:8085) + `clawbox-m1-dispatcher`；token `clawbox-m1-smoke-token-0001`、tenant `tenant-a`。
- 远程 `~/ClawBox` 同步到 `8c29e1e`；`~/ClawTune` v2=`80d4408`。
