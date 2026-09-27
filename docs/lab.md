# 日常实验入口

CubeSandbox 在这里使用单机 standalone 部署：它管理 VM、模板和网络，
不需要部署 Kubernetes。`scripts/lab` 负责检查本机环境，调用同一套
`clawbox experiment` 执行逻辑，并清理本次创建的 VM。

在已安装 CubeSandbox、匹配 SDK、Runtime/Tool 模板和 ClawBox Python
环境的 Linux 主机上使用。首次安装或迁移到空白机器仍需要
[安装文档](installation.md)中的镜像和内核；脚本不会格式化磁盘或删除旧实验。

## 开机后准备一次

```bash
cd /path/to/ClawBox
bash scripts/lab setup --warm
bash scripts/lab doctor
bash scripts/lab baselines
```

脚本自动读取 `~/.config/clawbox/machine.env`，从模板 API 获取真实镜像摘要、
VM 大小和节点，将验证后的配置保存到 `~/.config/clawbox/lab.json`。
已有配置优先于旧环境文件；重复 setup 不会把升级后的模板换回旧版本。
首次可通过 `setup --runtime-template ID --tool-template ID --node NODE` 指定模板。
`--warm` 在空闲主机上准备 tmpfs；已有运行中的 VM 不会被停止。
需要对已安装的 systemd 服务和挂载操作有 sudo 权限。
准备阶段会等待节点完成初始化，并创建、执行、销毁一台临时 VM 验证可用性。
`--warm` 会备份并同步 standalone 启动配置中的快照目录，必要时重启空闲节点的相关服务。

## 选择 baseline 并运行 trace

任务 YAML 指定任务提示词、原始 trace、模型名和最终验证命令。
任务镜像必须含有对应项目及初始版本。脚本默认回放已有模型响应，不调用付费模型 API；
工具命令仍真实执行。不要把 schema 示例 trace 当作真实任务录制。
回放工具名须匹配当前 OpenClaw 接口。对 SWE-rebench research schema-5 录制，先导入：

```bash
bash scripts/lab import-trace original.trace.jsonl --output replay.jsonl \
  --python /opt/conda/envs/testbed/bin/python
```

原文件不变，输出 schema-6 replay 和 `replay.import.json`，后者记录来源摘要、
原环境说明及逐调用转换。任务 YAML 的 trace 路径指向新文件。
支持 `edit_file` → `edit`、仅带 path 的 `read_file` → `read`、`write_file` → `write`，
以及 `exec.working_dir` → `workdir`。`replace_all=true` 等尚未支持的参数直接报错。
`list_dir` 转成 Tool VM 内的 Python 目录枚举，支持排序、递归和总条目上限，
不递归跟随目录符号链接；文本格式不保证与历史版本完全相同。其他未知工具直接拒绝。
导入不更改原 shell 命令或编辑文本，也不会自动安装依赖。

为任务镜像固定源码初始提交和测试依赖，并配置运行前检查，例如：

```yaml
sandbox:
  template_id: tpl-task
  workspace: /testbed
  preflight_command: >-
    test -d /testbed/.git &&
    /opt/conda/envs/testbed/bin/python -c 'import pytest, sqlglot'
validation:
  command: cd /testbed && /opt/conda/envs/testbed/bin/python -m pytest -q
```

检查在新建 Tool VM 中、Agent 启动前执行，失败即终止并清理本 run 的 VM；
结果保存在 events 的 `task_environment_checked` 记录中。检查命令用于验证，
依赖安装应在构建镜像时完成。`python3` 等入口需要能启动任务虚拟环境，
不要只把虚拟环境解释器软链接到其他目录。跨架构或依赖版本差异可能改变行为；
回放不会重新规划，最终任务验证和遥测验证仍须通过，才能用于训练及比较。

如果已构建并推送了带 ClawBox Tool 集成的新任务镜像，可以注册独立任务配置：

```bash
.venv/bin/python scripts/register-task-image.py \
  --image REGISTRY/task@sha256:DIGEST --alias task-unique-name --output task-profile.json
bash scripts/lab --profile task-profile.json run task.yaml --baseline tool-static-resident
```

它复用默认配置的节点、guest 内核和 VM 大小，并保留原有 lab 配置。
新镜像需要包含任务初始代码和测试依赖；普通项目 Docker 镜像不能直接作为 Tool 模板。

```bash
# 常驻 VM；两组实验分别运行 1 个和 4 个 Agent，每个 Agent 使用两台 VM。
bash scripts/lab run task.yaml --concurrency 1 4 --baseline tool-full-resident

# 比较固定内存预留下的常驻与立即保存策略；默认只允许内存快照。
bash scripts/lab run task.yaml --concurrency 1 4 \
  --reserve-during command --estimate fixed --idle resident --idle immediate

# 已有覆盖任务命令的 LatticeKB 额外内存峰值数据时，使用预测准入和 WARM。
bash scripts/lab run task.yaml --baseline tool-p50-eager-reactive --concurrency 2

# 只回放前 3 次模型响应，用于短验证；不代表完整任务完成。
bash scripts/lab run task.yaml --max-model-steps 3 --concurrency 1
```

可以重复 `--baseline NAME`；`baselines` 列出所有支持的组合及额外配置要求。
维度参数与 `clawbox experiment configure` 相同：`--reserve-during`、
`--estimate`、`--idle`、`--resume`。省略选择时使用 `tool-full-resident`。
`--model NAME` 可补充录制模型名；`--trace FILE` 仅允许替换单任务 YAML 的录制路径，
不会替换提示词或任务镜像。其他固定值仍由任务 YAML 配置；静态准入的固定预留量
可由 `--static-tool-memory-mib` 在运行时覆盖。

`tool-p50-*` 使用 guest 额外内存峰值的 P50 预测。先用固定准入采集完整任务，
再从通过最终验证的 CubeSandbox 训练 run 生成冻结预测文件：

```bash
bash scripts/lab train ~/clawbox-results/TRAIN_RUN --trace /data/replay.jsonl \
  --repository owner/repo --output /data/p50.json
bash scripts/lab run task.yaml --predictions /data/p50.json \
  --baseline tool-static-resident --baseline tool-p50-resident --concurrency 1 4
```

静态准入的每次调用预留量可以在命令行覆盖，无需修改任务 YAML：

```bash
bash scripts/lab run task.yaml --baseline tool-static-resident \
  --static-tool-memory-mib 512 --concurrency 4 --pool-gib 8
```

实验应记录该数值的来源。使用独立训练 run 中实测最大额外内存向上取整，
比把每次调用都按 Tool VM 的完整配置容量计费更适合作为校准后的静态对照。

ClawBox 优先使用 LatticeKB，不可用时回退 ToolKB；两者均须使用 guest
`MemTotal - MemAvailable` 峰值减去调用前基线的测量口径。若两者均不可用，
只有成功训练样本全部不超过 20 ms、且因执行太短而没有执行中采样的相同命令，
才按轻量命令处理，最小预留 1 MiB。结果单独标记该假设，不将其当作实测零值。
其他缺失预测会报错。任务 YAML 必须设置最终 `validation.command`，
训练和评估使用独立 run，并保持 Tool 镜像与 VM 配置一致。
不传 `--predictions` 时仅使用 Runtime 提供的 LatticeKB P50。

默认 `--storage memory` 禁止 COLD checkpoint 和 WARM→COLD 溢出。
WARM 必须为 tmpfs，且主机禁用 swap。容量不足会等待或失败，不会写磁盘大 RAM 快照。
`incremental-cow` 的首代仍是完整内存基线，保存在 tmpfs；后续代保存脏页增量。
因此 WARM 容量要容纳首代基线及仍被引用的增量，不能只按单次 delta 大小配置。
依赖 COLD 的分层 baseline 必须显式使用 `--storage disk`，并在 YAML 中配置
COLD 路径和分层资源。Guest 正常的文件系统写入、镜像与模板仍使用磁盘。

## 查看与清理

```bash
bash scripts/lab status ~/clawbox-results/RUN_ID
bash scripts/lab cleanup ~/clawbox-results/RUN_ID
```

每次运行生成独立目录，包含展开后的 `experiment.yaml`、`lab-state.json`、
原始测量和报告。结束、失败或可处理的中断后，脚本销毁本 run 的 VM 并检查归属清单。
不导出 VM 镜像包，保留任务模板供下次使用。SSH 断开或进程被强杀后可再次执行
`cleanup`；它不会删除别人的 VM 或结果。运行期间保持终端连接，或在 tmux 中运行。

## 更新镜像集成

升级 ClawTune 插件、Sidecar 或 Tool bridge 后，使用同一源码重建两种模板：

```bash
bash scripts/lab images --registry REGISTRY/clawbox
```

该命令需要 Node/npm、Go、Docker 和可推送的镜像仓库；`CLAWTUNE_ROOT` 指向匹配源码。
它保留选中任务镜像的项目环境，只更新集成代码，注册新模板，成功后更新 lab 配置。
原模板不删除。Go 不在 PATH 时用 `--go /path/to/go`；失效的本机代理可用
`--direct-network` 绕过，也可指定 `--pip-index URL`。
镜像更新记录保存在配置旁的 `lab.images.json`，中断后可据此核对已注册的模板。
Tool 镜像更新面向 ARM64；在 `machine.env` 中配置 `CLAWBOX_GUEST_KERNEL_SOURCE` 和
`CLAWBOX_GUEST_KERNEL_BUILD`，分别指向构建实际 guest 内核的源码和输出目录，也可用
`--kernel-source`、`--kernel-build` 指定。脚本临时启动 VM 核对内核配置，使用该次构建生成的
eBPF 头文件。只更新 Tool 可加 `--role sandbox`，只更新 Runtime 可加 `--role runtime`。

## 修复 Cubelet 的 WARM 分配中断

使用尚未处理 `fallocate(EINTR)` 的增量 CubeSandbox 构建时，在空闲主机执行一次：

```bash
bash scripts/lab repair-warm
bash scripts/lab setup --warm
```

`CUBE_SOURCE_DIR` 指向已安装版本的源码，`CLAWBOX_GO` 指向 Go 工具链。
若源码未包含 COW 构建依赖，`CLAWBOX_COW_SDK` 指向匹配版本的 `include/` 和 `lib/` 目录。
命令修复源码、测试 EINTR 重试及空间不足失败、构建并备份替换 Cubelet；有运行中 VM 时拒绝安装。
它只重试被信号中断的同一分配范围，不重试空间不足等其他分配错误。
