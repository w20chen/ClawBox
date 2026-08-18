# ClawBox 交接：read/write 工具修复 + 真实任务全链路成功（2026-08-17 19:33 UTC）

> 后续权威路线图：`docs/AGENT_HANDOFF_2026-08-18-managed-sandbox-roadmap.md`。本文保留单任务里程碑与真机证据；从当前 MVP 走向 managed agent sandbox system 的实施顺序、安全门和最终验收以新文档为准。

目标主机：openEuler 24.03 LTS-SP1、aarch64、单节点 K8s 1.35.7、Kata 3.31.0 + Firecracker 1.12.1、`weitianc@193.124.7.2`（PowerShell `ssh.exe`/`scp.exe`，远程命令用 `bash /tmp/x.sh` 脚本，注意引号）。

## ✅ 里程碑：真实 SWE-ReBench 任务全链路跑通（cell `real001-001`）

- **结果**：Cell `real001-001`（真实问题描述 + 1800s 预算）→ `Cleaned/Succeeded` 21m，`agent_exit_code 0`。
- **ingester result** `48a9a077...`（92718B）：`status=succeeded`、`patch_status=present`、**patch len=1175**（`src/scim2_filter_parser/transpilers/sql.py`）、`final_answer` 13329B（"Done. The change is complete and verified."）。
- **agent 行为**：用 **read 工具**读 `/testbed/src/scim2_filter_parser/transpilers/sql.py`、**edit 工具**改（`edit: ok in 1650ms`），产出与 gold patch（PR #13）语义一致的修复：`AttrPath = namedtuple('AttrPath', ('attr_name','sub_attr','uri'))` 替代普通 3-tuple；随后用 conda 环境 `/opt/miniconda3/envs/testbed/bin/python` 跑 `tests/test_attr_paths` 验证。
- **关键证据**：`escapes count = 0`（此前每次 read 都报 "Sandbox path escapes allowed mounts"）；`exec 19`、`LLM 52`。

## 🔧 read/write 工具修复（上轮遗留 3.1，已完成）

**根因**（openclaw 源码分析，npm pack openclaw@2026.7.1-2）：
- SSH sandbox 的 `getMounts()` 唯一 container mount = `remoteWorkspaceDir`（=`<workspaceRoot>/openclaw-ssh-<scope>-<hash>/workspace`，即 `/testbed/openclaw-ssh-shared-8198076c/workspace`）。
- `resolveTarget` 对绝对路径做**字符串前缀**检查（`isPathInsideContainerRoot`），symlink 无法绕过——绝对 `/testbed/...` 必须让 mount root 字符串 == `/testbed`。
- `resolveSshRuntimePaths` 恒拼接 `runtimeId`（djb2 hash），配置无法让其等于 /testbed → **只能打补丁**。

**修复**（commits `3a77bf1`/`7000c50`/`10217d5`，runtime 镜像 `sha256:cd5fec73`）：
1. `docker/Dockerfile.runtime`：heredoc `RUN` 内 node 脚本把 dist 里 `remoteWorkspaceDir: path.posix.join(runtimeRootDir, "workspace")` → `remoteWorkspaceDir: workspaceRoot`（用 `npm root -g` 定位，node:22 全局装在 `/usr/local/lib/node_modules`，不是 `/usr/lib`；第二个 node 验证脚本确保替换存在否则构建失败）。
2. `scripts/runtime-entrypoint.sh`：agent 启动前 SSH 预建哨兵目录 `/testbed/openclaw-ssh-shared-8198076c`（scope=shared 恒定 hash），让 openclaw `ensureRuntimeInner` 的 `[ -d runtimeRootDir ]` guard 通过、跳过 `replaceRemoteDirectoryFromLocal`（该函数先 `clearRemoteDirectory` rm -rf 再上传本地 /workspace，**会清空 /testbed**）。另在顶部补 `tool_target` 变量。

**验证**：
- 镜像内 `browser-bridges-DdlAaIG3.js` 含 `remoteWorkspaceDir: workspaceRoot`；`/tmp/verify-runtime-patch.sh`。
- dbg10217-001（900s，模糊提示）→ Cleaned/Succeeded，`escapes=0`；但 agent 花光预算探索环境，`patch_status=empty`。
- real001-001（真实提示 + 1800s）→ 完整成功（见上）。

## ⚠️ 已知问题/注意
- **任务提示**：`/tmp/problem.txt`（"Fix the reported issue..."）太模糊 → agent 全花在探索。真实 SWE-bench 问题描述（`/tmp/problem-real.txt`，基于 issue #12 原文）显著提速。**跑真实任务务必用真实问题描述**。
- **测试环境**：任务镜像默认 python 无 `sly`/`django`（测试依赖），pip 无 egress 装不上；正确环境是 `/opt/miniconda3/envs/testbed/bin/python`。agent 找了一会儿才用上。
- **间歇性 runtime→tool VM SSH 255**：即使 `mkdir && echo` 也偶发返回非零（哨兵预建步骤打印 WARN 但目录已建、无害）。post-agent SSH 用 `timeout -k 10 120` 兜底。
- **任务名复用 → result 409**：ingester result 不可变。调试/压测必须唯一前缀（如 `real001`/`dbg$(date +%s)`）。
- devmapper 状态查询需要 sudo（无免密），scale 脚本会打印 `sudo: a password is required`（无害）。

## 待办（下个任务）
1. **并发压测（handoff #4）**：`scripts/scale-swe-rebench.sh --count 4/8/16/32`（或 `single-image-scale.sh`），唯一前缀，关注 devmapper Data%、pod 启动时序；用 `--delete` 清理。
2. **真实任务成功率提升**：给 agent 提示里带上测试环境路径（conda testbed），减少探索时间；或调高 `agents.defaults.timeoutSeconds`。
3. **（可选）sly/django 预装进任务镜像**：免去 agent 找环境。

## 当前镜像
- runtime: `127.0.0.1:5000/clawbox/runtime-arm64:dev` @ `sha256:cd5fec73`（openclaw 补丁 + 哨兵预建）
- control-plane: `127.0.0.1:5000/clawbox/control-plane-arm64:dev` @ `sha256:9fd6fbf8`（不变）
- task: `.../swe-rebench-arm64@sha256:bdf4637498...`（15five__scim2-filter-parser-13）

## 运维速查
- 同步目标机仓库：`scp scripts/sync-target-repo.sh ...:/tmp/ && ssh ... "bash /tmp/sync-target-repo.sh"`（git fetch 需 `-c http.proxy= -c https.proxy=`）。
- 重建 runtime：`bash /tmp/rebuild-runtime.sh`（从 `~/ClawBox` worktree 构建 + push 本地 registry；RUNTIME_IMAGE=...:dev 为 Always pull，无需改 controller）。
- 监控：`bash /tmp/check-prefix.sh <prefix>`、`bash /tmp/check-agent-log.sh <prefix>`、`bash /tmp/verify-read-fix.sh <task_id>`、`bash /tmp/dump-result.sh <task_id>`、`bash /tmp/dump-answer.sh <task_id>`（均在目标机 /tmp）。
- watcher：`CLAWBOX_WATCH_SECONDS=N bash /tmp/start-watcher.sh`。
