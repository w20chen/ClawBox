# ClawBox 交接：从真实单任务 MVP 到 Managed Agent Sandbox System

> 日期：2026-08-18（Asia/Shanghai）
>
> 基线：`main` @ `ce9252b`，上一份真实任务交接是 `docs/AGENT_HANDOFF_2026-08-17-real-task.md`
>
> 本文定位：**后续工作的权威总路线图**。它不改写已完成里程碑的历史证据，但后续实现顺序、架构决策、验收门和发布口径以本文为准。

---

## 0. 执行摘要：现在在哪里，最终要到哪里

ClawBox 已经证明了一个真实 SWE-ReBench 任务能在两个 Kata + Firecracker 微 VM 中完整跑通：Tool VM 提供 `/testbed` 和静态 Tool Bridge，Runtime VM 运行 OpenClaw + ClawTune，Agent 成功读写源码、执行测试、产出 patch，trace/result 收到中央 ingester receipt 后 Cell 进入 `Cleaned/Succeeded`。这是可运行 MVP，但还不是 managed service。

最终目标是一个 **managed agent sandbox system**：租户通过稳定 API 提交 Agent Run，系统负责身份、策略、镜像供应链、资源预留、微 VM 隔离、凭据、网络、观测、产物耐久化、取消/重试、清理、配额、公平性、容量与 SLO；ClawTune 从当前的旁路观测器演进为有安全边界的“观测 → 知识库 → 影子预测 → 有界调优”闭环。

### 0.1 必须保持的主线

- **生产执行路径只有一条**：`SandboxTask CRD → CellReconciler → Tool VM + Runtime VM`。
- `clawbox/scheduler`、`allocator`、`controller`、`node_agent`、`tool_agent` 是早期兼容控制面，可以拆取其 KB/lease/observation 思想和经验，**不得整体重接回 benchmark 路径**。
- 不能通过直接把 `enableCgroup`、`enableAffinity`、`enableNuma` 或 eBPF 开关改成 `true` 来宣称 ClawTune 完整闭环已实现。真实命令在 Tool VM，ClawTune 在 Runtime VM，两者不共享内核和 cgroup 层级。
- 先做可追溯观测和影子预测，再做任何资源执行；任何主动调优都必须有限额、回退、租户隔离和 kill switch。
- 工作项只有在代码、自动化测试、目标机证据三者都完成后才能标记完成。

### 0.2 里程碑依赖关系

```text
M0 封住当前基线
 └─ M1 Managed API + 持久任务身份/生命周期
     └─ M2 持久产物 + 凭据/策略/供应链
         └─ M3 可恢复单节点控制面 + 故障注入
             ├─ M4 ClawTune 观测/知识库闭环（不执行调优）
             │   └─ M5 Tool VM cgroup/eBPF 与有界执行
             └─ M6 多节点、HA、公平调度与 32+ 并发
                 └─ M7 服务化 GA（SDK、SLO、升级、备份、计量）
```

M4 可以在 M3 后与 M6 的多节点工作并行，但 M5 主动执行不得早于 M4 的影子预测验收。

---

## 1. 当前基线和必须显式承认的缺口

### 1.1 已经有真机证据的能力

- openEuler 24.03 LTS-SP1 / aarch64 / K8s 1.35.7 / Kata 3.31.0 / Firecracker 1.12.1 的 FC-0～FC-5 substrate gate 已通过。
- Cell 拥有独立 Tool Pod/VM 和 Runtime Job/VM，使用 `kata-fc-arm64`，不允许架构或 VMM fallback。
- 真实任务 `real001-001` 在 1800s Agent 预算下 21 分钟达到 `Cleaned/Succeeded`，产出 1175B patch，`agent_exit_code=0`。
- OpenClaw `read/write/edit` 已能成功针对 `/testbed` 工作，真实任务中未再触发 path-escape 拒绝。这只证明路径补丁的功能链路，**不是 sandbox escape 安全性证明**；guest-root/supervisor 风险见 §5.3 和 M2。
- OpenClaw plugin hooks、ClawTune LLM proxy、trace 增量上传、result/final marker/receipt 握手已完成真实闭环。
- Tool 不获取 LLM key 或 upload token，Runtime 通过任务专属 SSH key 访问 Tool Bridge，工作 Pod 不挂 ServiceAccount token。

### 1.2 当前真实实现形态（后续不能继续按旧图理解）

- 由于该 Kata 实现在容器间共享 volume 时失败，Tool Pod 已改成**单容器**：Tool Bridge 烘入任务镜像，不再使用 init container 拷贝。
- Runtime Job 也是**单容器**：ClawTune sidecar 是由 `runtime-entrypoint` 在同容器内启动的后台进程，不是 restartable init container。
- 因 Kata guest agent 把 Secret/ConfigMap volume data dir 写成 mode `0000`，Tool/Runtime 容器暂时在微 VM 内以 root 运行。隔离边界是 Firecracker VM，但这仍需要安全威胁模型和持续验证。
- `docs/CONCURRENT_KATA_SWE.md`、`docs/IMPLEMENTATION_MAPPING.md` 和部分旧交接仍描述 init/native sidecar，它们是文档漂移，不是当前 manifest 事实。

### 1.3 已知未完成项

| 领域 | 当前状态 | 为什么阻止称为 managed system |
|---|---|---|
| 自动化测试 | 2026-08-18 本地 `pytest` 为 21 通过/3 失败；3 个失败仍断言旧 init/sidecar/volume 布局 | `main` 不是全绿，架构契约没有被正确锁定 |
| 规模 | 真实成功样本为 1，4/8/16/32 并发未完成 | 不知道容量、成功率和清理泄漏曲线 |
| 控制器 | 单副本、2s 全量轮询、只支持恰好 1 个 Firecracker-ready node | 无 leader election、work queue、持久预留和水平扩展 |
| 入站 API | benchmark launcher 直接创建 CR | 无租户身份、幂等 key、配额、取消/重试 API、事件流和审计 |
| 数据耐久性 | ingester 默认 SQLite + `emptyDir` | Pod 丢失会丢结果，无对象存储、备份恢复或保留策略 |
| 凭据 | 长寿命 K8s LLM Secret + 控制器 HMAC 任务 token | 无 workload identity、租户级 secret broker、密钥轮换与撤销闭环 |
| 网络 | NetworkPolicy + 用户提供 CIDR；真实验证曾使用 `0.0.0.0/0:443` | 不足以表达域名/供应商策略，缺少集中 egress proxy 与请求审计 |
| ClawTune | `observe-only` + `hook-only`，eBPF/cgroup/affinity/NUMA 显式关闭 | 只证明了旁路 trace，没有“学习 → 预测 → 执行” |
| Tool 资源观测 | Tool Bridge 有 wall time、CPU time、MaxRSS 和输出字节数 | 无 cgroup v2 真实边界、OOM/IO/network/process tree/eBPF 证据，无规范化 observation 入库 |
| 稳定性 | runtime→tool SSH 偶发 255，已有 timeout 兜底 | 根因、频率、影响面和自动恢复无量化 |
| 任务身份 | ingester result 对 task ID 不可变，重用名字会 409 | 用户可见 Run 和内部 Attempt 未分离，重试语义不完整 |

---

## 2. Managed Agent Sandbox 的精确定义和 GA 验收口径

后续不要用“Pod 起来了”或“Agent 出了 patch”代替 managed system 验收。本项目的 GA 定义需要同时满足以下能力。

### 2.1 对用户可见的资源模型

1. **SandboxTemplate**：版本化的运行模板，包含 Agent/Runtime 版本、Tool 镜像策略、允许工具、资源边界、网络策略、产物与保留策略。发布后不可变，修改创建新 revision。
2. **AgentRun**：用户可见的逻辑任务，具有服务器生成的全局 ID、tenant/project、idempotency key、输入引用、模板 revision、总预算、期望状态和保留期。
3. **SandboxAttempt**：一次不可变执行尝试，对应一个内部 `SandboxTask`。重试始终创建新 Attempt ID，绝不复用 ingester result 主键。
4. **ArtifactManifest**：结果、patch、trace、日志、observation 的不可变 manifest，每个 blob 有 digest、size、media type、producer revision、脱敏级别和 retention。
5. **Policy**：租户可用模型、镜像、工具、egress、资源上限、并发、保留和调优模式的服务端策略，不由任务请求直接绕过。

第一个 GA 可只支持 batch AgentRun；交互式长寿命 sandbox/session 必须在 batch 生命周期、取消、审计和孤儿回收通过后再开启。

### 2.2 产品级不变量

- 所有工作负载镜像都是 registry digest，通过架构、签名、SBOM/来源和合同测试后才可调度。
- 每次 Attempt 是完整的两 VM 预留，绝不先启动一半 Cell。
- Tool VM 永远不获取 LLM key、中央产物读凭据或其他租户信息。
- Runtime/Tool 无 hostPath、无共享 PVC/RWX、无 Kubernetes API token，不得 fallback 到 runc/QEMU/cloud-hypervisor。
- 成功是指 Agent 结果与 final trace manifest 已中央持久化，不是主进程刚退出 0。
- 用户取消、超时、控制器重启、节点重启和上传重试都不能产生两个可写的同 ID result。
- 任务终态后必须在有界时间内清除 Pod、Job、Service、Secret、ConfigMap、NetworkPolicy、sandbox、shim 和 Firecracker 进程；未清除会阻止发布。
- 任何 KB 更新都必须是租户隔离、可去重、可追溯、可回滚的。身份/签名/schema/采集质量不合格的记录绝不训练；采集完整且可信的 timeout/OOM/failure 可作为 censored sample 保留，只有明确支持 censoring 并经离线验证的算法才可使用；第一版 active estimator 只使用 complete/trusted 成功样本。

### 2.3 GA 的最低量化门槛

| 类别 | 最低验收标准 |
|---|---|
| 正确性 | 所有 CI 全绿；CRD/API/DB migration 契约测试全绿；目标机真实 AgentRun 可重复 |
| 并发 | 在发布目标硬件上通过 1/2/4/8/16/32 阶梯，每阶段无平台侧结果丢失和 VM 泄漏 |
| 长稳 | 至少 100 个连续 Attempt 作为功能/soak 下限，将 Agent/模型失败与平台失败分开；对外采用 <1% 平台失败 SLO 前，必须在更大滚动窗口中报告样本量和置信区间，不只看 100 次点估计 |
| 清理 | 业务终态后 120s 内无活动 Tool/Runtime VM（p99）；T+15min 对账时 orphan shim/Firecracker/sandbox/snapshot/reservation = 0 |
| 取消 | API 接受取消后 120s 内停止 Agent/Tool、撤销 execution/LLM 凭据并清除活动 VM（p99）；artifact finalization 使用独立有界语义，不延长不可信 VM 存活 |
| 耐久性 | ingester/controller 重启不丢任务、result、trace manifest 或 KB generation；备份恢复演练通过 |
| 隔离 | 两租户并发恶意测试无文件、凭据、网络、K8s API、artifact 或 KB 交叉访问 |
| 供应链 | 拒绝 tag、非 ARM64、未签名、证明缺失或合同测试失败的镜像 |
| Artifact | `platformOutcome=Succeeded` 的 Attempt 所有 required artifact 100% 完整；failed/cancelled/interrupted 也必须有显式 `Complete/Partial/FinalizationPending/Failed` 状态，不允许静默缺失 |
| 可观测性 | Run/Attempt/Cell/VM/tool-call/artifact 全链路 correlation ID；可区分 admission、拉镜像、VM boot、Agent、Tool、LLM、upload、cleanup 耗时 |
| ClawTune | observation 去重入库、KB generation 和影子预测可追溯；主动模式必须在 canary 中证明无 SLO/成功率回归 |

---

## 3. 目标架构和信任边界

```text
Client / SDK / CLI
        |
        v
API Gateway -- OIDC/API key -- Rate limit -- Idempotency
        |
        +--> Run Service + PostgreSQL (tenant/run/attempt/audit/outbox)
        |              |
        |              +--> CR Dispatcher --> SandboxTask CRD
        |                                      |
        |                             Cell Controller(s)
        |                          leader election + workqueue
        |                                      |
        |                       durable Reservation / Placement
        |                                      |
        |               +----------------------+------------------+
        |               |                                         |
        |        Tool Pod / Firecracker VM                 Runtime Job / Firecracker VM
        |        - immutable task image                    - OpenClaw
        |        - Tool Bridge                             - ClawTune plugin/sidecar
        |        - per-exec collector                      - LLM proxy
        |        - cgroup actuator (later)                 - trace uploader
        |               ^                  task SSH                 |
        |               +------------------------------------------+
        |                                                          |
        +<-- Status/Event Projector <-------------------------------+
        |
        +--> Artifact Service --> object store (blob) + PostgreSQL (manifest)
        |
        +--> Observation Projector --> tenant KB store --> shadow prediction
        |
        +--> Policy/Quota/Image/Secret services
```

### 3.1 数据面与控制面边界

- API/Run Service 不直接创建 Pod/Job，只创建经策略解析后的内部 Attempt/SandboxTask。
- Cell Controller 是 Tool/Runtime 子资源的唯一 owner，不执行用户代码，不读 LLM 密钥明文。
- Runtime VM 是 Agent 与模型凭据边界；Tool VM 是不可信代码执行边界。Tool 只接受任务专属、短寿命、可审计的执行通道。
- ClawTune 在 Runtime VM 中负责会话/LLM 观测和决策；Tool 进程的内核级观测与执行必须在 Tool VM 内由 Tool Bridge/collector 完成。
- 中央 Artifact/Observation 系统只接受带 tenant/run/attempt/audience/scope/expiry 的凭据，所有路径都有 digest 和去重键。

### 3.2 必须提前做出的架构决策

1. **活跃执行的 source of truth 是 Kubernetes CR，用户和审计 source of truth 是 PostgreSQL。** Run Service 用 transactional outbox 记录创建意图，Dispatcher 幂等创建 CR，Status Projector 将 CR 事件回写为 API 投影；不得让 API 和 controller 同时任意更改同一业务状态。
2. **Run ID 与 Attempt ID 分离。** `POST /runs` 的 idempotency key 只防止重复创建 Run；`POST /runs/{id}:retry` 必须生成新 Attempt ID 和新 ingester namespace。
3. **大输入和产物不放 CRD/ConfigMap/DB 大字段。** API 将 prompt/repository bundle 存入对象存储，CR 只持有 digest 锁定的引用；在迁移期间保持当前 `problemStatement` 兼容。
4. **首个生产产物后端使用 PostgreSQL + S3 兼容对象存储。** PostgreSQL 保存 manifest/状态/审计/幂等键，blob 保存 trace/result/log/patch；不把 SQLite/PVC 当最终方案。
5. **eBPF 第一落点是 Tool VM，不是宿主 privileged DaemonSet。** 先实现 `/proc` + `rusage` + cgroup v2 标准 collector，再用经审计的 CO-RE/BTF 程序补充 process/file/network 事件。
6. **执行分层上线。** 全文统一使用 `observe → shadow → advise → canary-enforce → active-enforce`，每层均可在全局、tenant、template 三个维度一键降级到固定 profile。
7. **多节点前先引入持久 Reservation 与 leader election。** 当前通过枚举 CR status 重建内存预留只适用单 controller/单 node，不能直接将 `replicas` 改为 2。
8. **交互式 sandbox 是后续升级，不是第一个 GA 的隐式要求。** 先把有界 batch Run 做到可取消、可恢复、可审计，再设计 session lease/idle timeout/attach protocol。

---

## 4. 工作包与顺序总表

| ID | 工作包 | 依赖 | 主要产出 |
|---|---|---|---|
| M0 | 基线封口 | 无 | 测试全绿、文档与 manifest 一致、单任务和初始 scale 证据 |
| M1 | Managed API 与身份/生命周期 | M0 | Run/Attempt/Template/Policy API，幂等、取消、重试、事件 |
| M2 | 数据、凭据与供应链 | M1 | PostgreSQL/S3，artifact manifest，short-lived identity，egress/image policy |
| M3 | 可恢复控制面 | M1/M2 | informer/workqueue、leader election、reservation、reaper、chaos 证据 |
| M4 | ClawTune 数据闭环 | M2/M3 | Observation v1，中央 projector，tenant KB，shadow prediction |
| M5 | 资源观测与有界执行 | M4 | Tool VM collector/eBPF/cgroup，canary/kill switch |
| M6 | 多节点与容量 | M3（M4 可并行） | 节点级 placement/quota/fairness，HA，32+ 并发与长稳 |
| M7 | Managed service GA | M2～M6 | SDK/CLI、SLO、告警、备份恢复、升级、运营手册 |

每个里程碑下面的“完成定义”是阻断门，不得因时间压力跳过。

---

## 5. M0——封住当前可运行基线

**目标**：在添加新功能前，让代码、测试、文档和真机证据描述同一个系统。M0 期间不开放任何外部租户流量。

### 5.1 修复测试与架构契约

1. 更新 `tests/test_kubernetes_backend.py` 中 3 个过时断言，但不能只是删断言：
   - Tool 是单容器，任务镜像的 `/usr/local/bin/tool-bridge` 为启动命令；
   - Tool Secret projection 只包含 authorized public key 和 SSH host key，不包含 Runtime private key/upload token/LLM key；
   - Runtime 是单容器，entrypoint 以后台进程启动 ClawTune，`.runtime-complete → final upload → .upload-complete` 契约仍被静态和行为测试覆盖；
   - 两个 Pod 均使用 `kata-fc-arm64`、无 hostPath/PVC/ServiceAccount token，NetworkPolicy 仍 fail-closed；
   - 微 VM 内 root 是有证据的暂时兼容形态，测试应锁定当前实际安全边界并指向 M2 中的子进程降权，不应假装容器仍是 non-root。
2. 为渲染后的 Tool Pod、Runtime Job、Secret 和 4 个 NetworkPolicy 增加 golden/structural contract test；关键安全字段变化时必须显式更新 fixture。
3. 为 `runtime-entrypoint.sh` 的 ClawTune 配置增加解析级测试，不要只 grep 字符串；断言 `observe`、`hook-only`、fail-open 和 cgroup/affinity/NUMA 关闭。
4. 为 uploader 增加断网重试、重启恢复、同块幂等、不同内容 409、结果不可变和 receipt 缺失测试。M0 可以先锁定当前语义，M2 必须修复“trace 冲突后跳过整文件仍可 complete”的完整性缺口。

### 5.2 消除文档漂移

- 更新 `README.md` 顶部架构图、`docs/CONCURRENT_KATA_SWE.md` 和 `docs/IMPLEMENTATION_MAPPING.md`：当前 Tool/Runtime 都是单容器，ClawTune 是 in-process sidecar process。
- 保留旧交接作为历史证据，但在过时章节顶部增加链接，明确当前形态以本文和现代 manifest 为准。
- 将“已真机通过”、“只有本地测试”、“计划中”使用三种固定标记，禁止把计划当成已实现能力。

### 5.3 安全红线记录（M0 必须写清，M2 必须修）

当前 Tool Bridge 与不可信 shell 在同一 Tool 容器/微 VM 内以 guest root 运行。恶意命令理论上可以杀死 bridge、修改 `/testbed/.clawbox/tool-bridge.jsonl`、读取 Tool 端 Secret projection、创建脱离当前 process group 的后台进程。Firecracker 保护了宿主和其他 VM，但没有保护同一 Tool VM 内的 supervisor/audit 完整性。**在 M2 完成 guest 内权限分离前，不得将系统对不可信外部租户开放。**

另一个必须阻断的 confused-deputy 问题：当前 `SandboxTask.spec.llmSecretName` 能引用同 namespace 中的任意 Secret，controller ServiceAccount 会替 CR 创建引用它的 Runtime Pod。因此不得把 CR create 权限直接给租户。Managed API 只接受 tenant/project 绑定的 `credentialRef`/`modelProfileRef`，由服务端验证并换发 Attempt 级凭据。

### 5.4 重放基线和初始规模证据

1. 用唯一 run prefix 重跑 1 个真实任务，保留 CR YAML、Pod/Job YAML、事件、controller/runtime/tool/ingester 日志、result/trace receipt、patch digest 和镜像 digest。
2. 先跑 1/2/4，然后根据泄漏、devmapper 和 SSH 255 统计决定是否继续 8/16/32。任一阶段出现平台侧失败就停止放大，不用增大 timeout 掩盖根因。
3. 每阶段记录：admission wait、镜像拉取、Tool ready、Runtime ready、Agent、post-agent SSH、upload、cleanup 耗时；DNS/SSH/upload 错误次数；thin-pool Data/Meta%；shim/Firecracker/sandbox 前后数量。
4. 证据包必须记录 Git SHA、镜像 digest、K8s/Kata/Firecracker/kernel 版本和目标节点名；必须脱敏，不得收集 API key 或 Secret 原文。

### 5.5 M0 本地验收命令

```powershell
python -m pytest -q
python -m ruff check .
python -m mypy .
python -m py_compile clawbox\cell\controller.py clawbox\cell\manifests.py
Push-Location toolbridge
go test ./...
go vet ./...
Pop-Location
git diff --check
git status --short
```

Shell 语法检查应在 Linux CI 运行 `bash -n scripts/*.sh` 和 `shellcheck`；Windows checkout 必须确认 shell/Python entrypoint 保持 LF。

### 5.6 M0 完成定义

- 上述本地/CI 测试全绿，不存在因为旧架构假设导致的 xfail/skip。
- 权威文档与当前 manifest 相符，安全红线和非生产限制显式可见。
- 真实单任务至少连续重放 3 次无平台失败；这是进入 M1 的硬门。1/2/4 阶梯要有完整证据；若 4 失败，必须形成已定位 issue 和可重现脚本，它阻断 M3 容量/恢复门和 M6，但不阻断 ADR/M1 纯契约工作。
- M0 不代表安全多租户已完成；外部流量仍被 M2 安全门阻断。

---

## 6. M1——Managed API、任务身份与完整生命周期

**目标**：把“运维人员直接创建 SandboxTask”变成有身份、幂等性、状态语义和审计的服务 API。M1 只建控制面契约，不开启 ClawTune 主动调优。

### 6.1 API 最小面

```text
POST   /v1/runs                         Idempotency-Key 必填，创建 AgentRun
GET    /v1/runs/{run_id}                返回逻辑 Run 和当前 Attempt
GET    /v1/runs                         tenant/project/status/cursor 过滤
POST   /v1/runs/{run_id}:cancel         幂等设置 desired_state=Cancelled
POST   /v1/runs/{run_id}:retry          创建新 Attempt，不复用 task/result ID
GET    /v1/runs/{run_id}/events         有序、可续传事件
GET    /v1/runs/{run_id}/artifacts      返回 manifest，不直接返回大 blob
POST   /v1/templates                    管理员/有权用户发布 revision
GET    /v1/templates/{id}/revisions/{n}
```

所有接口使用 OIDC subject/service identity 导出 tenant/project，不接受请求 body 中自声明 tenant 作为授权依据。列表 API 必须是 cursor pagination，不允许一次枚举全部租户任务。

M1/M2 就必须先有最小防 DoS/成本硬限：API request rate、每 tenant 最大 queued/active Run/Cell、可选固定 resource class、Run wall-clock、LLM token/cost 和 artifact bytes。M6 再实现跨节点 fair queue、priority/aging 和高级配额。因此 M2 安全门过后也只能向受控试点租户开放，公开 GA 仍被 M6/M7 阻断。

### 6.2 Run/Attempt 状态机

API 层不直接暴露每个 Kubernetes 过渡态，但必须保留详细 event：

```text
Run:      Accepted -> Queued -> Running -> Finalizing -> Succeeded
                                           |          -> Failed
                                           |          -> TimedOut
                                           +----------> Cancelled

Attempt:  PendingDispatch -> Queued -> Admitted -> ToolStarting -> ToolReady
         -> RuntimeRunning -> Collecting -> Succeeded/Failed/TimedOut/Cancelled

Cleanup:  Pending -> Running -> Complete/Failed   (与 Attempt 业务终态正交)
```

- `Run.status` 是投影，`Attempt.status` 与 CR status/conditions 对应。目标契约中清理不覆盖 `Succeeded/Failed/TimedOut/Cancelled`，而是 `CleanupComplete` Condition；当前 `Cleaned + outcome` 只是 `v1alpha1` 兼容语义。
- 记录 `reason`、`message`、`observedGeneration`、`lastTransitionTime` 和标准 Conditions：`Accepted`、`Scheduled`、`ToolReady`、`AgentComplete`、`ArtifactsDurable`、`CleanupComplete`。
- 取消是 desired state，Dispatcher/Controller 幂等收敛。在 upload 已开始时取消，仍必须上传一个终态 manifest，标识 incomplete/cancelled，不得伪装成 success。
- retry policy 必须区分 platform-retryable 和 agent/user failure；默认不自动重跑可能有外部副作用的 Agent。

Outcome 不能继续压在一个 `status` 字段中。固定枚举与语义为：

| 字段 | 枚举 | 语义 |
|---|---|---|
| `phase` | 过渡态 + `Succeeded/Failed/TimedOut/Cancelled` | 对用户可见的生命周期；终态后不被 cleanup 覆盖 |
| `platformOutcome` | `Pending/Succeeded/Failed/Interrupted` | Cell/transport/storage/control-plane 是否完成平台契约 |
| `agentOutcome` | `Pending/Succeeded/Failed/TimedOut/Cancelled/Unknown` | Agent 进程/会话本身结果 |
| `artifactOutcome` | `Pending/Complete/Partial/FinalizationPending/Failed` | required/optional 产物的明确完整性 |
| `evaluationOutcome` | `NotRun/Passed/Failed/Error` | benchmark/用户评估，不与平台成功混淆 |

- Agent 返回非 0，但 VM/transport/artifact 正常：`phase=Failed`、`platformOutcome=Succeeded`、`agentOutcome=Failed`、`artifactOutcome=Complete`。
- worker 丢失：`phase=Failed`、`platformOutcome=Interrupted`，是否创建新 Attempt 由 retry policy 决定，不新增第二套 phase 枚举。
- `platformOutcome=Succeeded` 只在平台契约完成且 required artifact complete 时提交；Agent/evaluation 结果分开。
- terminal commit point 是带 Attempt fencing/version 的原子 DB event/CR condition 投影。若 durable completion 先提交，后到 cancel 返回 already terminal；若 cancel intent 先提交，Attempt 必须 `Cancelled`，即使 Agent 恰好返回也只作为竞态证据保留。
- 自动 retry 时旧 Attempt 已终态，Run 保持 `Running/Retrying`；只在 retry policy 决定不再创建 Attempt 后 Run 进入终态。
- cancel 在 120s p99 内停 Tool/Agent、撤销 execution/LLM/新通用 blob write 凭据并清理 VM。可保留最多 120s、只能写已知 closing segment/terminal manifest 的 finalization-only credential，或由 controller/artifact service 合成平台取消 manifest；然后全部撤销。存储不可用时不为等待 finalization 保留不可信 VM；只有已进入 VM 外耐久 spool 的数据可补偿，否则显式 `Partial/Failed`。

### 6.3 存储和一致性

- PostgreSQL 最少包含 `tenants`、`projects`、`templates`、`template_revisions`、`runs`、`attempts`、`run_events`、`idempotency_keys`、`outbox`、`audit_events`。
- `(tenant_id, idempotency_key)` 唯一，同 key + 同 request digest 返回既有 Run，同 key + 不同 digest 返回 409。
- API transaction 同时写 Run/Attempt 和 outbox；Dispatcher 以 Attempt ID 为幂等键创建 CR。创 CR 成功后才将 Attempt 标记 `Queued`。
- Status Projector 使用 watch stream + per-object UID/最后观测 `resourceVersion` 恢复和去重，`resourceVersion` 是 opaque token，**不用于跨对象业务排序**。用户 event 顺序使用 DB transaction 分配的 per-Run monotonic sequence；relist 后根据 generation/conditions 重建投影。数据库短暂不可用时不丢 CR 状态，恢复后可重建投影。

字段所有权固定如下，任何组件不跨界修改：

| 组件 | 唯一写权 |
|---|---|
| Managed API + PostgreSQL | 用户身份、Run intent、idempotency、cancel/retry intent、审计 |
| Dispatcher | 从 outbox 幂等创建 CR 不可变 execution spec，转发单调 desired state |
| Cell Controller | 只写 CR status/conditions 和其拥有的子资源/Reservation |
| Status Projector | 只追加 DB event/更新可重建投影，不反向改 CR spec/status |
| Artifact/KB services | 只写各自 manifest/observation/snapshot，通过引用/condition 与 Attempt 关联 |

### 6.4 CRD 演进

不要直接破坏 `v1alpha1`。新建 served 版本（建议 `v1alpha2`，语义稳定后再升 `v1beta1`）；在切换 storage version 前必须已实现并验证 conversion webhook、storedVersions migration、活动任务 fail-closed plan 和回滚，不是只“准备”测试。内部 spec 至少需要：

- `runRef`、`attemptId`、`tenantRef`、`projectRef`（服务端生成）；
- `templateRef` + immutable revision/digest；
- `inputRef` + SHA256，迁移期兼容 inline problem statement。首个 GA 可只开放平台 builder 已将 workspace 固化进可信 tool image 的模式；如开放动态输入，必须由受信 Input Materializer 使用只读单一 digest 的短凭据下载、校验、防 path traversal/symlink/device bomb 展开、chown 后撤销，agent UID 不触及凭据。Runtime prompt 同样由受信 materializer 取数；
- `modelProfileRef`，不再允许客户端指定原始 Secret 名；
- `networkPolicyRef`、`artifactPolicyRef`、`resourcePolicyRef`；
- `deadlineSeconds`、`commandBudget`、`maxOutputBytes`，以及独立的 `control.desiredState`。当前 `self == oldSelf` 需拆成 execution payload 不可变 CEL，另一条 CEL 只允许 `desiredState` 保持不变或 `Running → Cancelled` 单向转移，不允许取消回滚或修改执行输入；
- status 中的 `attemptId`、`nodeName`、`reservationRef`、`artifactManifestRef`、`conditions[]` 和每段时间戳。

### 6.5 代码落点

- 新建 `clawbox/api/` 或明确命名的 managed control-plane package，不把用户 API 塞进 benchmark launcher。
- 新建 Alembic migration，代替“启动时直接 create_all”作为生产 schema 策略。
- 在 `deploy/` 中分离 API、Dispatcher/Projector、Cell Controller 的 ServiceAccount 和最小 RBAC；API 没有 Pod/Secret 创建权限。
- benchmark launcher 变成 API client/验收工具。生产不保留直接 CR 写路径；break-glass 也必须经过有 JIT/MFA/审批、同样策略和全量审计的特权 API。直接 CR 只能存在于编译/部署隔离的开发/灾难诊断 namespace，生产 RBAC 显式拒绝。

### 6.6 M1 完成定义

- 同租户同 idempotency key 并发 20 次只产生 1 个 Run/Attempt/CR；不同 request digest 稳定 409。
- retry 创建新 Attempt/task/result namespace，旧 artifact 保持不变；取消在每个过渡阶段都有测试。
- API 无法引用任意 Kubernetes Secret，无法越 tenant/project 读 Run/event/artifact manifest。
- API/DB/Dispatcher 任意一个组件在创建过程中重启，恢复后不丢请求、不重复创建可执行 Attempt。
- M1 的 API 仍只在内部测试环境可用，外部租户开放被 M2 安全门阻断。

---

## 7. M2——Tool VM 安全、持久产物、凭据、网络和供应链

**目标**：消除向不可信租户开放前的阻断级问题。M2 不是一个单一 feature，它是 managed 安全和耐久性的最小基础。

### 7.1 Tool VM 内 supervisor 与不可信命令分权（最高优先级）

Tool Bridge 保留 guest root supervisor 身份以读取 Kata Secret volume 和管理 cgroup，但每个 shell 必须以专用非 root UID/GID 执行：

0. **先跑真机 cgroup feasibility gate**：证明 Tool 容器内是 unified cgroup v2，目标 controller 可用，supervisor 拥有可写的受控 subtree，`cgroup.kill`/`cgroup.events populated` 真实生效，sandbox UID 不可写父/兄弟 cgroup。该 gate 使用与生产相同的 Kata/guest kernel/seccomp/capability。失败时 M2 阻塞：必须选择修改 guest/Kata cgroup delegation，或把 supervisor 下沉到可信 guest 层；不能等 M5 再发现生命周期无法隔离。
1. 镜像合同固定 `sandbox` UID/GID，`/testbed` 只向该 UID 可写；Secret、bridge binary、audit spool 对该 UID 不可读/写。
2. Bridge 在启动时读取 key 并保持最小必要 fd，创建子进程时清理环境、fd、capability，设置 non-root credential、`no_new_privs`、受审核 seccomp 和独立 process group。
3. 每个 execution 在独立 cgroup v2 中运行，超时/取消使用 `cgroup.kill`，不只依赖 process group；bridge 是 subreaper，执行结束后证明后台子孙进程为 0。这里 cgroup 先用于隔离/清理，M5 才用于 ClawTune 调优。
4. audit 写入 `/var/lib/clawbox/audit` 等 sandbox UID 不可改的目录，使用 monotonic sequence + execution ID + previous-record hash；实时或结束时上传内容寻址 spool，不信任 `/testbed` 中的 trace 作为唯一审计证据。
5. 为恶意命令建立黑盒回归：尝试读 key/audit、kill bridge/PID 1、fork bomb、double-fork/setsid、占满 stdout/disk/memory/PID、写 `/proc`/`sysfs`，必须受限且 Cell 仍可清理。
6. supervisor 位于受保护 parent cgroup，预留 CPU/memory/PID 和 cleanup headroom；execution 子 cgroup 的限额总和不能耗尽 supervisor。Workspace、`/tmp`、execution output 与 audit spool 有独立磁盘配额/保留空间，保证磁盘/内存/PID 炸弹后 supervisor 仍能记录、终止和清理。

M2 实现 Tool Supervisor protocol core：固定 framing/version/capability、execution ID、deadline/cancel、identity/fencing 和幂等查询；M4 在不破坏 core 的情况下完成 correlation/signed observation/resource-intent schema。不得让旧 Runtime 静默连到不兼容 bridge。

### 7.2 持久 artifact 系统

- 将 ingester 从 SQLite + `emptyDir` 切到 PostgreSQL；用 Alembic migration、connection pool、readiness 中的 DB 探测和 schema revision gate。
- trace/log/patch/final answer 作为 content-addressed blob 写入 S3 兼容存储；DB 保存 Attempt-scoped manifest、digest、offset/sequence、size、media type、finalized_at。
- 上传协议升级为版本化 API；每个 chunk/blob 幂等，最终 manifest 枚举全部对象和 root digest。receipt 只在 result + final manifest + 所有 required blob 存在且 digest 一致时 complete。
- 修复当前“可变 trace 在同 offset 返回 409 后 uploader 跳过整文件，仍可写 final marker”的语义。推荐 sidecar 轮转/快照出 append-only spool 后上传，不直接跟踪会被 OpenClaw 重写的活动文件。
- artifact 状态是 `partial/final/expired/quarantined`；Agent 失败也应有 final manifest，但缺 required 对象不得返回 durable success。
- Cell Controller 在进入 `Succeeded` 前使用 Attempt identity 独立查询/验证中央 receipt，不只相信 Runtime Job exit 0。Runtime 握手是第一道门，controller-side verification 是最终状态门。
- 按 tenant/policy 实施保留、legal hold、删除和配额；删除是可审计的 tombstone + 后台 GC，不用用户提供的路径直接删文件。
- 使用静态加密、TLS、对象存储 bucket policy、tenant/project 授权和短寿命 signed download URL。
- authz/policy、credential issue/revoke、Run/Attempt 状态、execution grant/digest/resource limit、observation quality、artifact receipt、operator/break-glass 操作写入追加审计流；审计有单调 sequence/hash chain 或签名、WORM/object lock、时钟同步、脱敏和租户级访问/保留策略。

### 7.3 凭据与模型访问

- API 接受 `modelProfileRef`，Policy Service 确认它属于 tenant/project、允许该 template 使用、未超预算；绝不允许客户端提交 Kubernetes Secret 名。
- 建立 LLM egress broker/proxy：真实供应商 key 留在 broker，Attempt 只获取具有 model、tenant、token/cost limit、expiry 的短寿命凭据。Runtime 内的 OpenClaw/ClawTune 不再持有长寿命上游 key。
- ClawTune sidecar 只绑定 `127.0.0.1`，不再监听 `0.0.0.0:8765`；Runtime 内受信 supervisor/broker 与 OpenClaw 分权，OpenClaw 子进程环境不含真实 provider key、artifact upload token 或 signing key。
- upload token 至少包含 `tenant_id/run_id/attempt_id/aud/scope/exp/jti/key_id`，分离 artifact write 与 receipt read scope，支持 key rotation 和 Attempt 取消后撤销。
- 凭据不写日志、trace、CR status 或 API event；增加 entropy/known-secret 扫描和脱敏单元/端到端测试。
- 为控制面引入 workload identity（如 K8s projected SA token + 外部 identity/KMS），管理凭据不使用默认 `development-only-*`。生产启动时发现默认 secret 必须 fail closed。

### 7.4 网络与租户隔离

- 每个租户/项目使用受管 namespace 或有等价强隔离的标签/策略边界；在 namespace 创建时就安装永久 default-deny，不依赖每个 Cell 后创建 policy 来避免策略窗口。
- Tool 默认无 egress；Runtime 只能访问 DNS、Tool Service、Artifact Service 和 LLM broker。用户不能用 `0.0.0.0/0` 扩大边界。
- 对需要互联网的任务使用审计 egress proxy，Policy 用域名/供应商/端口表达，proxy 做 DNS pinning/rebinding 防护、请求记录、字节/请求限额和私网/metadata/K8s API 拒绝。
- 增加跨租户 Service DNS/IP、Node IP、API server、cloud metadata、ingester archive read API 的攻击测试。

### 7.5 镜像供应链

- 发布镜像全部用 immutable digest，禁止 `:dev`/`:latest` 出现在生产 manifest。
- 产生并验证 provenance/SBOM/signature，记录 source revision、builder identity、platform、ClawTune/OpenClaw/tool-bridge revision。
- admission policy 验证 `linux/arm64`、签名者、漏洞阈值、任务镜像合同（`/testbed`、sandbox UID、tool bridge version）；验证失败在创建 VM 前终止。
- registry 不再依赖单节点 loopback 作为最终方案；可用性、垃圾回收、备份和 digest 保留有独立 runbook。

### 7.6 M2 完成定义

- 恶意 Tool 命令无法读/改 key/audit，无法 kill root bridge，超时/取消后 cgroup 中无子孙进程。
- ingester/DB/object store 分别在上传中重启后，最终 manifest 或者完整、或者明确失败，不出现静默丢 trace；完成一次备份恢复。
- 不存在任务可选的原始 Secret 名或长寿命上游 key；取消后 Attempt token 失效。
- 两租户红队测试无凭据、文件、网络、artifact 串访；namespace 创建到删除期间 default-deny 无窗口。
- 生产部署不含 `:dev`/`:latest`、SQLite/`emptyDir` 产物库、development-only secret 或未签名镜像。

---

## 8. M3——可恢复控制面、持久预留与故障注入

**目标**：让单节点生产路径在组件重启、API 冲突和局部失败下仍能自动收敛，并为多节点/HA 提供正确的一致性基础。

### 8.1 Controller manager 改造

- 用 watch/informer + rate-limited workqueue 替代 2s 全量轮询；支持 bookmark/resourceVersion 过期后 relist，错误使用指数退避和有上限 dead-letter/condition。
- 引入 Kubernetes Lease leader election，先允许多副本待命但只有一个 active reconciler。不能先改 `replicas: 2` 再补一致性。
- 所有 status/metadata patch 处理 resourceVersion 冲突并重试；每个状态转移是幂等的，不依赖进程内记忆。
- 子资源除 owner UID 外记录 immutable spec hash/controller revision。既有子资源同 owner 但 spec 不同时 fail closed，不静默 adopt。
- 加入明确取消路径、每阶段 deadline 和 cleanup deadline；终态不应短暂进入 `Succeeded` 后立即只留 `Cleaned`，status/conditions 必须长期保留 outcome 和 artifact reference。

### 8.2 持久 Reservation 与节点容量

- 引入 `CellReservation` CR 或等价的持久带 fencing token 预留对象，绑定 Attempt UID、node、CPU/memory/storage/pods、expiry。
- admission 使用乐观并发/resourceVersion 或专用调度单写者，保证两个 controller 不会同时超售。
- NodeCapacity 是节点级、带采集时间和有效期的；devmapper Data/Meta 预算、RuntimeClass overhead、非 Cell Pod request、Pod 数和安全余量都纳入。过期容量证据必须停止新 admission。
- reservation 只在 Tool/Runtime 已消失且 node-side sandbox 已确认清理后释放，不在 CR 刚进终态时提前释放。

### 8.3 Orphan reaper 和节点健康

- 实现只读对账和受审计清理两步：对比 CR/Pod/Job、containerd sandbox、kata shim、Firecracker PID/CID、devmapper snapshot，生成 orphan report；只清除能证明不属于活跃 Attempt 的对象。
- 节点出现 orphan、thin-pool 阈值超限、Kata gate 失败或无法上传证据时，移除 `firecracker-ready`/设置 taint，停止新任务，不自动执行不可证明安全的广泛 `pkill`。
- 节点维护是 `cordon → 等待/取消 Cell → 对账 → 重启 → FC gate → uncordon`，需要自动脚本和运维手册。

### 8.4 故障注入矩阵

| 注入点 | 必须证明的行为 |
|---|---|
| controller 在每个 phase 后重启 | 无重复子资源、无丢预留，状态继续收敛 |
| leader 失联 | 新 leader 在 lease timeout 后接管，不发生双 admission |
| Tool 启动/运行中 crash | Runtime 不被错误启动或及时失败，产物标为 partial，最终清理 |
| Runtime 在 Agent/upload 阶段 crash | 准确区分 Agent 失败与 artifact 失败，不伪造 success |
| DNS/SSH 30～120s 中断 | 有界重试，记录指标，不无限卡死 |
| PostgreSQL/S3/ingester 不可用 | 停止 final success，恢复后幂等继传 |
| API server 409/429/5xx | workqueue 退避且不热循环，condition/metric 可见 |
| 用户在各 phase 取消 | 60s p99 停止执行、凭据撤销、终态 manifest 与清理 |
| containerd/kubelet 重启 | 不把孤儿 VM 当已清理，节点在对账前不接新任务 |

### 8.5 M3 完成定义

- 单节点上 controller 可以部署 2 个副本，只有 leader 执行；上述故障注入全部自动化并通过。
- 任何时刻预留总和不超过带安全余量的节点容量；controller 重启/切主后账本与活跃 VM 对齐。
- 100 个混合 success/failure/timeout/cancel Attempt 后 orphan 为 0，无提前释放导致的超售。
- 只有 M3 通过后才能开始多节点和 ClawTune enforce 执行。

---

## 9. M4——Tool Execution Protocol、可信 Observation 和 ClawTune KB 影子闭环

**目标**：将当前“OpenClaw hook trace + Tool Bridge 末尾 JSONL”升级为能精确 join、去重、校验、回放和分租户学习的数据闭环；本阶段预测只运行 shadow，不改变真实资源。

### 9.1 完成 `ToolExecutionProtocol v1` 的 correlation/observation 契约

当前 SSH `exec` 只发一段 shell 字符串，execution ID 由 Tool Bridge 内部随机生成，不能确定性关联 ClawTune span、资源预测和中央 observation。M2 已经建立协议运输/身份/生命周期 core；M4 补齐下列完整 envelope、correlation 和 observation 语义。协议可以继续用 SSH 作为运输，但必须使用版本化 subsystem/长度前缀 envelope，不将 JSON 拼成 shell 字符串。

请求至少包含：

```text
schema_version, tenant_id, project_id, run_id, attempt_id,
sandbox_uid, execution_id, traceparent, tool_name,
command_digest, argv_or_command, cwd, sanitized_env,
deadline, output_limit, resource_intent,
policy_version, prediction_id, kb_generation,
nonce, issued_at, expires_at, fencing_token, signature
```

响应/observation 至少包含：

```text
execution_id, started_at, completed_at, duration,
exit_code, signal, timeout, cancelled, oom,
stdout_bytes, stderr_bytes, truncated,
cpu, memory, io, pids, network/process summaries,
requested_limits, applied_limits,
collector_type/version, collection_quality,
observation_digest, supervisor_signature
```

- Tool Bridge 必须拒绝过期、nonce 重放、tenant/attempt/sandbox UID 不符、command digest 不符、fencing token 过时和超出 Cell 硬上限的请求。
- execution ID 在 Runtime/ClawTune 侧生成，贯穿 plugin span、Tool Bridge、observation、artifact 和日志。一个命令已经在 Tool 端开始后，运输失联不得盲目重放；必须先按 execution ID 查询或终止。
- 协议握手返回 bridge build revision、schema/capability 列表。Runtime/Tool 不兼容时在执行用户命令前 fail closed。
- 任务镜像 mapping/attestation 记录 Tool Bridge protocol 和 collector capability，避免新 Runtime 连到旧 bridge 静默降级。

### 9.2 取代脆弱 OpenClaw bundle 字符串补丁

当前 Runtime 镜像直接在 OpenClaw `dist/*.js` 中替换 `remoteWorkspaceDir`，并预建固定 hash 哨兵目录。这个补丁已在当前版本真机通过，但不应成为长期 managed 契约。

1. 优先向 OpenClaw 提供正式 SSH sandbox root/configuration 能力或实现 ClawBox tool backend/adapter。
2. 在上游能力可用前，将补丁改成固定 upstream revision 上的可审查 patch file，CI 同时编译/启动 OpenClaw 并运行 read/write/exec 契约；不仅检查 bundle 中是否存在某字符串。
3. 用 `clawtune.lock`/integration lock 锁定 ClawTune commit、OpenClaw version、plugin schema 和 protocol version；镜像不从未校验的相邻工作区构建生产发布。

### 9.3 `ToolExecutionObservation v1`

Observation 必须是不可变、可签名、可按租户删除/重建派生数据的正式 artifact，而不是从日志临时 grep。

- 唯一键为 `(tenant, attempt, execution, observation_type, schema_version)`；同 digest 重传幂等，不同 digest 冲突进 quarantine/audit，不训练。
- repo fingerprint、tool name、command feature/digest 必须定义脱敏与 tenant-scoped 规则；默认不上传 argv/env/文件内容原文。
- `complete=false`、collector degraded、身份/签名错误、schema 未知、execution join 失败的记录可保留作诊断，但不进 active KB。
- timeout/OOM/failure 是 censored observation，应保留失败界信息，不可当普通成功样本。第一版它们只进 diagnostic/censored store；只有明确支持 censoring 且经批准的后续 estimator 才可消费。
- Tool VM 可持有只能签 observation 的 Attempt 级密钥，不持有中央 artifact upload token；Runtime relay/uploader 不得改写 supervisor 签名 payload。

### 9.4 中央 Observation Projector 和 KB Store

```text
signed raw observation
  -> Artifact Service immutable store
  -> schema/identity/signature/quality validator
  -> trusted_observations (append-only)
  -> tenant KB builder
  -> immutable KB snapshot + generation + provenance
  -> Prediction API (shadow only)
```

- 从旧 `clawbox/scheduler/kb.py` 提取 `RuntimeToolResourceKB` adapter 的经验到新 `clawbox/tuning/` 或 `clawbox/kb/`，不让生产路径调用旧 `Scheduler.run()`。
- public baseline、tenant overlay、repo/tool layer 有明确的来源、授权和隔离。一个 tenant 的 private sample 不会数值混入另一 tenant。
- snapshot 包含 generation、schema/model/algorithm version、input observation range/digest、created_at、builder revision、quality summary；可回滚、可离线重放、可按 tenant 删除并重建。
- projector 是至少一次消费 + 数据库唯一约束，重复 observation 不增加 generation。异常值、数据投毒、收集版本 drift 有指标和 quarantine。

### 9.5 Shadow Prediction API

预测返回：`prediction_id`、`kb_generation`、`p50/p90` CPU/memory/duration/I/O，`confidence`、`sample_count`、`match_level`、`fallback_reason`、`expires_at`。第一阶段：

- `FixedProfileSizer` 仍决定真实 Cell 大小；shadow prediction 只写 Attempt status/event 和指标。
- 任务后将 prediction 与 actual observation 对比，统计 underprediction/OOM risk、over-allocation、匹配覆盖率、置信度校准和 drift。
- 建立离线 replay dataset，包含 repo/tool/workload class 分层；训练/评估数据分离，不用同一任务证明自己的预测有效。

### 9.6 M4 完成定义

- 真实 OpenClaw tool span 和 Tool Bridge execution 能用同一 execution ID 精确 join，不依赖时间模糊匹配。
- 真实 Tool execution observation 从 Tool VM 进入正确 tenant KB；签名、身份、schema、质量任一不合格均不训练。
- 重传 observation 不增 generation，冲突不污染 KB，删除 tenant 后 derived snapshot 可重建。
- 第二次相似任务能读到新 generation 和可解释 shadow prediction，但真实 resource request/limit 与固定基线完全一致。
- 低质量/未签名/跨租户 observation 攻击套件通过。

---

## 10. M5——Tool VM cgroup/eBPF 与 ClawTune 有界执行

**目标**：在 Tool VM 内获得可信资源观测，再通过可回退的分级模式让 KB 影响下一个 Cell 和下一条命令。eBPF 是增强观测，不是第一版 KB 的前置条件。

### 10.1 Tool VM 能力 gate

每个任务镜像/guest kernel 发布前自动检查：

- unified cgroup v2 以及 `cpu/memory/pids/io/cpuset` controller 可用性；
- Tool Supervisor 拥有可写的受控 subtree，agent UID 不可写控制文件；
- `memory.peak/events`、`cpu.stat`、`io.stat`、`pids.current/events` 的内核实际支持；
- `/sys/kernel/btf/vmlinux`、`CONFIG_BPF`、`CONFIG_BPF_SYSCALL`、`CONFIG_CGROUP_BPF`、`CONFIG_DEBUG_INFO_BTF`，所需 `bpf()`/perf/ringbuf 能力和 seccomp 放行；
- collector 可以只按 execution cgroup ID 过滤，Cell 清理后 program/map/link 数回到基线。

gate 不通过时，eBPF 标记 unavailable/degraded，但 cgroup + `/proc` + `rusage` 是正式 fallback。不能为了打开 eBPF 给 agent UID `CAP_SYS_ADMIN`/`CAP_BPF`。

### 10.2 观测分层

1. **Rusage 基线**：保留已有 user/system CPU、MaxRSS、duration、exit/timeout、stdout/stderr bytes。
2. **cgroup v2 精确层**：采集 `cpu.stat`、`memory.current/peak/events`、`pids.current/events`、`io.stat`，正确区分 OOM、throttle、timeout、cancel。
3. **Tool-local eBPF 层**：只由 supervisor 加载签名、版本固定的 CO-RE 程序，默认只采集 exec/exit、进程树、I/O 与网络元数据，不采 argv/env/文件内容。map/ring buffer 有界，暴露 lost-event counter，在 guest 内聚合后上传。

eBPF 应与 cgroup `/proc` 数据交叉验证；任何 lost event 或 collector crash 都会降低 collection quality，不能仍标 `valid`。

### 10.3 先修正单容器 Runtime 资源账本

当前 `FixedProfileSizer` 单独计算 sidecar 预算，但 manifest 只把 `size.runtime` 写入 Runtime 容器 request/limit；ClawTune 实际与 OpenClaw 共用这个 limit。M0/M5 必须选择并锁定一种一致模型：

- 建议把 `runtime + clawtune overhead` 合并成 Runtime 容器的真实 request/limit，同时 reservation 不再另计一份 phantom sidecar；
- 或者在 Kata 上游共享 volume 问题解决后恢复独立容器和独立资源，但这是另一次需要重跑全部真机 gate 的架构迁移。

不得让 admission 账本、Kubernetes request 和容器 limit 三者继续不一致。

### 10.4 执行模式

```text
observe         只采集，使用固定 profile/限制
shadow          生成预测但不应用
advise          返回建议并记录若应用的结果，仍不改真实资源
canary-enforce  在 policy clamp 内应用到受控 5%/25%/50% 流量
active-enforce  canary 门禁全过后的已批准生产模式
```

模式由服务端 Policy 选择，不由 prompt/Agent 改写。全局、tenant、template 和 run 级 kill switch 均可立即回退到固定 profile，已创建 Firecracker VM 不假设可在运行中热扩缩。

### 10.5 Cell 级 coarse sizing

- 将 `CellSizer.size(profile)` 演进为接受 `SizingRequest`：tenant/project、repo/workload fingerprint、template bounds、deadline、KB generation、prediction。
- 实现新的 `ClawTunePredictionSizer`，预测只能映射到管理员批准的 resource class/min/max 区间，叠加 VM overhead、pipeline headroom 和安全余量。
- 无预测、低置信度、KB/API 超时、generation/schema 不兼容或预测越界时 fail safe 到固定 profile，不拒绝本可运行的任务，也不自动放大资源。
- status/event 保存 prediction ID/generation、requested/applied size、clamp/fallback reason，能够从产物重放每个决策。

### 10.6 Command 级 fine control

M2 已经要求每条命令必须位于独立 cgroup 以保证生命周期；M5 在此基础上根据经策略裁剪的 resource intent 设置：

- `cpu.max`/`cpu.weight`；
- `memory.high`/`memory.max`；
- `pids.max`；
- 硬件/内核支持且产品需要时的 `io.max`；
- Tool guest 内的 cpuset（不等于宿主 NUMA）。

Tool Supervisor 会对 intent 再做 Cell parent limit、tenant policy、最小生存边界和 fencing 验证，返回真实 applied limits。ClawTune 不能越过 Supervisor 直接写 cgroup。`TOOL_MAX_CONCURRENCY` 从固定 semaphore 演进为在 parent 硬上限内的资源感知 admission。

### 10.7 Affinity/NUMA 必须分层

- Tool guest cpuset 只能选 guest vCPU，不证明 Firecracker vCPU thread 在宿主 NUMA 上的位置。
- 宿主层优先使用 Kubernetes CPU Manager/Topology Manager、Guaranteed QoS 与经审计的 Kata/Firecracker placement。
- 若必须控制 VMM thread affinity，需要独立 node actuator，以 sandbox UID/fencing 授权，最小权限、可审计、可回退；guest 绝不直接控宿主进程。
- 在 CPU Manager/Topology Manager、Firecracker vCPU pinning、`numastat` 和并发成功率证据齐备前，NUMA 保持关闭。

### 10.8 Canary 门槛

- 扩展次序是 5% → 25% → 50% → 100%，每阶段有最小样本数和完整周期，不以一个成功任务扩全量。
- 与同 repo/tool/workload 的固定 profile 对照，关注 OOM、timeout、Agent/platform success、队列时间、成本、CPU/memory waste 和 eBPF overhead。
- 任一隔离问题、observation 伪造、KB 串租户、prediction 越界、OOM/platform failure 超过预定 budget 立即回退。

### 10.9 M5 完成定义

- CPU/memory/PID/支持的 I/O 限制可确定性触发，OOM/throttle/timeout/cancel 分类正确，结束后 10s 内 cgroup `populated=0`。
- agent 无法移出 cgroup、写 controller file、加载/卸载 eBPF 或清空审计；collector 只关联目标 execution。
- 在规定峰值下 eBPF lost events = 0，CPU p95 overhead 不高于经批准预算（初始建议 3%），内存/map 有界；不支持 eBPF 的镜像使用 cgroup/proc fallback 仍可运行。
- 形成 `observation → generation+1 → next prediction → applied Cell/command decision → new observation` 完整证据链，且 KB/ClawTune 故障时自动回退固定安全 profile。
- enforce canary 的成功率、OOM、延迟和容量利用达到预先批准门槛，每个决策可审计和回放。

---

## 11. M6——多租户公平性、多节点、HA 与容量验收

**目标**：将经 M3 证明可恢复的单节点路径扩展为多节点 managed worker fleet，同时保证全局不超售、租户公平和节点故障语义明确。

### 11.1 租户配额与公平队列

每 tenant/project 至少控制：

- concurrent Run/Sandbox/Tool execution；
- reserved CPU/memory/ephemeral + devmapper storage/VM/Pod；
- queue length、wall-clock、command count/output bytes；
- LLM token/cost/rate；
- artifact bytes/retention；
- 可用 template/resource class/model/network policy。

API 在接收时做请求级限额，队列做 fair scheduling/priority/aging，Cell admission 做硬资源上限，Tool Supervisor 和 LLM broker 做实际用量限制。各层语义要一致，不能 API 显示有配额但底层无限执行。保留全局 emergency reserve 与 N+1 drain 容量，任一 tenant 不能占尽 worker fleet。

### 11.2 Per-node capacity 和 placement

- 删除 `build_reconciler()` 中“恰好一个 ready ARM64 node”假设，但只在每个新 worker 都独立通过 FC-0～FC-5 后扩展。
- `NodeCapacityProvider` 返回 per-node 向量和健康时间，不先聚合所有节点资源再把一个完整 Cell 固定到一个节点。
- 每节点上报 CPU/memory/pod slots、devmapper Data/Meta/snapshot、KVM/Kata/Firecracker/CNI/DNS 健康、镜像 locality、节点污点/维护状态。
- Tool 和 Runtime 默认成对落在同一合格 worker 以降低跨节点网络变量，除非完成单独安全/性能评审。
- placement 同时考虑资源、devmapper high watermark、image locality、failure domain、anti-affinity、tenant spread 和经证明的 topology；没有合格整 Cell 时保持 Queued。

### 11.3 Worker 与控制面分离

- managed GA 不接受 Kubernetes control-plane/etcd 与不可信 sandbox 共置在同一节点。
- worker 使用专用 node pool/taint/toleration，最小 host package、无通用用户登录，只暴露受控管理面。
- 一个 Attempt 一组新微 VM；在证明跨租户内存/磁盘/网络/凭据抹除前不引入 warm VM reuse。

### 11.4 并发与长稳矩阵

- 基础阶梯仍是 1 → 2 → 4 → 8 → 16 → 32，每阶段至少 3 轮 cold-cache synthetic 和 3 轮 warm-cache synthetic，再跑代表性真实 Agent 子集。
- 基础设施压测使用 stub/local LLM 与确定性 workload，模型容量另测；不用供应商限流掩盖 sandbox 容量。
- workload mix：noop/短命令、CPU、memory/OOM、I/O/磁盘填充、大输出、网络拒绝、长 pytest/build、真实 SWE-ReBench、cancel/timeout。
- 最高合格并发是满足全部 SLO 并保留 N+1 余量的最高阶，不是 CPU 除法的理论值。
- 最高合格并发后跑 24h churn、72h soak、至少 1000 次 create/cancel/timeout/cleanup，且 10% Attempt 在不同 phase 注入故障。

任一隔离失败、artifact gap/错误 complete、orphan、devmapper Data/Meta 达到硬门（当前 80%）、节点/CNI 失稳或平台失败率超预算立即停止扩量。

### 11.5 M6 完成定义

- 至少 2 个 worker 同时运行，每个节点的 reservation 不超容量，不会因聚合资源导致单节点放不下 Cell。
- leader 切换、worker NotReady/reboot/drain、containerd/kubelet 重启后无双 Cell、无丢 reservation、无孤儿 VM/snapshot，Run 得到确定 outcome/retry 语义。
- 两个以上 tenant 的 quota/fairness/priority/noisy-neighbor 测试通过，任一 tenant 不能使其他 tenant 或平台管理负载饿饿。
- 32 并发连续 3 轮无平台导致失败，所有 required receipt 完整，清理后快照/shim/Firecracker/预留回到基线；高于 32 只在容量证据允许时逐级扩展。

---

## 12. M7——Managed Service GA

**目标**：将已验证的执行、数据、调优和 fleet 能力交付为可运营、可升级、可恢复、对用户稳定的服务。

### 12.1 SDK/CLI 与用户体验

- 基于版本化 OpenAPI 生成至少 Python SDK 和 CLI，支持 create/get/list/cancel/retry/events/logs/artifacts，服务端与 SDK 有兼容性测试。
- event/log 流支持 cursor 断点续传与背压，不让用户直接 `kubectl logs`。
- 错误模型区分 auth/policy/quota/admission/platform/agent/LLM/artifact/evaluation，提供稳定 machine-readable code 和 correlation ID。
- 最初只交付 batch Run。交互式 attach/session 若纳入后续，必须设计 session lease、idle/max TTL、断线、取消、用户 I/O 审计和 workspace 污染边界。

### 12.2 可观测性

统一结构化日志字段：

```text
timestamp, level, component, cluster, tenant/project,
run_id, attempt_id, execution_id, sandbox_uid,
phase, reason, node, pod_uid, trace_id,
git_revision, image_digest, clawtune_revision
```

prompt、命令原文、输出、API key 不进普通控制面日志。Prometheus label 不使用 run/task/command/repo 等高基数/敏感值，这些只在结构化日志/trace 中作 correlation。

必须有指标/面板/告警：

- API request/latency/error/idempotency/authz/quota；
- queue age/depth、admission/reconcile/phase/cleanup latency、stuck finalizer；
- per-node allocatable/reserved、devmapper Data/Meta/snapshot、Firecracker/shim/orphan、CNI/DNS；
- VM boot、SSH handshake/255/retry、Tool exec duration/timeout/OOM/truncation/RSS；
- LLM status/latency/token/cost（无正文）；
- artifact bytes/retry/conflict/gap/finalize/receipt incomplete/backlog；
- observation accepted/rejected/quality、eBPF lost event/overhead、KB generation、prediction error/fallback/clamp/enforcement result。

liveness 只证明进程活着；readiness 检查是否能承载新请求，例如 DB/object store/K8s API/leader/cache schema 状态。当前无条件 `/healthz=ok` 不够。

### 12.3 初始内部 SLO（外部 SLA 前至少收集 30 天基线）

| SLI | 初始目标 |
|---|---:|
| Managed API availability | ≥99.9%/月 |
| 有容量时 Accepted→ToolReady | p95 ≤60s，p99 ≤120s |
| Runtime complete→artifact receipt | p99 ≤120s |
| cancel→无活动 VM | p99 ≤120s（稳定后收紧到 60s） |
| 平台导致的 Attempt 失败率 | <1% |
| required artifact 完整率 | 100%，不允许静默缺失 |
| receipt 后 artifact 丢失 | 0 |
| 运行结束 15min 后 orphan | 0 |
| 租户隔离事件 | 0 |

Agent 是否修对 bug、外部 LLM 拒绝/限流、用户预算耗尽不计入 platform availability，但要作为独立产品质量指标。错误预算消耗 50% 时冻结高风险发布，耗尽时停止自动扩量并进入可靠性修复。

### 12.4 备份、灾备和恢复演练

- 生产控制面至少 3 副本且跨故障域，PostgreSQL 与对象存储具有高可用/冗余，至少两个 sandbox worker failure domain 并保留 N+1 drain 容量。控制面不与不可信 sandbox workload 共置。
- PostgreSQL WAL 持续归档 + 定期全备 + PITR；对象存储 versioning、跨故障域复制、object lock；etcd 快照；registry digest/signature/provenance 可在灾备站重新拉取。
- KB derived snapshot 必须能从 immutable observations 重建；密钥/KMS 有轮换、备份、撤销和恢复程序。
- 运行中微 VM 默认不承诺内存级灾备；worker 永久丢失后 Attempt 标为 `Interrupted/PlatformFailed`，按策略创建新 Attempt，不伪装原 VM 可续跑。
- “备份 job 成功”不是验收；必须在隔离环境恢复，通过 manifest/hash 和应用级 Run/artifact/KB 查询校验。上线前完成全量恢复，初期每月、稳定后至少每季演练。

初始 RPO/RTO 目标必须在 ADR 中批准；建议基线：

| 数据/能力 | RPO | RTO |
|---|---:|---:|
| receipt 已确认 artifact | 0 | ≤4h |
| Run/Attempt/控制元数据 | ≤5min | ≤1h |
| 原始 KB observation | ≤5min | ≤4h |
| KB derived snapshot | 可从 raw observation 重建 | ≤4h |
| 关键审计 | ≤1min，安全关键操作同步持久 | ≤4h |
| 运行中 sandbox | 不做内存恢复，以新 Attempt 重试 | 按 Run policy |

### 12.5 发布与升级

- CI 至少分为 PR unit/contract，PostgreSQL/MinIO component，K8s integration，ARM64/Firecracker nightly/release gate，镜像 release/sign，staging promotion。
- Python/Go/NPM/base image/ClawTune/OpenClaw 依赖都锁定 digest/hash/revision，构建生成 SBOM、漏洞报告、provenance 和签名。
- DB 使用 expand/contract migration，至少保持一个发布窗口的前后版本兼容；CRD 有 served/storage/conversion 升级和回滚测试。
- staging 顺序：expand migration → control plane → single synthetic → 真实单任务 → 隔离攻击套件 → 小并发 → 人工/strategy promotion。
- host bootstrap/磁盘初始化与应用发布完全分离；任何 CI 不自动清盘、宽泛 kill 或 `kubeadm reset`。

### 12.6 M7/GA 完成定义

- M0～M6 所有门禁有机器可读、指向精确 revision/digest 的证据包，无 Critical/High 隔离、凭据、artifact durability 问题。
- 72h soak、故障矩阵、多租户攻击测试、最近一次备份恢复和升级/回滚演练均在有效期内通过。
- 不使用单节点控制面、SQLite `emptyDir`、全局 bearer token、可变镜像 tag、任意 CIDR、租户可选 Kubernetes Secret 或未审计 root shell。
- SDK/CLI/API、SLO/dashboard/alert/on-call/runbook、备份/灾备、计量/配额、审计/删除和 release evidence 完整。
- eBPF/cgroup/KB 任一降级都 fail safe，不扩大权限/资源或污染 KB；NUMA、affinity、warm pool 没有证据时保持关闭，不作为“已完成”宣传。

---

## 13. 旧控制面收敛计划：最终只保留一条生产写路径

### 13.1 需要处理的旧模块

```text
clawbox/scheduler
clawbox/allocator
clawbox/controller
clawbox/node_agent
clawbox/tool_agent
clawbox/common/models.py 中 legacy lease/grant/observation
docker-compose.yml 中 legacy services
```

这些代码有价值的部分包括 tenant KB overlay、prediction/observation 契约、lease/fencing、NUMA capacity 建模和幂等 API 经验；但其 `Scheduler.run → allocator → old controller/tool-agent` 不是现代 SandboxTask 执行路径。

### 13.2 迁移步骤

1. 统计旧 API 的真实调用者和流量，冻结新功能，返回 deprecation header 并公布下线版本。
2. 将 KB adapter/algorithm 提取到 M4 的 `clawbox/tuning`/独立 package，用新 Observation/Prediction schema 封装，不带入旧 run/lease/tool lifecycle。
3. 将 lease/fencing 的有用不变量重新实现在 M3 `CellReservation`，不共享两份活动预留数据库。
4. 无外部消费者时，导出历史 execution/observation 为只读归档后删除旧执行面。有消费者时，建立 compatibility adapter，只将请求转成新 AgentRun；adapter 无 Pod/Lease/Secret 写权限。
5. 历史数据导入时生成 legacy run/attempt ID 和 provenance，不迁移任何活动 lease/tool instance，不将未签名旧 observation 默认放入 trusted KB。
6. 两个发布周期零流量后删除旧 Deployment、镜像、API route、表和运维脚本。

### 13.3 收敛完成定义

- 只有 Managed API/Dispatcher 能创建 execution CR，只有 Cell Controller 能创建 Tool/Runtime 工作负载。
- 仓库中不存在两套活跃 reservation、两套 Run 状态机或两个 tool lifecycle owner。
- legacy adapter 无管理 K8s 或凭据权限，其流量/错误/最后调用者可观测，下线时间明确。

---

## 14. 测试金字塔、CI 和发布门

### 14.1 建议目录

```text
tests/unit/          纯逻辑、状态机、策略、token、KB
tests/contracts/     OpenAPI、CRD/CEL、RBAC、manifest、protocol/schema
tests/component/     PostgreSQL、object store、ingester、projector、broker
tests/integration/   API->outbox->CR->controller->mock/runtime->artifact
tests/e2e/           目标 ARM64/Firecracker 真实 Agent
tests/security/      跨租户、凭据、网络、恶意命令/镜像
tests/chaos/         组件/节点/网络/存储故障矩阵
tests/fixtures/      已脱敏且版本化的契约样本
```

### 14.2 PR 必跑

- Python unit/type/lint，Tool Bridge Go test/race/fuzz/vet/gofmt，shell syntax/shellcheck，行尾与 `git diff --check`。
- API model、idempotency、authz、cancel/retry、状态转移、capacity/reservation、token、artifact manifest、observation/KB 单测。
- CRD CEL + server-side dry-run、OpenAPI backward compatibility、golden manifest、NetworkPolicy/RBAC/Secret projection/image policy contract。
- Tool Protocol 过期/重放/篡改/版本不兼容、supervisor 降权与 cgroup cleanup 组件测试。
- Alembic 空库升级、上一发布升级、expand/contract 兼容与回滚窗口。
- uploader 重复/乱序/中断/可变文件/空洞/checksum/manifest 终结测试。

当前已跟踪的 `test-clawbox.db` 不应继续作为测试状态容器。M0 需要将数据库测试改到 pytest 临时目录，并将本地 DB/缓存/证据目录纳入 `.gitignore`；不要在用户没有明确许可时删除任何可能是真实产物的数据库。

### 14.3 Nightly/Component

- PostgreSQL + S3/MinIO 真实组件，API idempotency 并发，outbox/Projector 重启，uploader 大文件与背压。
- kind/K8s integration 可验证 controller/RBAC/CRD/NetworkPolicy 渲染和恢复，但 **kind 不是 Firecracker 隔离验收**。
- dependency/image/secret 扫描、OpenAPI/CRD/DB migration 兼容、离线 KB replay/prediction 评估。
- 控制器双 reconcile、resourceVersion 冲突、leader 切换、reservation race、DB/object store/ingester 延迟与重启。

### 14.4 ARM64/Firecracker Release Gate

```text
host/status + FC-0..FC-5
image signature/provenance/contract
single synthetic
single real AgentRun
cross-tenant/isolation attack suite
1/2/4/8/16/32 scale
fault matrix
cleanup/orphan reconciliation
backup restore (within required cadence)
upgrade/rollback (within required cadence)
```

发布候选镜像必须在门禁前固定 digest，门禁后只提升同 digest，不重建一个“内容应该相同”的镜像。

### 14.5 平台 outcome 与 Agent 质量分开

status/result/report 至少分开：

- `platformOutcome`：隔离、启动、Tool transport、存储、清理是否正常；
- `agentOutcome`：Agent 退出、超时、无 patch、拒绝等；
- `evaluationOutcome`：benchmark 测试/评分是否通过；
- `artifactOutcome`：required/optional telemetry 完整性。

不允许用 Agent 没修对来掩盖平台 SSH/upload 失败，也不允许用平台成功声称 benchmark 任务正确。

---

## 15. 安全攻击和故障注入最小全集

### 15.1 安全矩阵

- 读取其他 Cell workspace/Service/Secret/artifact/KB；
- 读 Runtime/Tool 中不应可见的 LLM/upload/SSH/observation signing 凭据；
- 错误 SSH key、伪 host key、过期/重放/跨 tenant/task/scope token/grant；
- Kubernetes API、node/control-plane/etcd、cloud metadata/link-local、RFC1918/Service/Pod CIDR、DNS exfiltration；
- fork bomb、double-fork/setsid、OOM、PID/IO/磁盘/输出炸弹、kill/ptrace bridge；
- 修改/伪造 tool trace/observation、重放 execution ID、篡改 cgroup/eBPF map；
- 恶意/未签名/非 ARM64/协议不兼容镜像，artifact path traversal/chunk collision/超大 payload；
- prompt injection 诱导读凭据/未授权 endpoint，宿主 `/proc`/device/`/dev/kvm`/mount 探测。

### 15.2 故障矩阵

- 每个 phase kill controller leader；
- ingester 503/timeout/restart，PostgreSQL failover，object store 不可用；
- Tool/Runtime Pod 删除/crash，runtime→tool 丢包/SSH 255，DNS/CNI/egress gateway 故障；
- LLM 429/5xx/timeout/stream truncation；
- 上传重复/乱序/文件重写、cancel 与 completion 竞态；
- worker NotReady/reboot/drain，kubelet/containerd 重启，devmapper 高水位，registry 不可用；
- token/certificate/key rotation 和时钟偏移；
- KB/Prediction API 超时、错误 generation、异常预测、collector/eBPF 降级。

每个用例必须断言：状态不倒退、不重复不可幂等执行、不错误出 receipt、不泄露凭据、预留不超售、最终可清理。

---

## 16. 发布证据与工作项完成规则

### 16.1 证据包建议布局

```text
release-evidence/<release>/<cluster>/<run-id>/
  manifest.json
  gate-summary.json
  host-audit/
  image-attestations/
  api-crd-db-contracts/
  isolation-tests/
  e2e/
  load/
  chaos/
  restore/
  slo-report/
```

`manifest.json` 至少包含 release/Git SHA、镜像 digest、ClawTune/OpenClaw/dataset/recipe revision、host/kernel/K8s/containerd/Kata/Firecracker 版本、CRD/API/migration/protocol/observation/KB schema version、RuntimeClass/config hash、节点与执行时间。

`gate-summary.json` 记录每个 gate 的 pass/fail/blocked、命令、开始/结束时间、产物路径与 SHA256。隔离、凭据边界、receipt durability 和备份恢复不允许人工 waiver。

证据存在受管对象存储/WORM 或 CI artifact，不建议把大量日志/blob 直接提交 Git。收集前脱敏，凭据值永不进证据包。

### 16.2 任何 issue/PR 的 Definition of Done

- 有明确威胁/故障模型、输入/输出契约、不变量和不在范围内的内容；
- 包含单元 + 契约 + 适当层级的集成/真机测试，负向路径和回滚经过验证；
- 指标、结构化日志、告警/运维影响和数据保留/隐私已评审；
- API/CRD/DB/protocol 兼容与升级/回滚路径已评审；
- 文档/runbook/evidence manifest 更新，所有自动化检查全绿，工作区无无关产物；
- 对应里程碑 gate 有精确证据，不用“理论上可以”标记完成。

---

## 17. 下一个工作会话必须从这里开始

不要先做 eBPF、NUMA 或新 UI。下一个实施会话按下列依赖顺序执行；未通过的门只能阻断依赖它的后续工作，不阻断与之无依赖的 ADR/纯契约工作。硬主线始终是 `M0 → ADR → M1 → M2 → M3`。

### 17.1 第一批：恢复可信 `main`

1. **CBX-M0-001：修复 3 个过时 manifest 测试**
   - 文件：`tests/test_kubernetes_backend.py`、`clawbox/cell/manifests.py`。
   验收：不删安全断言；新断言覆盖单容器、Secret projection、RuntimeClass、无 hostPath/PVC/SA token、in-process ClawTune 握手；`pytest` 全绿。
2. **CBX-M0-002：修正 Runtime + in-process ClawTune 资源账本**
   - 文件：`clawbox/cell/capacity.py`、`clawbox/cell/manifests.py`。
   验收：admission reservation、Runtime request/limit 和设计 profile 数学一致，新测试覆盖每个 profile 与 RuntimeClass overhead。
3. **CBX-M0-003：消除双容器文档漂移**
   - 文件：`README.md`、`docs/CONCURRENT_KATA_SWE.md`、`docs/IMPLEMENTATION_MAPPING.md`。
   验收：它们均描述当前单容器事实，旧交接有过时指引，不改写历史证据。
4. **CBX-M0-004：隔离本地测试 DB/临时产物**
   - 文件：`tests/conftest.py`、`.gitignore`。
   验收：测试使用每次新临时 DB，不污染仓库；任何现有 DB 是否删除先明确数据属性。
5. **CBX-M0-005：增加 release/evidence manifest generator**
   验收：能在不读凭据原文的情况下生成 Git/镜像/配置/host/CR/artifact digest 清单，同一 evidence 可重新校验。

### 17.2 第二批：真机基线与安全设计

6. **CBX-M0-006：唯一 ID 单任务连续重放**，至少 3 次，保存每次 receipt/清理证据。
7. **CBX-M0-007：SSH 255 分层探针**，至少记录 DNS、TCP、SSH handshake、request、command exit 五层错误；运行 1000 次短 exec，得到频率和根因分布。
8. **CBX-M0-008：1/2/4 阶梯**，前后比较 Pod/Job/Secret/Policy/sandbox/shim/Firecracker/snapshot/devmapper；通过后才开 8/16/32。
9. **ADR-001～010**：在 M1 代码前完成下节决策，尤其是 Tool guest 分权、Run/Attempt ID、artifact 完整性和唯一生产路径。

### 17.3 第三批：先完成 M1，再实现 M2 安全阻断修复

ADR 批准后先完成 M1 的 Run/Attempt identity、CRD/API、tenant/model/network/artifact policy reference 契约。M2 的凭据、artifact token 和策略实现依赖这些身份，不得先于 M1 各自发明临时 ID。只有 Tool VM cgroup delegation/`cgroup.kill` 真机可行性 spike 和威胁实验可与 M1 并行，但不得以 spike 代替 M2 生产实现。

10. **CBX-M2-001：Tool Supervisor 与 non-root execution**，先完成恶意命令测试和 cgroup.kill 生命周期，再做性能调优。
11. **CBX-M2-002：凭据/模型 broker 设计**，立即禁止公共 API 接受 `llmSecretName`；ClawTune sidecar 只绑 `127.0.0.1`，OpenClaw 不持有真实上游长 key/upload token。
12. **CBX-M2-003：PostgreSQL + object store + strong manifest/receipt**，先修复假 complete，再做高并发 uploader 优化。
13. **CBX-M2-004：永久 namespace default-deny + server-side policy refs**，禁止用户原始 CIDR/Secret/Pod spec。
14. **CBX-M2-005：镜像 attestation/admission**，生产 manifest 全部锁 digest，拒绝 tag/非 ARM64/未签名/协议不兼容镜像。

完成 M1 和这批 M2 工作后，再按 M3 → M4 → M5 → M6 → M7 的门禁推进。

---

## 18. 必须在实现前完成的 ADR

| ADR | 决策主题 | 必须回答的问题 |
|---|---|---|
| 001 | Canonical production path | API/CR/Controller/DB 各自拥有哪些状态，legacy 如何下线 |
| 002 | Run/Attempt/Execution identity | ID 格式、幂等 key、retry、artifact namespace、重放语义 |
| 003 | CRD versioning/cancellation/status | storage version、conversion、desiredState、Conditions、清理与业务终态 |
| 004 | Artifact immutability | blob/manifest/chunk、required/optional、receipt、保留/删除/恢复 |
| 005 | Image trust | builder、registry、signature/SBOM/provenance、Tool Bridge attestation、admission |
| 006 | Sandbox trust boundaries | Tool supervisor/agent UID、Runtime credential broker、Secret/network/RBAC、guest root |
| 007 | Tool execution protocol | 运输、grant/fencing、版本协商、幂等/取消、OpenClaw adapter |
| 008 | Observation/KB/tuning modes | schema、签名、质量、tenant overlay、generation、shadow/canary/rollback |
| 009 | Reservation and multi-node consistency | per-node capacity、CAS/lease、leader election、释放、fencing、orphan |
| 010 | Persistence, tenancy and disaster recovery | PostgreSQL/S3/KMS、RPO/RTO、backup/restore、audit/WORM、数据删除 |

每份 ADR 都必须包含背景、决策、备选方案、安全/运维后果、迁移和回滚。不允许用代码已写完倒逼 ADR 审批架构事实。

---

## 19. 风险登记表

| 风险 | 触发/影响 | 预防与回退 |
|---|---|---|
| Kata Secret/ConfigMap mode `0000` | 容器被迫 guest root | root supervisor 只做 staging/管理，Agent/OpenClaw 降权；持续跟踪上游并保留真机 gate |
| Kata 无跨容器共享卷 | native sidecar 失败 | 当前单容器/in-process；任何恢复双容器需重跑全部 gate |
| Runtime/Tool 双内核 | Runtime eBPF/cgroup 看不到 Tool 进程 | collector/actuator 固定 Tool VM，Runtime 只决策/中继 |
| root Agent 与 bridge 同权 | 审计/限制可绕过 | M2 外部开放阻断；supervisor root + command non-root + protected audit + cgroup.kill |
| OpenClaw bundle 字符串 patch | 升级静默失效/清空 `/testbed` | 正式 backend/upstream capability；固定补丁和端到端契约 CI；锁定版本 |
| 任务名作 result 主键 | retry/tenant 冲突 409 | 服务端 run/attempt UID，名字只展示 |
| SQLite/emptyDir | receipt 后仍丢数据 | PostgreSQL + S3 + strong manifest + 备份恢复 |
| trace 重写冲突被跳过 | 假 complete | immutable segment/snapshot spool，manifest 无 gap 验证 |
| 租户直接 Secret/CIDR | confused deputy/内网探测 | model/network policy refs、broker/proxy、API 无 CR/Secret 写权 |
| 多 namespace 独立预留 | 全局超售 | cluster/node-scoped Reservation + leader/CAS，先一致性后多租户 |
| KB 投毒/串 tenant | 错误 sizing、数据泄漏 | 签名 observation、quality/quarantine、tenant overlay、provenance/rollback |
| 预测 under-allocation | OOM/成功率下降 | shadow、headroom/clamp、confidence threshold、canary、fixed fallback/kill switch |
| eBPF BTF/capability/seccomp 不支持 | collector 无法启动 | feature gate，cgroup/proc/rusage 为正式 fallback，不给 agent 提权 |
| guest cpuset 被当作 host NUMA | 调优无效或回归 | guest command control 与 host topology 分层，无 VMM thread 证据时 NUMA 关闭 |
| SSH 255/网络抖动 | pipeline 偶发失败/重复执行 | 分层探针、execution ID 查询、只对未开始命令有界重试、指标/SLO |
| VM/shim/snapshot 泄漏 | worker 资源耗尽 | owner/fencing 精确 reaper、对账后再 ready、chaos/soak、无广泛 pkill |

---

## 20. 最终完成检查表

只有下列全部为 `[x]` 时，才能将项目对外定义为 managed agent sandbox system：

- [ ] 用户通过认证、版本稳定的 API/SDK/CLI 管理 Run，而不是直接操作 Kubernetes。
- [ ] Run/Attempt/Execution 有全局唯一 ID、幂等、cancel/retry/deadline/event/audit 语义。
- [ ] 租户/project 在 API、队列、quota、namespace/network、Secret、artifact、KB 上严格隔离。
- [ ] Tool/Runtime 继续是两个独立 Firecracker VM，不 fallback，镜像/bridge 有签名和 provenance。
- [ ] Tool Supervisor 可信，任务命令 non-root/no capabilities/no-new-privs，无法篡改审计或逃过清理。
- [ ] 每条 execution 的 cgroup v2 观测/限制真实生效，eBPF 在 Tool VM 通过 gate 或明确使用受支持 fallback。
- [ ] signed observation 可靠进入正确 tenant KB，generation/prediction 可追溯、可回滚、可删除/重建。
- [ ] 相似任务存在“observation → KB → prediction → bounded execution → observation”证据链，失效时回退固定安全策略。
- [ ] PostgreSQL/object store 耐久，required manifest/receipt 强完整，备份恢复经演练。
- [ ] Controller watch/workqueue/leader/fencing/reservation 可恢复，支持多节点、公平队列和不超售。
- [ ] 1/2/4/8/16/32、72h soak、故障注入、多租户攻击、升级/回滚和备份恢复门禁通过。
- [ ] 终态后无 Pod/Secret/NetworkPolicy/sandbox/snapshot/Kata shim/Firecracker/reservation 泄漏。
- [ ] SLO、dashboard、alert、on-call/runbook、release evidence、retention/deletion、审计和计量完整。
- [ ] legacy scheduler/allocator/controller 不再构成第二条生产写路径。

---

## 21. 交接给下一位实施者的最后提醒

1. 先读本文、`AGENT_HANDOFF_2026-08-17-real-task.md` 和当前 manifest，不从过时的 native sidecar 图开始修代码。
2. 从 `CBX-M0-001` 开始，每次只推进一个可验收契约，不直接打开 ClawTune cgroup/eBPF/NUMA 开关。
3. 任何需要目标机 root/磁盘操作、宽泛清理、凭据暴露或外部服务变更的步骤，都必须先明确授权和精确目标。
4. 所有真机任务使用唯一 Attempt/run prefix，先收集证据再清理，清理后校验 VM/shim/snapshot 差分。
5. 如果实现与本文的架构决策发生冲突，先新增/修订 ADR 和迁移方案，不要在代码中静默分叉。
