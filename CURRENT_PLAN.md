# Current Plan

Updated: 2026-10-22

## 环境、镜像位置与共享边界（2026-10-22 20:50 中国时间核对）

**503 个任务环境已准备好；环境内容验证、并发功能验收、存储性能验证是三件独立的事。** 官方镜像源码/依赖适配 ARM、生成 ext4 模板、Cube 用 CoW 创建独立 VM，都是正常的环境交付形式。额外按磁盘压力分批做的跨模板块去重是临时容量优化，不是环境运行的必要步骤，也尚未建立性能中性的证据。

**最新并发状态纠正（20:52:30 中国时间 / 12:52:30 UTC）：本轮已出现失败，不再是零失败运行，不能通过释放屏障将它认作成功。** 事件记录有 1279 个环境检查和 1279 份原生工具采集，2558 台 VM 创建、252 台销毁、126 个 `session_failed`；125 个 `session_complete` 均为 `valid=false`，不是成功数。监控记录的完整 VM 对峰值为 1178，未证明 1280 同时存活。屏障累计 arrivals 为 1279，但 arrivals 不会扣除随后失败/销毁的任务，不能当成当前活跃数。前两个验证失败样本在屏障 HTTP 请求收到 `ConnectionResetError: [Errno 104]`，另有 1 个 ApiError 尚未定位。不能把这些失败归因于 ext4 去重，亦不能据此推翻已完成的 503 环境一致性检查。

CLI 目前仍为 `running`、`cleanup_verified=false`，存在剩余 VM；goal 保持暂停且未完成。本次仅记录，没有释放、重试或改配置。**接续时应先保留证据并用下文 CLI abort 清理此失败运行，再排查屏障连接与 API 失败；不要照旧交接节等待 1280 后释放。** 21:55 的等待上限不再是唯一阻碍，提前出现的失败已经使本轮不能通过验收。

### 准备状态与任务身份

- 当前 catalog 确认为 503 accepted images、503 registered templates、503 VM-smoke verified、503 parser-loaded、pending 0。全量一致性审计和原始 trace 首轮原生工具资格验证均已通过；不代表完整长 trace replay 已通过。
- 源码直接来自原 Benchmark 镜像 `/testbed`；保留原 Git HEAD、工作区文件及依赖版本，不用 clone/reset/checkout 重建。宿主是 ARM/openEuler，接受的任务用户态为 Debian 12 系列；宿主发行版不要求 guest 使用同一发行版。ARM 二进制及架构相关系统包差异有记录，录制时镜像 digest 缺失的限制仍然存在。
- 用 catalog 的 `manifest` 和 `workload` 字段定位确切任务清单，不通过目录数量猜就绪数。`final-state.json` 包含历史尝试（当前 546 条），不是 546 个接受环境；接受集合以 503 条 collector/Cube 状态与 catalog 为准。成功条目可能保留历史 `error`，应结合最终 status 和验证回执判断。
- 每条 trace 的映射是：`case_id -> 原镜像 manifest / 原 testbed -> ARM image_id -> 注册用 pinned_image digest -> template_id -> Cube artifact_id -> ext4`。镜像 ID、registry manifest digest、模板 ID 和 artifact ID 不是同一个标识。需要查具体 ext4 时，通过 `Template.get(template_id).replicas` 的 `artifact_id` 定位，不能把 `tpl-*` 当成 `rfs-*` 文件名。

### 文件和镜像在哪里（以下均为 kunpeng 路径）

| 内容 | 位置及使用方式 |
| --- | --- |
| 接受集合、任务到镜像/模板的映射 | `/mnt/clawbox-data/artifacts/swe-readiness-20261021/environment-catalog-20261022/latest.json`；读取其中 `manifest`、`workload` 指向的冻结文件 |
| 环境准备根目录 | `/home/weitianc/clawbox-swe-images-20261021/` |
| 原镜像提取的仓库和清单 | 准备根下 `workspaces/<规范化任务名>/testbed/`、`prepared.json`、`source-file-manifest.json`；保留作为来源证据 |
| ARM Docker 镜像身份 | 准备根下 `build/final-state.json`、`build/collector-state.json`；最终 collector 镜像如 `clawbox-swe-collector:armmbed__mbed-tools-285`，执行/审计以保存的不可变 `image_id` 为准 |
| Docker 本地镜像层 | `docker info` 确认 `DockerRootDir=/var/lib/docker`；由 Docker 管理，不是一任务一个 `.ext4` 或 `.tar`，不要直接编辑内部文件 |
| Cube 注册使用的 OCI 镜像 | `build/cube-collector-state.json` 的 `pinned_image`，形如 `127.0.0.1:5000/clawbox/swe-traces@sha256:...`；同条目给出 `template_id` |
| 本机 registry 数据 | `clawbox-registry` / `clawbox-registry-podnet` 共享 volume，宿主为 `/var/lib/docker/volumes/263663430d5263294332fc2cd199929b83791dffebef7c455df9b192d7a1b2f5/_data`，容器内为 `/var/lib/registry`；由 registry 管理 |
| Cubelet 模板缓存服务路径 | 配置 `cubetool_base_dir=/home/weitianc/clawbox-storage/cubelet-cache`，其 `cubebox_os_image/` 即 `/mnt/clawbox-data/cubelet-cache/cubebox_os_image/`；模板文件通常为 `<artifact_id>/<artifact_id>.ext4` |
| Cubelet 新缓存的实际写入位置 | 上述 `cubebox_os_image` 是扩容后根盘目录 `/data/clawbox-preparation-storage/cubelet-cache` 的 bind mount。旧条目有链接，示例解析到 `/mnt/clawbox-data/cubelet-cache/cubebox_os_image-before-root-routing/`；对具体条目用 `readlink -f` / `findmnt -T` 确认，不能假定全部已搬迁 |
| CubeMaster 模板存储服务路径 | `/data/CubeMaster/storage/`，bind 到根盘 `/data/clawbox-preparation-storage/master/`；新文件写根盘，旧条目仍链接到 `/mnt/clawbox-data/cubemaster-storage-active/` |
| 较早参与去重的另一缓存位置 | `/usr/local/services/cubetoolbox/cubebox_os_image/<artifact_id>/<artifact_id>.ext4`；仍保留，不应误当成唯一的当前缓存根或直接删除 |
| 运行中 VM 的独立磁盘/CoW 存储 | `/data/cubelet/storage/`；当前 Cubelet `storage_backend=cubecow`，位于扩容后的根盘；由 Cube 生命周期管理 |
| 一致性审计证据 | `/data/clawbox-acceptance-20261022T102744Z/fidelity/summary.json`、`fidelity/cases/` |
| 当前并发运行证据 | `/data/clawbox-acceptance-20261022T102744Z/current-run.json` 指向的 run；输入、trace、代码、事件和清理证据均保留 |

`/home/weitianc/clawbox-storage` 指向独立数据盘 `/mnt/clawbox-data`，但其中部分子目录又 bind 到扩容后根盘。路径前缀不等于物理磁盘归属；移动、删除前必须检查最终目标。当前不需要重新搬盘、清理镜像或重建 503 个模板。

### 实际做过的共享

1. **Docker 层复用**：多个镜像可以引用相同的只读层；每个容器另有可写层。不是自动对所有不同镜像内部的相同文件做块去重。
2. **Cube 原生 CoW**：当前 `cubecow` 从模板创建独立 VM 磁盘时使用 reflink/写时复制。取消额外跨模板去重，并不等于关闭 Cube 自身的 CoW。
3. **额外跨模板 ext4 块去重**：自编批处理使用 `duperemove` 以 4 KiB 建索引，再调用 Linux `FIDEDUPERANGE`，由内核确认字节相同后共享物理块；操作前后校验整文件 SHA-256。处理对象是 ext4 磁盘文件，不是 VM RAM 页，不是 NUMA 共享池。

额外去重的证据位于 `/mnt/clawbox-data/artifacts/swe-readiness-20261021/block-sharing-20261022/`，范围清单为相邻 `environment-catalog-20261022/sharing-scope.json`。核对到 32 份完成回执，覆盖 316 个不同 ext4 文件名，在缓存/Master 两处共 632 个文件路径，32 份均记录前后哈希一致。这些是操作历史，**不等于当前 503 个任务全部共享，也不等于所有相同块都已找到并共享**。

这些 316 个缓存 ext4 的逻辑大小为 5.25–7.25 GiB，中位 5.5 GiB，合计约 1.77 TiB；不能将逻辑大小或逐文件 `st_blocks`/`du` 相加当作独占物理空间，共享块可能重复计数。批次期间磁盘空闲量还受其他写入/清理影响，不能把回执的空闲增量直接当精确净节省量。

额外去重采用磁盘压力触发、最多 40 个文件一批的临时处理；“哪些块可以共享”按内容相同判定，“哪些模板被处理”受当时完成时间和批次选择影响。因此它不是全量统一的镜像准备规则。用户要求优先完成环境后已停止；核查发现旧 sudo 监督进程处于 stopped、Python 子进程 defunct，并非仍在扫描去重。保留已完成的共享；本次仅记录，不恢复处理，也不解除共享。

### 性能实验的限制和下一步

- **内容哈希一致只能证明内容不变，不能证明性能不变。** 共享块的写入涉及 CoW 分配和映射/引用计数更新；4 KiB 去重可能改变区段数量与布局，进而影响冷读、磁盘克隆和写入。方向和幅度尚未测量，不能把当前启动慢直接归因于去重。
- 当前功能/并发验收可以证明所记录条件下环境能否运行；它不建立“与完全不共享一样快”的结论。部分模板去重、部分没有，是需要记录和控制的存储实验条件；不应直接把不同 trace 的耗时差归因于调度策略。
- 正式策略性能对照应冻结相同模板集合、trace/会话分配、磁盘创建方式和共享状态，并控制缓存/运行顺序。若需量化额外去重影响，应选同一任务做独立、同配置、可重复的共享/不共享对照；区分“无跨模板去重但保留 Cube CoW”和“每台 VM 全物理复制”两个基线。
- **未执行的性能验证**：上述共享/不共享 A/B；当前没有完成的实测结果或既有验收命令，不得引用环境审计或 1280 首轮运行代替。记录缺口，不自动展开新实验或批量重写模板。
- 当前并发运行使用 OS-default、CPU 0–319 / NUMA0–3、1800 GiB VM 上限；NUMA2 从开始即可直接分配内存，未启用 LOCAL→共享池借用。磁盘块共享与此无关。

以下接续节的并发数字是带时间戳的历史快照。本次更新只核对和记录环境/存储情况，不恢复已暂停 goal、不释放屏障、不改冻结实验配置；继续运行前应读取远端最新状态。

## 当前接续点：2026-10-22 20:14 中国时间（12:14 UTC）

**环境准备已完成；1280 并发验收仍在后台运行，尚未通过，goal 未完成。** 用户要求停止当前 session 的等待，换 session 接续；不要重新构建环境、重新注册模板或另起重复实验。本节覆盖下文较早的运行状态；下文“全部 VM 已清理”只指旧实验，不适用于当前运行。

### 已完成及当前实测

- 503/503 原生 ARM 镜像、collector、Cube 模板、VM 环境检查和新一轮一致性审计均通过。保留官方 Benchmark 镜像内的 `/testbed`，没有通过 clone/reset/checkout 重建任务源码。
- 审计核对原镜像源码哈希、Git HEAD、原有工作区内容、Python/Conda/包版本、可移植系统包、root 用户、`/testbed` 工作目录及 trace 来源。ARM 二进制和架构相关系统包差异单独记录；503 个接受环境的 Python 包版本核对均通过。录制时未保存原镜像 digest，因此不能声称与历史镜像逐字节一致。
- 当前运行的全量资格验证 `qualification-d7a3f7fb61d8` 已成功：503 个任务全部完成原始 trace 首轮原生工具执行，503 个独立写入标记，零新增失败，1006 台 VM 已清理。原 trace 中已有的 4 个工具失败按调用身份和结果匹配，不算新失败。
- **12:14 UTC 正式运行快照：878 对完整 Runtime/Tool 环境同时存活，1760 台 VM（另有正在创建的不完整任务对），878 个环境检查通过，833 个任务完成首轮并到达屏障，零新增失败。** 数字持续变化，必须重新读现场。覆盖全部 503 种环境；1280 是会话数，503 种 trace 按 round-robin 分配，每个会话独立写入。
- 当时 VM cgroup 实际占用约 1293 GiB，OOM/oom_kill 为 0，当前 memory PSI 为 0。这不是客体配置内存之和，也不是全部应用 RSS，包含文件缓存。
- **当前是 OS-default：CPU 0–319、NUMA 0–3 从启动时全部可用，VM 总上限 1800 GiB。没有启用 LOCAL→共享池借用，超过 1 TiB 不触发切换。** NUMA2 已直接分配内存；此实验不能证明共享池借用或 NUMA 绑定策略有效。
- 启动较慢：同时创建任务对限制为 8；最近每台 VM 创建约 9–10 秒、一对约 20 秒。VM 环境检查后到原生 SSH 工具通道就绪，中位约 113 秒；首轮执行与采集中位约 6 秒。不要在当前冻结实验中修改配置来加速。
- 原定 18:27:44–19:27:44 中国时间的一小时窗口已超时，不能报告按时完成。

### 当前运行与证据位置（kunpeng）

```text
G=/data/clawbox-acceptance-20261022T102744Z
ID=swe-all503-c1280-barrier-20261022t114333z
RUN=$G/runs/$ID
正式 arm=28bc3a829c0a2936b52c49c1
正式 attempt=attempt-dd818512e83a459f
VM ownership task_uid=clawbox-8dbd7650de789c5dd474
```

- 权威当前指针：`$G/current-run.json`。不要使用旧的 `/home/weitianc/clawbox-1280-current.json`；旧 `goal-progress.json` 中“已全部清理”也尚未反映本次后台运行。
- 冻结输入/代码：`$RUN/experiment.yaml`、`workload.json`、`traces/`、`source/`。保留每个 run ID 和旧失败证据，不覆盖、不重用。
- 状态：`$RUN/results/$ID/run-state.json`、`launch.log`、`monitor-latest.json`、`monitor-history.jsonl`；事件位于 `results/$ID/attempts/*/*/events/*.jsonl`。
- 环境审计：`$G/fidelity/summary.json` 和 `fidelity/cases/`。环境目录：`/mnt/clawbox-data/artifacts/swe-readiness-20261021/environment-catalog-20261022/latest.json`（503/503）。准备状态在 `/home/weitianc/clawbox-swe-images-20261021/build/`；注册 worker 已停止。
- 主机已应用配置和原配置备份：`$G/full-host/`；保存的 `~/.config/clawbox/host.yaml`、`host.json` 已同步为当前四 NUMA 配置。系统盘约 5.82 TiB，新增准备文件已路由到扩容后的系统盘；不要再搬盘或清理数据。
- 后台进程在交接时仍活着：supervisor 1602900、worker 1723019、屏障服务 1598124、每 30 秒记录状态的 watcher 1738823。跨 session 先核对进程身份，不能只凭旧 PID 操作。

### 新 session 立即执行

可靠 SSH 参数（保留 host-key 验证）：

```powershell
ssh -T -o BatchMode=yes -o IdentitiesOnly=yes -i C:\Users\29068\.ssh\id_ed25519 -o IPQoS=none -o ConnectTimeout=15 -o KexAlgorithms=diffie-hellman-group-exchange-sha256 -c aes128-ctr kunpeng
```

在 kunpeng 上读取现场，不会启动新任务：

```bash
python3 /home/weitianc/.codex-monitor-acceptance.py
python3 /home/weitianc/.codex-brief-barrier.py
```

当前首轮执行完成的任务保持 VM 存活，在认证 HTTP 屏障等待；**不会自动释放，也不会因聊天 session 结束而停止后台运行。** 屏障描述符 `$G/current-barrier.json` 含测试认证 key，不要打印或复制到文档。服务端口 14207；实际位置以描述符为准。

**必须在约 2026-10-22 21:55:47 中国时间（13:55:47 UTC）之前接续检查并在满足条件后释放。** 最早任务的屏障等待上限是 7200 秒，超过会失败；不要修改冻结脚本延长时间或把失败认作通过。

当现场显示 1280 到达、零失败后，运行下面的验收并释放命令。它会先重新核对 1280 个环境标记、1280 份有效原生执行/telemetry、全部 503 种任务映射，以及 SDK 当前列出的 2560 台 running VM 与事件身份完全一致；任一条件不满足就失败，不释放：

```bash
sudo -n /home/weitianc/clawbox-swe-images-20261020/.venv/bin/python /home/weitianc/.codex-release-concurrency-barrier.py
```

- 只读核对脚本为 `/home/weitianc/.codex-verify-concurrency-barrier.py`。成功生成 `$RUN/concurrency-barrier-receipt-*.json`、`verified-barrier-receipt.txt`；释放后保存 `barrier-release-response.json` 和屏障目录的 `released.json`。
- **上述完整验收/释放尚未执行**：交接时未达到 1280。脚本语法、冻结 workload 解析及 arm 身份已检查；不能把脚本存在当作验收通过。
- 释放后继续等待 CLI 正常收尾，运行 `python3 /home/weitianc/.codex-summarize-concurrency-evidence.py`；要求 `concurrency_observed`、`all_first_rounds_observed`、`completed_normally`、`cleanup_verified` 均为 true，1280 个有效 `session_complete`，零失败，2560 台 VM 已销毁。再以 SDK ownership inventory 核对零残留；确认后停止本次 watcher 和屏障服务，保留证据，更新计划并完成 goal。
- 本次验收仅执行原始 trace 首轮模型返回的真实工具调用，不是完整长 trace replay，也不是 LOCAL/shared-pool 策略性能实验。

若出现失败/超时：先保留异常及 `openclaw/*/sidecar-startup.log`，使用支持的 CLI 中止并验证清理，再考虑新 run ID；不要直接启动重复运行：

```bash
RUN=/data/clawbox-acceptance-20261022T102744Z/runs/swe-all503-c1280-barrier-20261022t114333z
bash "$RUN/source/scripts/clawbox" --sudo --output-root "$RUN/results" experiment abort swe-all503-c1280-barrier-20261022t114333z
```

### 本轮修复与边界

- 本地 ClawBox `clawbox/experiments/openclaw_driver.py` 增加可配置 Runtime 就绪等待 `runtime_startup_timeout_seconds`（默认 60，本次 300）和失败日志留存；相关 30 项测试在 Windows、kunpeng Linux 均通过。Linux 首次缺 `CLAWTUNE_SOURCE_DIR` 的调用失败，加载保存的 machine.env 后重跑通过；日志均保留。文档 `docs/guide.md`、测试和当前计划已修改，尚未 commit。不要覆盖其他既有工作区修改。
- 较早 NUMA-bound 运行在 236 对时受 `cpuset_post_attach` 内核 cgroup 迁移阻塞；另一轮只使用 CPU 0–159、1000 GiB 的 OS-default 资格验证在 465 对附近出现启动/命令超时。两轮都已清理，但没有把旧失败改为成功。
- 当前修复/配置组合通过全 503 资格验证，尚不能在交接时宣称 1280 已支持。后续启动速度优化、NUMA 绑定和共享池机制验证属于独立后续工作；先完成当前验收。

## 原有约束与历史证据

Prepare **500+ distinct, validated ARM64 SWE-ReBench trace environments** for high-density CubeSandbox experiments. Concurrency is independently configured: assign each VM pair `pair_index % valid_trace_count`, with isolated writable environments.

- Recover prepared `/testbed` filesystems from official benchmark images; do not reconstruct repositories through cloning/resetting.
- Preserve recorded source and dependency versions when adapting to ARM64. Share identical base/dependency layers. Skip genuinely unportable cases only while retaining 500+ valid traces. Cross-architecture equivalence requires validation; it is not bit-identical reproduction.
- Treat each Benchmark image's recovered `/testbed`, Git HEAD, dirty state, package inventory, and immutable source-image manifest as authoritative. Do not use `git clone`, `git checkout`, or `git reset` to recreate task source.
- Reuse layers at three levels: architecture/base-system variant, identical dependency environment, and the small task-specific source/install delta. Docker reuses OCI layers, but the current Cube builder materializes a separate ext4 rootfs for each task template; do not assume physical Cube deduplication without measured evidence.
- Cleanup is authorized only inside `/home/weitianc/` (user update on 2026-10-22). Protect source datasets, experiment evidence, active environments, and configuration; verify symlink and mount targets before any cleanup. Write new large artifacts and outputs under `~/clawbox-storage` as needed. Do not clean other directories.
- The user separately authorized byte-preserving block sharing of this batch's Cube templates outside `/home/weitianc/`. This does not authorize deleting files outside the home directory.

## Background work

- Host: `weitianc@193.124.7.2` (Kunpeng).
- Run directory: `/home/weitianc/clawbox-swe-images-20261021`.
- Additional storage: `/home/weitianc/clawbox-storage` -> `/mnt/clawbox-data` (independent XFS filesystem).
- Root expansion is complete (2026-10-22): the explicitly authorized disk with serial `034XETD9Q8004022` (`nvme2n1`) now joins `nvme10n1p3` in `vg_sda/lv_root`. XFS grew online to about 5.82 TiB, with about 3.39 TiB free at completion. Preserve `root-expansion-20261022T100515Z/receipt.json` and the before/after LVM backups under the readiness artifact directory. The independent ClawBox data filesystem did not grow. Future cache and Master artifacts now use root-backed bind mounts from `/data/clawbox-preparation-storage`; existing artifacts remain accessible in place. Routing receipt: `root-preparation-routing-20261022T101205Z/receipt.json`. Both running services see the new mounts, and fstab verification passed without restarting services.
- Accepted environment set: 503 final ARM64 images and 503 collector-verified images. The collector stage is complete.
- At the 2026-10-22 07:47 UTC catalog export, 219 task templates are registered and 216 have VM-smoke receipts. All 503 have collector-verified ARM64 image receipts. Read live counts and the usable workload path from `/mnt/clawbox-data/artifacts/swe-readiness-20261021/environment-catalog-20261022/latest.json`.
- Monitor: `build/collector-state.json`, `build/cube-collector-state.json`, `logs/`, and `HANDOFF.txt` in the run directory.
- Count image readiness, cached Cube templates, and replay/telemetry acceptance separately. Build and VM-smoke counts are not successful replay counts.
- Direct registry mirrors and a checksum-verified package cache are primary; desktop VPN is fallback. Free-space guards protect both disks; any deletion must stay within the authorized home-directory cleanup scope.

## Remaining work

1. Preserve the accepted set of 503 eligible environments passing source, dependency, ARM runtime, and collector checks. Investigate failures only when doing so preserves the original Benchmark semantics; do not weaken the acceptance checks to raise the count.
2. Use the catalog export's `workload-ready.json`, which binds each included trace to its immutable image digest, Cube template, and original `/testbed` identity. The existing workload parser accepts all exported cases. Keep the accepted set at 503 and distinguish a partial usable workload from the final complete workload.
3. All 503 environments are Cube-VM verified and passed fresh fidelity checks. The full 503-case native first-round qualification succeeded with verified cleanup. Actual 1280 concurrency remains unverified: NUMA-bound startup stalled at 236 live Runtime/Tool pairs; an OS-default qualification reached 465 live pairs but produced VM startup and command failures. Both runs are stopped and cleanup is verified. Do not rebuild the 503 accepted environments or claim 1280 support from these results.
4. Three different task VMs passed concurrent startup, ARM64 and `/testbed` identity checks, and isolated writes; all three were destroyed afterward. Preserve the receipt under `concurrent-environment-smoke-20261022T064209Z`. This does not establish 500 simultaneous VM capacity.

The ARM64 image and Cube environment-preparation stages are complete: 503/503 immutable images, collectors, VM checks, and fresh fidelity audits passed. The full 503-case qualification passed; actual 1280-task concurrency acceptance remains incomplete.

## Current acceptance gate

- The user explicitly authorized deleting five named historical directories outside the home directory; that cleanup is complete and freed 134.8 GiB on `/`. Preserve `authorized-five-directory-cleanup-20261022T094022Z.json`. This is not blanket authorization for further deletion outside `/home/weitianc/`.
- A second narrow cleanup exception is complete: 22 `/var/log/messages-*` archives dated before 2026-10-22 were removed, with 50.16 GiB allocated and a 48.42 GiB observed increase in root free space during ongoing preparation. Current `messages`, today's archive, and `boot.log` were retained. Preserve `old-messages-cleanup-1792662885742574056.json` in the readiness artifact directory; no other system-log deletion is authorized.
- The actual VM admission block was `template_locality` enforcing the snapshot storage mode at its hard-coded 85% rejection threshold. Cleanup brought root usage to 80.86%; `GET /cube/snapshot/storage?refresh=true` confirmed writable `warn` mode. No Master threshold change was made.

- After the clean reboot, Cube reports `Healthy=true` and `ReportedReady=true`; host apply passed KVM, cgroup, service, memory, CPU, and VM create/execute/destroy checks. Preserve the host-apply receipt under `/mnt/clawbox-data/artifacts/swe-readiness-20261021/reboot-clean-network-20261022`.
- Cubelet still uses `~/clawbox-storage/cubelet-cache/cubebox_os_image`, now a bind mount of root-backed `/data/clawbox-preparation-storage/cubelet-cache`. Master storage similarly uses `/data/clawbox-preparation-storage/master`. All 497 existing cache and 462 Master entries retained their device/inode identities through links; new artifacts and VM checks passed after routing. Existing files were not copied or deleted.
- Registration now requires cache and Master storage to reside on the expanded root filesystem and reserves `32 + 16 * workers` GiB there (160 GiB for eight workers). The independent data disk retains legacy artifacts and logs/state, with a 32 GiB reserve. The old 84.8% registration cutoff was removed. Do not equate that script cutoff with a verified CubeMaster rejection cause; inspect live admission evidence.
- The pressure-triggered sharing watcher is stopped. Future reuse must have a stable artifact/content rule, independent of disk occupancy; disk thresholds control admission, not content identity.
- The completed 40-file batches recovered approximately 146.85 GiB on the system disk and 139.62 GiB on additional storage, with unchanged complete-file hashes. The sharing watcher is now stopped; the user explicitly deferred sharing work. Receipts are under `/mnt/clawbox-data/artifacts/swe-readiness-20261021/block-sharing-20261022`.
- `/home/weitianc/` is currently a bind mount on the additional disk. Cleaning 35 verified old QEMU ELF core dumps there freed approximately 94.17 GiB on that disk, not on `/`. Preserve `build/home-core-cleanup-applied-20261022.json`; further home cleanup cannot solve system-disk pressure.
- Preserve and finish cleanup of `swe-multitrace-c1280-20261022t075939z` under `/mnt/clawbox-data/experiments/`. Its 216-case qualification passed, but the formal run reached 71 session failures by 08:18 UTC, including missing vCPU threads after creation, command HTTP 404, and TCP endpoint failures. Abort cleanup is now verified and preparation has resumed. Investigate the VM failures before the next high-concurrency run. Cubelet logged 83 create failures with context canceled during 08:10–08:19 UTC; the parent VM cgroup reported zero OOM events. A demonstrated defect is the 30-second CubeAPI/SDK creation deadline combined with ClawBox accepting a listed VM before guest readiness. CubeAPI now places creation on its 120-second lifecycle route; ClawBox uses a configurable 130-second SDK deadline and requires a guest command during timeout recovery. All 142 API tests and 140 Linux ClawBox tests passed. The API imageInfo regression was fixed and the replacement passed an isolated real VM smoke before deployment; see deployment-metadata-fixed.json. Deployment and four repaired-template receipts are under /mnt/clawbox-data/artifacts/swe-readiness-20261021/startup-timeout-fix-20261022. Use that directory's clawbox-source (or an equivalent fresh export of the fixed worktree) for the next unique run; preserve old runs. The full 503-case/1280 acceptance remains outstanding. Configured capacity is not proof of working 1280 concurrency. `launch-state.json`, `launch.log`, and `results/` are authoritative; `/home/weitianc/clawbox-1280-current.json` points to the evidence.
- Preserve the prior `074837z` qualification, which rejected three Tool errors already present in the original traces. The applied fix compares call identity, command, exit code, and error against recorded evidence; unknown errors still fail. All 216 cases remain in the workload. No Benchmark source or image was changed for this fix.
- Preserve the Benchmark image's actual user-space ABI per shared base family. Debian 12 is observed in the dominant families but is not a ClawBox requirement; the Kunpeng host is openEuler and does not define guest user space.
- Keep the verified storage bind mounts and receipts under `/mnt/clawbox-data/artifacts/swe-readiness-20261021`. Do not move `/data/cubelet`, resume the paused Docker copy, prune images, or delete files outside `/home/weitianc/`.

## Deferred improvements

These do not take priority over completing the 500+ environment corpus.

- Make preparation status distinguish frozen/stopped workers from running workers; systemd `active` and a surviving PID do not establish progress. Report stage freshness and derive completion from acceptance receipts rather than a hard-coded flag.
- Consolidate the validated batch-preparation and manifest-export steps into the existing maintained image/experiment commands. Reuse existing adaptation code; retain separate source, environment, VM-smoke, and full-replay results.
- Improve template polling diagnostics to preserve the underlying API error and reconcile existing template IDs before retrying. A temporarily unavailable control plane must not cause duplicate template submission.
- Document how new preparation and VM-storage writes use the additional disk, including restart/mount prerequisites. An added filesystem alone does not move Docker or Cube storage.

## Active one-hour goal

- Requested window: 2026-10-22 18:27:44?19:27:44 Asia/Shanghai. Goal remains incomplete because 1280 concurrent task success is unverified.
- Completed: all 503 immutable ARM images, Cube templates, fresh source/Python/Conda/portable-OS fidelity audits, and all 503 native first-round sessions passed. Preserve original `/testbed`; no clone/reset/checkout is required. ARM differences are recorded. Recording-time image digests were not saved, so historical byte identity cannot be asserted.
- Evidence root: `/data/clawbox-acceptance-20261022T102744Z`; see `goal-progress.json`, `fidelity/summary.json`, `concurrency-blockers.json`, and immutable `runs/`. All acceptance VMs are cleaned up; the Cube VM cgroup reports `populated 0`.
- Required next: resolve high-density Cube startup before another full-set 1280 acceptance. NUMA binding blocks in `cpuset_post_attach -> __flush_workqueue`; competing cgroup writes and readers stall. Shared mapping migration is a hypothesis requiring verification. OS-default avoids that binding path but still showed guest RCU/timer stalls, a demonstrated 3-second guest-time-reset RPC timeout, envd initialization failures, and command deadlines. It is not a successful workaround.
- Validate the startup fix under sustained resident density, then use a new frozen experiment/run ID with all 503 cases through `clawbox experiment launch`. Require 1280 simultaneously live, environment-verified Runtime/Tool pairs, successful original tools, unique writable markers, and verified cleanup. This acceptance does not require complete long-trace replay.
- Earlier profile limitation: this acceptance used CPUs 0?159 and a 1000 GiB VM cgroup on a 320-CPU, approximately 2 TiB host. Configured Cube quota is distinct from physical or observed capacity. Do not count additional configured quota as a repair.

- Current continuation: the supported host apply now enables all 320 CPUs and 1800 GiB across NUMA nodes 0?3; about 200 GiB remains outside the VM limit. Canonical host YAML/JSON match the applied profile; backups and receipt are in `full-host/` under the goal evidence root. This is capacity configuration, not acceptance proof.
- Runtime sidecar readiness is now configurable with `inference.configuration.runtime_startup_timeout_seconds`; startup failures preserve sidecar diagnostics without masking the original error. All 30 driver tests passed on Windows and kunpeng Linux (Linux requires the saved machine environment for the ClawTune source path).
- Active run: `swe-all503-c1280-barrier-20261022t114333z`, all 503 cases, OS-default placement, 8 concurrent pair creations, 300-second Runtime readiness budget. Its formal validation waits at an authenticated barrier until all 1280 independently identified sessions have completed their native first round and are simultaneously alive. Inspect events and arrivals before releasing it. Keep the original 1280 requirement; the goal remains incomplete.
