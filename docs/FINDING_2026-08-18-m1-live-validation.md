# M1 真机验证：Managed API → Dispatcher → SandboxTask → 真实 Cell

> 日期: 2026-08-18 · 目标机 `193.124.7.2` (Kunpeng arm64, openEuler 24.03) · 提交 `a4ea8d7` + 后续

## 结论 (Summary)

M1 的**前向控制路径**在真机上完整打通并跑起了真实 Firecracker 微 VM：

```
POST /v1/runs (Managed API, SQLite)
  → run.accepted + outbox (单事务)
  → Dispatcher: attempt.created → SandboxTask CR (v1alpha1, 名称小写化)
  → 既有 Cell Controller 接管: Queued → Admitted → ToolStarting → ToolReady → RuntimeRunning
  → 真实 Tool + Runtime 两个 microVM 均 1/1 Running
```

- **Run ID**: `01M0ACVX2JXCVAHWPNAZBR5F2P`（ULID，26 字符）
- **Attempt #1**: `01M0ACVXQ0DYG7E6C5HJHWNCGM`
- **CR**: `run-01m0acvx2jxcvahwpnazbr5f2p-a1`（名字用小写 ULID）
- **幂等重放**: 同 key 第二次 POST 返回同一 runId + `idempotencyReplay:true`（201 → 200）
- **Run events**（经 API 查询）: `run.accepted → attempt.created → run.queued`，sequence 1..3

## 真实 agent 任务（M1-15, 2026-08-18）

补上 `problemStatement` 通道（M1-15, commit `51ba8b7` + migration `bf40a779a85c`）后，
通过 M1 API 提交了**真实 SWE-ReBench 任务** `15five__scim2-filter-parser-13`：

- **Run ID**: `01M0ADY8DNNWNJWVW7G42390RD`；**CR**: `run-01m0ady8dnnwnjwvw7g42390rd-a1`
- CR 中确认携带**真实问题文本**（NamedTuple/AttrPath 任务）+ 真实 LLM secret `clawbox-llm` + 真实任务镜像 digest
- Runtime 日志: ClawTune sidecar 启动 → ingester OK → **`runtime-root-ok`（SSH 无 WARN）** → agent 开始执行
- 预期 ~20 分钟产出真实 patch（与 M0 real001-001 的 21m/1175 字符 patch 同路径）

## 验证中修复的缺陷 (Bugs found & fixed)

| # | 缺陷 | 修复 | commit |
|---|---|---|---|
| 1 | 控制面镜像没有 alembic（只在 dev extras），容器里无法跑 migration | alembic 移到主 dependencies | `3a8b89a` (M1-10) |
| 2 | 镜像缺 `alembic.ini`/`alembic/`，`alembic upgrade` 无法在容器运行 | Dockerfile 拷贝迁移文件 | `7abe778` (M1-12) |
| 3 | API 忽略 `CLAWBOX_SERVICE_TOKEN` env，永远用默认 token → 所有请求 401 | `create_app(service_token=None)` 读 env；main 从 env 建 app | `0f513a7` (M1-13) |
| 4 | CR 名用大写 ULID → 违反 RFC 1123（422 Invalid metadata.name） | `_cr_name` 用小写 run id（runRef 保留精确 id） | `a4ea8d7` (M1-14) |
| 5 | Dispatcher 容器 uid 10001 读不到 `~/.kube`（700） | 拷贝 + chmod 644 + 只读挂载 KUBECONFIG | (host 侧 smoke) |

## 环境与运行方式

- 因 Docker Hub 被宿主代理阻断，Postgres 镜像拉不下来 → **用 SQLite 完成 M1 前向链路真机验证**；PG + Alembic 生产路径在代码/清单中就绪，待镜像源可用后部署。
- 集群仍在 **v1alpha1** CRD（装 v1alpha2 需先部署 conversion webhook + controller 翻转，ADR-003 部署说明已注明）。Dispatcher 用 `CLAWBOX_CR_VERSION=v1alpha1` 写 CR；v1alpha1 会剪掉 runRef/desiredState（Warning 299），执行字段完整。
- 宿主容器：`clawbox-m1-api` (8085) + `clawbox-m1-dispatcher`，数据在 `~/clawbox-m1-data/`。
- 脚本：`scripts/m1-live-smoke.sh`（一次性流程）、`scripts/m1-fix2.sh`（重建两容器+建 run）、`scripts/m1-evidence.sh`（收集证据）、`scripts/m1-status.sh`、`scripts/rebuild-control-plane-image.sh`、`scripts/verify-managed-image.sh`。

## 尚存事项 (Remaining for M1/M3)

1. **CR → API 状态回传**：CR 的 Cell phase（ToolStarting→RuntimeRunning…）尚未镜像回 Run/Attempt phase（Attempt 仍 PendingDispatch）；这是 M3 reconciler/informer 的范围（roadmap §8），M1 只要求前向契约。
2. **PostgreSQL 生产路径**：postgres 镜像被代理阻断；需 host 内网镜像源或 k8s 内 Postgres 后再做 Alembic→PG 验证与部署 `deploy/managed-control-plane.yaml`。
3. **v1alpha2 翻转**：conversion webhook handler（M1-4/M2 范围）+ controller 翻转后，runRef/desiredState 才真正落在 CR 上。
4. 本次 run 的 cell 预期在 RuntimeRunning 阶段因 LLM 占位符/坏 problem 失败（与 M0 一致），平台链路已证明。
