# 已支持功能（代码核实版）

> 依据：仓库代码 + `pytest`（当前 **163 passed, 0 failed**）+ 真机验收记录。
> 每项给出代码位置；标注 ✅=已真机验证，🟢=代码+测试验证（未真机），🟡=部分/受环境限制。

## 1. 底层：Kata + Firecracker 双 VM 沙盒（M0，真机已验证）

| 能力 | 状态 | 证据 |
|---|---|---|
| 每任务双 Firecracker 微 VM（Tool VM + Runtime VM），`kata-fc-arm64`，无架构/VMM fallback | ✅ | `deploy/sandboxtask-crd.yaml`、`clawbox/cell/controller.py`、`README.md §1` |
| Cell 状态机 `Queued→…→Cleaned`，最终一致性 + finalizer 清理 | ✅ | `clawbox/cell/controller.py`（`CellPhase`/`CellReconciler`） |
| 全 Cell 原子准入/容量预留（small/medium/large profile + RuntimeClass overhead + 10% 余量） | ✅ | `clawbox/cell/capacity.py`、`scripts/collect-node-capacity.py` |
| SSH 凭据生成、NetworkPolicy fail-closed、Tool 不接触 LLM/upload 凭据 | ✅ | `clawbox/cell/manifests.py`、`clawbox/cell/controller.py` |
| 主机 bootstrap / FC-0~FC-5 gate / devmapper thin pool / live smoke | ✅ | `scripts/bootstrap-openeuler-arm64.sh`、`audit-kata-firecracker-arm64.sh`、`arm64-kata-smoke.sh` |
| 规模阶梯 1/2/4/8/16（=32 VM）全绿；3 并发真实任务全 `Cleaned/Succeeded` | ✅ | `docs/m1-concurrent-3x-evidence.md`、`scripts/scale-swe-rebench.sh` |

## 2. Managed 控制面（M1）

| 能力 | 状态 | 证据 |
|---|---|---|
| 多租户 API：`POST/GET /v1/runs`、cancel、retry、幂等 key（同 key 同体 200 重放 / 异体 409） | 🟢 | `clawbox/api/app.py`、`clawbox/managed/repo.py`、`tests/test_managed_api.py` |
| 租户隔离：`X-Tenant-Id` 作用域贯穿 API→repo→CR→runtime；跨租户读 404 | 🟢 | `clawbox/managed/repo.py`（所有查询按 tenant_id）、`tests/test_multitenant.py` |
| Run/Attempt/Event 生命周期 + 事件流/outbox 审计 + 重启安全 | 🟢 | `clawbox/managed/{models,state,repo}.py`、`clawbox/api/dispatcher.py` |
| Dispatcher：唯一 CR 创建者，outbox 驱动，CR 应用幂等 | 🟢 | `clawbox/api/dispatcher.py`、`tests/test_dispatcher.py` |
| Alembic 迁移 + PostgreSQL 兼容 schema（真机用 SQLite 验证，PG 路径代码就绪） | 🟡 | `alembic/`、`clawbox/managed/db.py`、`docs/AGENT_HANDOFF_2026-08-19-research-next.md` |
| **多租户模拟提交**（K 租户 × N 任务 CLI + 真机薄壳） | 🟢 | `clawbox/benchmark/multitenant.py`、`scripts/m1-multitenant.sh`、`tests/test_multitenant.py` |
| 单真实任务端到端（真实 DeepSeek LLM、agent 产出正确 patch） | ✅ | `docs/AGENT_HANDOFF_2026-08-19-p0-accepted.md`（run `01M0CVS4...`） |

## 3. 观测 / trace / join（数据地基）

| 能力 | 状态 | 证据 |
|---|---|---|
| ClawTune trace v6（LLM spans + tool spans）经 LLM proxy 真实采集 | ✅ | P0 验收 96 LLM / 94 tool spans（`docs/AGENT_HANDOFF_2026-08-19-p0-accepted.md`） |
| **execution_id 精确 join**（runtime envelope → tool-bridge 回显，无时间窗口） | ✅ | P0 `join_rate=1.0`（13/13）；`toolbridge/main.go`（`parseExecEnvelope`）、`clawbox/tuning/join.py` |
| trace/result/artifact 集中 ingester（HMAC 任务 token、chunk 校验、receipt） | ✅ | `clawbox/ingester/`、`scripts/artifact-uploader.py` |
| **三方 join 管线**（span + bridge + cgroup 工件），含未匹配上报 | 🟢 | `clawbox/tuning/{schema,join,dataset}.py`、`tests/test_cgroup_join.py` |

## 4. ClawTune KB / 预测（M4 论文主线）

| 能力 | 状态 | 证据 |
|---|---|---|
| 控制面 KB 持久化：按 `(tenant_id, repo_fingerprint)` append-only、HMAC 签名校验、去重、generation 快照、可回滚 | 🟢 | `clawbox/tuning/{store,projector,server}.py`、`tests/test_tuning_store.py` |
| 离线科研管线 CLI：schema→validate→exact join→dataset→estimators→KB→ablation→summary | 🟢 | `clawbox/tuning/__main__.py`（`python -m clawbox.tuning`）、`tests/test_tuning_cli.py` |
| runtime 拉取 KB snapshot（按 tenant×repo）+ finalize 时 observation 回刷（fail-open） | 🟡 | `scripts/runtime-entrypoint.sh`（P2 段）、`scripts/kb-flush.py` |
| ClawTune 影子预测（latency bucket / resource class / p90）在真机产生 | ✅ | `docs/AGENT_HANDOFF_2026-08-19-research-next.md`（真机已见 shadow prediction） |
| **KB 数据真实性**：目前喂的是"代理值"（插件侧覆盖率 + bridge 直接子进程），**非** Tool VM 命令的 cgroup/eBPF 消耗 | ⚠️ | `docs/GAPS.md` 第 1 条 |

## 5. 图像供应链 / 安全边界

| 能力 | 状态 | 证据 |
|---|---|---|
| ARM64 SWE-ReBench 镜像工厂（digest 映射、`unsupported-arm64` 拒绝、不可变 digest 提交） | ✅ | `clawbox/images/`、`scripts/build-swe-rebench-arm64.py` |
| 镜像必须 `image@sha256:...` 才可调度；Tool VM 永无 LLM/upload 凭据 | ✅ | `clawbox/cell/controller.py`（`validate_task`）、`clawbox/cell/manifests.py` |
| Tool Bridge：静态 Go SSH 桥、bounded 输出、进程级 user/system cpu + maxrss、`tool-bridge.jsonl` | ✅ | `toolbridge/main.go` |

## 6. 已知但受限/未完成的能力边界

详见 `docs/GAPS.md`。要点：Tool VM 内 **cgroup 写只读**（无 per-execution cgroup）、**无 BTF/内核头**（BCC/CO-RE eBPF 不可用，需非 CO-RE loader 实测）、admission/placement/NUMA/主动调优未接线、v1alpha2 未翻转、多节点 HA/32+ 并发未做、Postgres 未真机验证。
