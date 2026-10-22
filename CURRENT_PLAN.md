# Current Plan

Updated: 2026-10-22

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
