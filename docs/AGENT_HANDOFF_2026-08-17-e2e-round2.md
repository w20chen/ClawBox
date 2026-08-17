# ClawBox 交接报告（2026-08-17 · 第二轮 E2E 冲刺 · 上传修复 + Cell Succeeded）

更新时间：2026-08-17（UTC，深夜场）
交接基线：`main` @ `c052ba7`（已推送）。
上一份背景：`docs/AGENT_HANDOFF_2026-08-17-e2e.md`（§0 结论"唯一未解阻塞=最终上传"在本轮**已解决**）。
目标主机：openEuler 24.03 LTS-SP1、aarch64、单节点 K8s 1.35.7、Kata 3.31.0 + Firecracker 1.12.1、`weitianc@193.124.7.2`。

## 0. 最重要的结论（当前状态）

**"单 agent 任务端到端"已完整跑通并拿到 `Cleaned/Succeeded`**。上传（此前唯一阻塞）根因已定位并修复：

- **根因 1（commit `7c64073`）**：`scripts/artifact-uploader.py` 在某次 Windows 编辑后变成 **CRLF 行尾**，shebang 变成 `#!/usr/bin/env python3\r` → 上传器进程一 exec 就死，**从未运行** → 所有上传（trace/result/receipt）全失败 → `.upload-failed` → RuntimeFailed。此前误判为"Kata VM 网络间歇故障"（rounds 4/5 零请求其实是上传器根本没起来）。
  - 修复：转 LF + `.gitattributes` 补 `*.py`/`*.go eol=lf`（原来只有 `*.sh`/`Dockerfile*`，`*.py` 漏网）。
- **根因 2（commit `c052ba7`）**：上传修好后 cell 仍 `TimedOut`——agent 用满预算后只剩 300s pipeline 余量，post-agent 的 SSH 收集（`timeout 120 ssh` 无 `-k`）卡住即爆预算。
  - 修复：① SSH 改 `timeout -k 10 120`（ssh 卡 S-state poll 时 SIGKILL 能杀）② post-agent 阶段加进度 echo（collecting patch / tool-bridge / writing result / awaiting final upload）③ `pipeline_grace_seconds` 300→600（`manifests.py` 的 `activeDeadlineSeconds` 与 `controller._timed_out` 两处一致）。

**实测验证（16:51 UTC）**：Cell `single-001` 全生命周期 `Queued 16:43:13 → ToolReady 16:43:21 → RuntimeStarted 16:43:23 → Succeeded 16:51:52 (Cleaned)`；markers `.runtime-complete`+`.upload-complete` 均在；ingester DB 551 chunks + result 行（sha `ab0eb985`）；E2E runner 输出 `RESULT: all 1 cells reached a successful terminal phase`。

## 1. 本轮修复的镜像（本地 registry `127.0.0.1:5000`）

| 镜像 | 当前 digest | 说明 |
|---|---|---|
| `clawbox/runtime-arm64:dev` | `sha256:8f8c0e0a...` | LF 上传器 + startup probe（ready 后 getent/curl 探针）+ 上传失败时 dump sidecar.log/resolv/getent/curl/route 到 kubectl logs + post-agent 进度 echo + `timeout -k 10` |
| `clawbox/control-plane-arm64:dev` | `sha256:9fd6fbf8...` | `pipeline_grace_seconds=600` |
| 任务镜像（不变） | `...@sha256:bdf4637498...` | 15five__scim2-filter-parser-13，内置 tool-bridge |

## 2. 目标机仓库卫生（重要，勿再踩）

- 目标机 `~/ClawBox` 之前 HEAD 停在旧 commit，且带**未提交的 CRLF 本地修改**（`manifests.py`/`artifact-uploader.py` 等）——之前镜像都从 worktree 构建，这是 CRLF 污染的来源之一。
- 已备份到 `/tmp/clawbox-repo-backup-20260817/`，并 `git reset --hard origin/main`（`c052ba7`）。**以后改目标机代码一律走 git**，不要直接编辑 worktree 文件再构建。
- **git 拉取必须禁代理**：全局 git config 配了 `socks5h://127.0.0.1:1080`，代理常不在线 → `git -c http.proxy= -c https.proxy= pull/fetch`。
- 行尾验证用目标机 Linux：`git ls-files --eol` / `od -c`（PowerShell 管道会破坏 `\n`→`\r\n`，不可信）。

## 3. 当前未解决问题（下个 session 优先级）

### 3.1 agent 的 read/write 文件工具访问不了 /testbed 源码（交接 §4.4 已确认）
- 本 run（900s 预算）agent.log 明确：`[tools] read failed: Sandbox path escapes allowed mounts; cannot access: /testbed/src/scim2_filter_parser/__init__.py`。
- **exec 全程正常**（agent 靠 exec 绕行，turn 36+，真实命令 `ok in 10s`）。read 只允许 sandbox workspace 挂载内（`/testbed/openclaw-ssh-shared-8198076c/workspace`）。
- **修复方向**：让 `/testbed` 进入 sandbox 允许挂载。可在 runtime entrypoint 在 agent 启动前，用 SSH 到 tool 侧执行 `ln -s /testbed /testbed/openclaw-ssh-shared-8198076c/workspace`（注意 djb2("shared")=8198076c 恒定）。未验证 openclaw 是否会清 workspace，需实测。
- 另：agent 曾报 `exec host not allowed (requested gateway; configured host is sandbox)`——是 config 限制，无需处理。

### 3.2 runtime→tool 的 Kata VM 网络间歇故障（255 之谜）
- 独立环境验证：**tool-bridge + openclaw 完整 read 命令机制本身正常**（关闭 stdin 时 rc=0、13ms；openclaw 的 `runSshSandboxCommand` 会 `child.stdin.end()` 立即关 stdin，不会挂）。
- 但真实 runtime pod 内**手动** `ssh executor@<tool> echo hi` 也间歇挂起（`-v` 也等不出结果）或快速 255——说明 runtime→tool 两个 Kata VM 之间的网络**偶发故障**。agent 的 exec 恰好落在正常窗口时工作。
- 排查建议：在 runtime 侧加启动后重试/探针（现有 startup probe 只测 ingester）；或查 kata-fc 的网络拓扑（两个 VM 在同一 node，走 Calico；看 felix 日志、tap 设备）。**此问题优先级低于 3.1**（exec 绕行已可用）。

### 3.3 真实任务完成（"跑通一次真实成功"）
- 900s 预算的 run 中 agent 用 exec 干到 turn 59（20-60s 真实命令），**产出 patch.diff 1790 字节（真改了代码）**，但 900s 超时 → openclaw exit 124 → cell Failed（上传本身 OK）。
- 若 read 工具修好（3.1），agent 读源码效率会高很多，真实完成概率更大。

### 3.4 任务名复用导致 result 409（测试流程注意，非 bug）
- 实测：`single-001` 在 16:51 已上传 result（不可变，task_id 主键首写胜出）。18:05 重跑同名校验 → 最终上传 `POST /v1/tasks/single-001/result → 409 Conflict`（内容不同）→ `.upload-failed` → cell Failed。**管线完整到达了最终上传**（traces 已传、result 尝试上传），失败纯粹是重跑同名任务。
- **调试/压测必须用唯一任务名**：`--prefix dbg$(date +%s)`（或清理 ingester DB）。trace 409 已容忍，result 409 严格（by design 不可变）。

## 4. 运维速查（更新）

```bash
# watcher 启动（pkill 用 [e] 防自匹配；setsid 完全脱离）
CLAWBOX_WATCH_SECONDS=2400 bash /tmp/start-watcher.sh   # 脚本已更新，含 marker 确认

# 只改 control-plane
docker build --platform linux/arm64 --pull --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -f docker/Dockerfile.control-plane -t 127.0.0.1:5000/clawbox/control-plane-arm64:dev .
docker push 127.0.0.1:5000/clawbox/control-plane-arm64:dev
kubectl -n clawbox-system rollout restart deployment/clawbox-cell-controller
kubectl -n clawbox-system rollout status deployment/clawbox-cell-controller --timeout=180s

# 只改 runtime
docker build --platform linux/arm64 --pull --build-context clawtune=~/ClawTune \
  --build-arg CLAWTUNE_REVISION=$(git -C ~/ClawTune rev-parse HEAD) \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn \
  -f docker/Dockerfile.runtime -t 127.0.0.1:5000/clawbox/runtime-arm64:dev .
docker push 127.0.0.1:5000/clawbox/runtime-arm64:dev

# 调试 E2E（建议预算 900s+，让 agent 有时间真干活）
bash scripts/single-image-scale.sh \
  --tool-image 127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:bdf4637498a4b765f0e91333ff292c226bd011a31c220d76609daff43e2c39fd \
  --problem-file /tmp/problem.txt --llm-egress-cidr 0.0.0.0/0 \
  --count 1 --timeout-seconds 900 --wait-seconds 1800
```

## 5. 下一步建议（按优先级）

1. **修 read 工具（3.1）**：runtime entrypoint 里在 agent 启动前 SSH 建 `/testbed/openclaw-ssh-shared-8198076c/workspace → /testbed` symlink，跑 900s E2E 验证 agent 能用 read 读源码。
2. **跑通真实任务成功**：read 修好后用 1800s+ 跑完整任务；关注 agent 是否在预算内产出 patch（`patch.diff` 非空）。
3. **并发压测**（原定目标）：`--count 4/8/16/32`，注意 devmapper Data% 与 `--delete`。
4. 杂项：runtime→tool VM 网络间歇故障（3.2）值得在空闲时单独排查（felix/tap 层）。
