# AGENT HANDOFF 2026-08-19 —— 研究向下一步（监控+预测+KB 闭环 / 多沙盒并发 / Execution ID 精确 Join）

> 目标重定向：**科研论文**，不是产品。**跳过 M2 安全、M3 可恢复、M6/M7 GA 硬化**。
> 核心贡献 = **ClawTune 资源预测/调优**（基于真实工具执行观测学习预测 agent 工具资源），
> M1 managed 控制面作为 substrate（已够用）。
> 硬主线：`M0 ✅ → M1 🔶 → M4 闭环（主攻）→ 并发实验 → Execution ID 精确 join`

---

## 1. 当前状态（commit `97758eb` = HEAD，已 push origin/main）

### 已达成
| 项 | 证据 |
|---|---|
| M0 基线封住 | 99 tests 绿；3×真实 replay；并发 1/2/4/8/16 全绿（16=32 VM）；scale32 有文档化 FD 上限（~19） |
| ADR-001..010 | `docs/adr/`（fbc3cbe，Proposed） |
| M1 契约层 | `clawbox/managed/`（identity/state/models/db/repo）+ `clawbox/api/`（API/dispatcher/templates/schemas）+ Alembic + CRD v1alpha2 draft + RBAC 分离 + benchmark client |
| **M1 单真实任务** | run `01M0ADY8DNNWNJWVW7G42390RD` → Cleaned/Succeeded，agent 产出**正确 patch**（AttrPath namedtuple，commit c1ad059，与 gold PR#13 一致）；证据 `docs/m1-real-task-*` |
| **M1 3 并发真实任务** | 3 runs 同时 → 3 CR → 6 VM 并发 → **全部 Cleaned/Succeeded**，patch 1335/1175/1175（1175=gold），0 泄漏；证据 `docs/m1-concurrent-3x-evidence.md` |

### 关键发现（新 session 必须知道）
1. **patch 提取缺口**：runtime 用 `git diff`（工作区）收集 patch；agent 若 `git commit` 则结果 `patch_status=empty`（单 run 踩到，3 并发 run 因限时没 commit 所以正常）。→ 论文"任务成功率"指标必须修。
2. **SSH EOF 瞬态**：tool bridge 会周期性 `ssh handshake failed ... EOF`，runtime 重试可恢复但让 finalize 变慢（patch/trace 收集各可能拖 1-3 分钟）。job grace 600s 够。
3. **scale32 FD 上限**：kata shim soft RLIMIT_NOFILE=1024，~19 cells 并发上限（`docs/FINDING_2026-08-18-scale32-fd-exhaustion.md`）。论文并发做到 ~8-12 即可。
4. **SQLite 验证**：Postgres 镜像被 Docker Hub 代理阻断，M1 用 SQLite 完成真机验证（生产 PG 路径代码/清单就绪，论文不需要）。
5. **v1alpha1 CRD 仍在**：v1alpha2（runRef）未翻转（需 conversion webhook + controller），论文不需要。

---

## 2. 三大工作流：现状 + 下一步（按优先级）

### A. 监控 + 预测 + 知识库维护（M4 闭环）—— 论文主结果，最高优先

**现状（真机已在跑，但不可评估）：**
- ClawTune sidecar **in-process**（runtime 单容器，`scripts/runtime-entrypoint.sh` 写 `openclaw.patch.json`）：
  - `mode=observe, failOpen=true, executionBackend=hook-only, cgroup/affinity/NUMA=false`
  - trace `schema_version=6`，目录 `/state/<cell>/logs/`（sidecar.log / plugin.log / agent.log / patch.log）
- 每个工具调用输出 **shadow prediction**（latency bucket、lattice shrinkage/loso/max_cardinality、runtime p90 cpu/memory）——真机已见。
- **缺口**：无 observation 质量门/签名/去重、无 tenant KB overlay + generation、无 offline dataset（train/eval 分离）、无 prediction-vs-actual 评估、无 ablation。

**下一步（先本地，代码为主，不依赖真机）：**
1. 新建 `clawbox/tuning/`（ADR-008：从旧 `clawbox/scheduler/kb.py` 提取经验，不带旧 run/lease 生命周期）。
2. **Observation schema + validator**：tool span（execution_id、tool_name、command_digest、latency、cpu、memory、exit_code、stdout/stderr 摘要、collection_quality）+ 校验（schema 版本、身份、HMAC 签名用 ingest_secret、质量字段、去重键 = execution_id+tool+seq）。不合格 → diagnostic store，不进 KB。
3. **Offline dataset 导出**：脚本把 ingester/state 的 trace 导出为 jsonl/parquet（train/eval 分层），字段对齐 observation schema。
4. **Estimators**：把 ClawTune 的 lattice/shrinkage/latency-bucket 逻辑实现为可评估 Python estimator（latency 分桶分类器、per-command p90、memory residual quantile）；评估指标：MAE、bucket 准确率、校准。
5. **KB snapshot + generation**：由 trusted observations 构建（generation + provenance + 可回滚）；"第二次相似任务能读到新 generation + shadow prediction"。
6. **Ablation**：固定基线 vs shadow；有无 KB；有无 repo/tool 特征。

> 读代码起点：`scripts/runtime-entrypoint.sh`（sidecar 启动 + patch.json）、`clawbox/scheduler/kb.py`（旧 KB 经验）、`scripts/dump-result.sh` / `check-agent-log.sh`（数据读取路径）。

### B. 多沙盒并发 —— 已有 3 并发基础，扩到 6/8

**现状：** `scripts/m1-concurrent.sh`（提交 N run，deadlineSeconds=180 限时 → CR timeoutSeconds → agent --timeout）。3 并发已证全 Cleaned/Succeeded。

**下一步：**
1. 参数化 N=6/8 重跑；采集并发证据：RuntimeRunning 重叠、devmapper Data%、firecracker 进程数、每 cell result。
2. 输出 scale 表脚本（N=1/2/4/6/8 的成功率/时长/资源）。
3. 注意：同镜像并发解包不是幂等的（devmapper AlreadyExists），必须先预拉一次镜像（`sudo ctr -n k8s.io images pull --snapshotter devmapper <img@digest>`）；串行化已有先例。
4. 上限参考 ~19 cells（FD cliff），论文做到 8-12 即可，别撞墙。

### C. Execution ID 精确 join —— 数据质量地基（用户明确要求，不能时间模糊匹配）

**现状：** ClawTune hook span 与 tool-bridge 执行记录**没有共享 execution_id**（bridge 内部随机生成），join 靠时间启发式 → 数据不可信。

**下一步（按 ADR-007 core）：**
1. **Runtime 侧生成 execution_id（uuid4）**，在 ClawTune hook/span 里带上；把 execution_id 通过 SSH 命令 envelope 传给 tool bridge。
2. **Tool bridge 接受可选 execution_id**（改 `toolbridge/` 或 bridge 源码），回显在响应/observation 里，并写入 bridge trace。
3. **精确 join**：ClawTune span ↔ bridge execution record 用 execution_id 精确匹配；加测试断言 join rate=100%（无时间窗口）。
4. observation 记录带上 execution_id（喂给 KB/评估）。

---

## 3. 真机操作手册（新 session 必备）

### 连接
```
ssh weitianc@193.124.7.2   # BatchMode key；目标机 193.124.7.2 (Kunpeng arm64, openEuler)
# 本地 ssh 不在 PATH → 用全路径 C:\Windows\System32\OpenSSH\ssh.exe / scp.exe
```

### 当前运行中的 M1 smoke 栈（宿主 docker 容器）
```
clawbox-m1-api        (127.0.0.1:8085, SQLite ~/clawbox-m1-data/clawbox-managed.db)
clawbox-m1-dispatcher (CLAWBOX_CR_VERSION=v1alpha1, KUBECONFIG=/tmp/m1-kubeconfig)
token: clawbox-m1-smoke-token-0001     tenant: tenant-a
```
- API 提交 run：`curl -X POST http://127.0.0.1:8085/v1/runs -H "X-Clawbox-Token: ..." -H "X-Tenant-Id: tenant-a" -d '{...}'`（body 含 projectId/templateRef/templateRevision/inputRef/inputSha256/deadlineSeconds/idempotencyKey/problemStatement）
- 模板 registry：toolImage=任务镜像 digest、secretName=clawbox-llm、min/max deadline 60/3600
- 限时：`deadlineSeconds: 180` → agent `--timeout 180`（~10 分钟完成含 finalize）

### 关键脚本（在仓库 `scripts/`，宿主 `/tmp/m1-*.sh`）
| 脚本 | 用途 |
|---|---|
| `m1-concurrent.sh` | 提交 N 并发 run + 盯到 terminal（改 `N=`） |
| `m1-conc-status.sh` / `m1-conc-agents.sh` / `m1-conc-finalize.sh` | 并发 CR/agent/finalize 状态 |
| `m1-realpatch.sh` / `m1-sqlcheck.sh` | 从 tool VM 提取 agent 的 committed patch / 检查 sql.py |
| `m1-extract-patch.sh` | git status/diff 检查 |
| `rebuild-control-plane-image.sh` | 后台重建+push 控制面镜像（nohup + /tmp/clawbox-build.log） |
| `verify-managed-image.sh` | 验证镜像 entry points + alembic |
| `dump-result.sh <task_id>` | 从 ingester 读 result（task_id = CR 名，如 run-xxx-a1） |
| `check-agent-log.sh <prefix>` | agent 工具行为（read/edit/pytest 计数） |
| `single-image-scale.sh` | M0 旧路径并发压测（直接写 CR） |

### 重建镜像（改了 clawbox 代码后）
```
cd ~/ClawBox && env -u http_proxy -u https_proxy -u all_proxy git pull --no-rebase origin main
nohup bash /tmp/rebuild-control-plane-image.sh >/dev/null 2>&1 &   # 完成后 cat /tmp/clawbox-build.log
```
镜像 `127.0.0.1:5000/clawbox/control-plane-arm64:dev`（digest 每次变）；重建后重建 smoke 容器（`/tmp/m1-fix2.sh` 会 recreate API+dispatcher+提交 run）。

### 已知坑（重要！）
- **PowerShell→ssh 剥引号**：复杂远程命令写成脚本 scp 过去跑，绝不用内联 `$(...)`/带 `|` 的 grep（会被本地 PowerShell 执行）。单词 grep 例外（`grep -c firecracker`）。
- **docker 输出流经 SSH pty 会卡死持久终端**：docker 命令一律 nohup → 日志文件 → cat；卡死就 kill 终端。
- **kubectl/kubernetes client 必须 NO_PROXY**：`export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12; unset http_proxy https_proxy all_proxy ...`。
- **同镜像并发解包不幂等**：并发前先 `sudo ctr -n k8s.io images pull --snapshotter devmapper <img@digest>`。
- **不要 scp 覆盖正在运行的脚本**（会中途语法错误）。
- 任务镜像 digest `bdf4637498...`；LLM secret `clawbox-llm`（真实凭证，agent 真能跑）。

### 数据/证据位置
- `release-evidence/`（gitignore，宿主）· `docs/FINDING_2026-08-18-m1-live-validation.md` · `docs/m1-real-task-patch-c1ad059.txt` · `docs/m1-concurrent-3x-evidence.md`
- ClawTune trace：runtime pod 内 `/state/<cell>/logs/{sidecar,plugin,agent,patch}.log`；ingester DB（Job 已 Completed，不能 exec；历史数据在 pod 卷里）

---

## 4. 下一步建议执行顺序（新 session）

1. **Execution ID 精确 join（C）**：改动小、数据质量地基，先做（bridge + runtime + 测试）。
2. **Observation schema/validator + offline dataset + estimator 评估（A1-A4）**：本地纯代码，产出论文主结果的数据管线。
3. **patch 提取修复**：runtime 结果收集改为 `git diff` + 最后 agent commit 兜底（`git show HEAD`/`git diff HEAD~1`），修"成功率"指标。
4. **KB generation + ablation（A5-A6）** + **并发扩到 6/8（B）** 出论文图表。

> 提交纪律：每个 phase 独立 commit + push origin main；真机操作按上面坑清单执行。
