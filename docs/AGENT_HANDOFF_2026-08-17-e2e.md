# ClawBox 交接报告（2026-08-17 · E2E 冲刺 · 单 agent 任务跑通）

更新时间：2026-08-17（UTC）  
交接基线：`main` @ `dd3a352`（本 session 已推送）。  
上一份背景：`docs/AGENT_HANDOFF_2026-08-17-runtime.md`（Kata Secret 卷 root 修复、单容器 pod 架构、并发压测计划——§1/§2 结论仍有效）。  
目标主机：openEuler 24.03 LTS-SP1、aarch64（Kunpeng 128 核/2.0 TiB）、单节点 K8s 1.35.7、Kata 3.31.0 + Firecracker 1.12.1、`weitianc@193.124.7.2`。

## 0. 最重要的结论（当前状态）

**"单 agent 任务端到端跑通"已推进到最后一公里**：tool 桥、runtime sidecar、openclaw onboard、真实 DeepSeek LLM、agent 经 SSH 在 `/testbed` 干活（读源码/跑测试全 exit 0）全部打通。**当前唯一的未解阻塞 = 最终上传（central artifact upload）**，它让 cell 在 `RuntimeFailed` 终止。

本 session 累计修复 6 个问题（全部已推送，见 §6 commit 清单），最后一步（上传失败）正在排查中，**不要丢失排查证据**（见 §4）。

## 1. 已打通的能力（本次实测）

- Tool 桥：`tool bridge ready address=0.0.0.0:2222 workdir=/testbed arch=arm64`，SSH exec 全成功（tool-bridge.jsonl 记录 exit=0）。
- Runtime sidecar（进程内）：Uvicorn 8765、`/health/ready` 200、LLM 代理 + 事件追踪正常。
- openclaw onboard：`Default vLLM model: deepseek-v4-flash` 成功。
- LLM：DeepSeek 真实 API `status=200`（`model-fetch … elapsedMs=6-37`），agent 多轮对话稳定。
- agent→tool：SSH exec 进 `/testbed` 跑 `git`/`pytest`/python 全部成功；agent 曾推进到 turn ~55 读 `tests/test_parser.py` 分析任务。
- openclaw 跑完不退出 → **收割器修复**（见 §3.3），日志确认 `agent completed but CLI lingered; reaping as success`。

## 2. 三个镜像（本地 registry `127.0.0.1:5000`）

| 镜像 | 当前 digest（本 session 最新） | 说明 |
|---|---|---|
| `clawbox/runtime-arm64:dev` | `sha256:a39d6ba11d14889d759d541ee481aa5dd92a18753c722343cde743001c982c91` | 含 sidecar 进程内 + scope=shared + 收割器 + patch-ssh timeout + 上传 409 容忍 |
| `clawbox/control-plane-arm64:dev` | `sha256:ac4e218d38c8805d9e5417aaaff74ed70ba382125424fb57dd2f9b4ab65aa595` | 含 job deadline=timeout+300、`KUBERNETES_IMAGE_PULL_POLICY=Always`（部署 env） |
| `clawbox/tool-bridge-arm64:dev` | 未变 | Go 静态二进制，打进任务镜像 |
| 任务镜像（唯一用于 cell） | `127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:bdf4637498a4b765f0e91333ff292c226bd011a31c220d76609daff43e2c39fd` | 15five__scim2-filter-parser-13，内置 tool-bridge，/testbed chown 10001。旧 `ef4a5559…` 勿用 |

## 3. 本 session 的 6 个修复（实现细节，勿回退）

### 3.1 sidecar 进程内运行（commit `0abc1c1` 之后，本 session 完善）
- 背景：Kata 无共享文件系统，runtime Job 是单容器，但 `runtime-entrypoint.sh` 死等 127.0.0.1:8765 的 sidecar → 启动 60s 即挂。
- 实现：`scripts/runtime-entrypoint.sh` 在健康检查前 `&` 启动 `/usr/local/bin/clawtune-sidecar-entrypoint`（所需 env 全在 runtime job 里：TASK_ID/TRACE_UPLOAD_TOKEN/TRACE_INGESTER_URL/OPENAI_*/CLAWBOX_STATE_DIR/CLAWTUNE_TRACE_DIR），日志重定向 `${LOG_DIR}/sidecar.log`。

### 3.2 cell 容器 imagePullPolicy 强制 Always（commit `8859f8e` 只覆盖 controller 自身，本 session 补齐）
- `clawbox/common/config.py` 默认 `IfNotPresent` → `:dev` tag 改了不重拉（实测 runtime pod 跑旧 digest）。
- 修复：`deploy/cell-controller.yaml` env 加 `KUBERNETES_IMAGE_PULL_POLICY=Always`；线上已 `kubectl -n clawbox-system set env deployment/clawbox-cell-controller KUBERNETES_IMAGE_PULL_POLICY=Always` + rollout restart。改 controller env 后必须 rollout restart（Settings 是 import 时实例化）。

### 3.3 openclaw agent 跑完不退出 → 收割器（commit 本 session）
- 现象：`openclaw agent --local --json` 在 `run … stopReason=stop`、final-answer.json 写满后进程仍挂 `do_epoll_wait` 不退出，阻塞整个流水线。
- 实现（`scripts/runtime-entrypoint.sh`）：后台启动 agent；轮询 `final-answer.json` 非空=跑完 → 给 30s 宽限 → 仍存活则 SIGTERM/SIGKILL 并视 `agent_status=0`；另有 `agent_deadline=task_timeout+120` 兜底。**注意**：agent 自己的 `--timeout`=TASK_TIMEOUT 超时也会写 final-answer（内容可能是"超时了"），收割器无法区分"自然完成"和"超时写出答案"——对流水线验证够用，对任务成功判定需注意。

### 3.4 patch 收集裸 ssh 加超时（commit `dd3a352`）
- 现象：post-agent 的 `ssh … git diff` 无超时，可能卡死流水线直到 job deadline。
- 实现：两条 `ssh`（patch 收集 + tool-bridge.jsonl 抓取）包 `timeout 120` + `-o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3`，`|| true` 容错。

### 3.5 job deadline = timeout + 300（commit `dd3a352`）
- 现象：`timeoutSeconds` 同时设 agent `--timeout` 和 job `activeDeadlineSeconds` → 相等时 agent 占满预算，post-agent 流水线（patch+上传）无余量，job 在 deadline 被砍（`DeadlineExceeded`）。
- 实现：`clawbox/cell/manifests.py` runtime_job `activeDeadlineSeconds: timeout + 300`（`pipeline_grace_seconds=300`），与 controller `_timed_out`（timeout+300）对齐。改后必须重建 control-plane + rollout restart（§7.1）。

### 3.6 trace 上传 409 容忍（commit `dd3a352`）
- 现象：ingester 日志大量 `POST /v1/tasks/<id>/traces → 409 Conflict`（`trace upload retry: HTTPError`）。409 = 同一 offset 内容变了 → trace 文件被重写（怀疑 openclaw 会重写 session-*.jsonl，非 append-only）。若发生在最终上传（`upload_traces(final=True)`）会直接 abort → `.upload-failed` → RuntimeFailed。
- 实现（`scripts/artifact-uploader.py`）：chunk 上传捕获 HTTPError，`code==409` 时打印告警、把该文件 offset 置为 size（标记消费）、跳过不 abort；其余错误 re-raise（瞬时网络重试）。result.json + `.final` 标记仍严格上传。改后必须重建 runtime（§7.2）。

## 4. 当前未解决问题（下个 session 的第一优先级）

### 4.1 最终上传失败（正在排查，勿丢线索）
- **实测证据（第 4 遍 debug E2E，runtime pod `single-001-runtime-v54d5`，IP 192.168.3.131）**：
  - runtime 日志：`ready=true` → `agent completed but CLI lingered; reaping as success` → **`central artifact upload failed`**（entrypoint 见 `.upload-failed` → exit 1 → job `BackoffLimitExceeded` → RuntimeFailed）。
  - **ingester 日志里来自 `.131` 的请求一条都没有**（对比第 3 遍 debug 的 `.155` 有大量 409 POST）→ 上传是**连接层失败（DNS/网络），不是 409**。
  - 已排除：NetworkPolicy（ingester selector 与 deployment 标签匹配：`app.kubernetes.io/component=trace-ingester`）；ingester 服务正常（10.101.107.195:8084）。
  - 疑点：前一轮（.155）能到 ingester，这轮（.131）完全不到 → 像是 Kata VM 瞬时 DNS/网络问题，或 `clawbox-ingester.clawbox-system.svc` 在 VM 内解析失败。**需要拿到 runtime pod 内的 sidecar.log / 或 exec 进去测 DNS+curl 才能定论。**
- **排查手段（已备好）**：`/tmp/clawbox-watcher3.sh`（目标机）会 exec 进 runtime pod 复制 `/state/<task>/logs/{sidecar,agent,plugin,onboard,patch}.log`、`result.json`、`patch.diff`、`final-answer.json` 到 `/tmp/clawbox-cell-logs/`。**注意：本 session 最后一次尝试启动 watcher3 未成功**（pgrep 只有自身，watcher.log 停在旧 v2）——下 session 先 `pkill -f clawbox-watcher`、再 `CLAWBOX_WATCH_SECONDS=1500 nohup bash /tmp/clawbox-watcher3.sh &` 并确认 `WATCHER3_RUNNING` 再开 E2E。
- 若确认 DNS 问题：查 runtime-egress NetworkPolicy 的 DNS 规则（`to kube-system/kube-dns` UDP/TCP 53）是否在 Kata VM 生效；或在 entrypoint 里加 `nslookup/getent` 探针日志。

### 4.2 trace 文件重写导致 409（根因未定）
- clawtune 插件 `TraceWriter` 用 append 模式（`/home/weitianc/ClawTune/packages/clawtune-plugin/src/trace/writer.ts`），不会截断。409 必来自其他进程重写同名文件（怀疑 openclaw 重写 `session-*.jsonl`，或 sidecar/uploader 读到正在写的一半）。409 容忍已让流水线不 abort，但 trace 数据有缺口——任务成功判定不依赖 trace 完整性（receipt 只看 result + .final 标记），可后置处理。

### 4.3 agent 完成不了真实任务（时间预算 vs 任务难度）
- 第 3 遍（1800s）agent 干到 turn ~55 仍未完成；有**测试命令挂起 690s**（`tool-bridge.jsonl: duration_ms:690109, exit_code:124, timed_out:true`，疑似 testbed 的 pytest 挂起）。debug 用 `--timeout-seconds 480` 时 agent 在 480s 被 openclaw `--timeout` 截断（写"超时"答案），收割器照常放行。
- 建议：换更简单的任务镜像验证"真完成"，或排查挂起测试；`commandTimeoutSeconds`（tool 桥单命令超时，默认 300）可调小。

### 4.4 file 工具访问不了 /testbed（已知，绕行中）
- openclaw SSH sandbox 把 agent workspace 嵌套成 `/testbed/openclaw-ssh-shared-8198076c/workspace`（`djb2("shared")=8198076c` **对 scope=shared 恒定**，已用脚本验证），file 工具（read/write）只允许 workspace 挂载内，读 `/testbed/src/…` 报 "escapes allowed mounts"。
- 现状：agent 用 **exec 绕行**（cat/echo 等）能干活，暂时够用。若需 file 工具直通：可在 tool 侧把 `/testbed/openclaw-ssh-shared-8198076c/workspace` symlink 到 `/testbed`（路径恒定，runtime entrypoint 可在 agent 前 SSH 建 symlink）——**注意 symlink 有被 `rm -rf <dir>/` 清空的潜在风险，未验证 openclaw 是否会清 workspace**。

## 5. 环境事实（续前，重点更新）

- 远程命令：本地 Windows PowerShell 直接 `ssh.exe`/`scp.exe`（`ssh`/完整路径偶尔失灵，`ssh.exe` 裸名最稳）。**嵌套引号必坏**：命令一律写成脚本 scp 到 `/tmp` 再 `ssh … "bash /tmp/x.sh"`。远程命令开头 `export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12; unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY`。
- **无免密 sudo**（非交互 SSH 下 sudo 全失败）→ `single-image-scale.sh` 的 ctr 预拉/devmapper status 报 sudo 错误是预期、非致命。
- **笔记本断网会把 ssh 会话打死**（本轮发生过：`client_loop: send disconnect: Connection reset`，目标机无恙）。长跑命令建议 nohup 或接受中断后重查状态。
- LLM：真实 DeepSeek（`clawbox-llm` secret：upstream=`https://api.deepseek.com`、model=`deepseek-v4-flash`、model-ref=`vllm/deepseek-v4-flash`、key=36B 从 `~/ClawTune/swe_rebench/llm_api_key.txt` 本地注入，未过对话）。egress 用 `--llm-egress-cidr 0.0.0.0/0:443`。
- `/tmp/problem.txt`（59B）存在。ingester 部署在 `clawbox-system`，服务 `clawbox-ingester:8084`。

## 6. 本 session 提交清单（`main`，全部已推送）

```
dd3a352 runtime: bound patch-collection ssh; controller: job deadline = timeout+300 pipeline margin; uploader: tolerate trace 409
8ba2b76 single-image-scale: add --timeout-seconds for fast debug iterations; README: note debug budget + agent reaper
a6717ac runtime: run clawtune sidecar in-process (single-container Job); cells: force Always imagePullPolicy; README: validated single-task E2E playbook
（更早的 0abc1c1/c3ea48e/8859f8e 等见 runtime 交接文档）
```

## 7. 命令速查

### 7.1 只改 control-plane（manifests.py 等）
```bash
cd ~/ClawBox && git pull
docker build --platform linux/arm64 --pull \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -f docker/Dockerfile.control-plane -t 127.0.0.1:5000/clawbox/control-plane-arm64:dev .
docker push 127.0.0.1:5000/clawbox/control-plane-arm64:dev
kubectl -n clawbox-system rollout restart deployment/clawbox-cell-controller
kubectl -n clawbox-system rollout status deployment/clawbox-cell-controller --timeout=120s
```

### 7.2 只改 runtime（entrypoint/uploader 等）
```bash
cd ~/ClawBox && git pull
docker build --platform linux/arm64 --pull --build-context clawtune=~/ClawTune \
  --build-arg CLAWTUNE_REVISION=$(git -C ~/ClawTune rev-parse HEAD) \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn \
  -f docker/Dockerfile.runtime -t 127.0.0.1:5000/clawbox/runtime-arm64:dev .
# 镜像内路径不带 .sh：grep 用 /usr/local/bin/runtime-entrypoint / artifact-uploader
docker push 127.0.0.1:5000/clawbox/runtime-arm64:dev
```

### 7.3 跑调试 E2E（8 分钟预算，快速收敛）
```bash
cd ~/ClawBox
export NO_PROXY=localhost,127.0.0.1,193.124.7.2,10.96.0.0/12
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
# 先起 watcher3（务必确认 WATCHER3_RUNNING）
pkill -f clawbox-watcher; sleep 2; rm -rf /tmp/clawbox-cell-logs; mkdir -p /tmp/clawbox-cell-logs
CLAWBOX_WATCH_SECONDS=1500 nohup bash /tmp/clawbox-watcher3.sh >/tmp/clawbox-watcher3-nohup.log 2>&1 &
sleep 3; pgrep -f clawbox-watcher3.sh && echo RUNNING
# 再跑
bash scripts/single-image-scale.sh \
  --tool-image 127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:bdf4637498a4b765f0e91333ff292c226bd011a31c220d76609daff43e2c39fd \
  --problem-file /tmp/problem.txt --llm-egress-cidr 0.0.0.0/0 \
  --count 1 --timeout-seconds 480 --wait-seconds 1200
```

### 7.4 验证点
- runtime 镜像：`docker run --rm --entrypoint /bin/sh 127.0.0.1:5000/clawbox/runtime-arm64:dev -c 'grep -c "exc.code != 409" /usr/local/bin/artifact-uploader; grep -c "timeout 120 ssh" /usr/local/bin/runtime-entrypoint'`（应输出 ≥1）。
- cell 终态：`kubectl get sandboxtasks -n clawbox-benchmarks -o wide`；controller 日志 `kubectl -n clawbox-system logs deployment/clawbox-cell-controller --tail=50`。
- ingester 请求：`kubectl -n clawbox-system logs deployment/clawbox-ingester --tail=500 | grep POST | tail`（看 409/200/来源 IP）。

## 8. 下一步建议（按优先级）

1. **修最终上传**：起 watcher3 → 重跑 debug E2E → 拿到 runtime pod 内 `sidecar.log` 的上传错误（DNS? 连接? result 缺失?）→ 针对性修。若 DNS：查 runtime-egress DNS 规则或 entrypoint 加 DNS 探针。
2. **跑通一次真实成功**：上传修好后，用 `--timeout-seconds 1800` 或更长跑完整任务（agent 需在预算内真正完成，换简单任务或解决挂起测试）。
3. 并发压测（原定目标）：`--count 4/8/16/32`，注意 devmapper Data% 和 `--delete`。
4. 杂项：watcher3 脚本的 `/state/<task>` 路径假设（用 label 取 task 名）已写，但未实测；`single-image-scale.sh` 的 sudo 提示可用 NOPASSWD 或交互跑消除。
