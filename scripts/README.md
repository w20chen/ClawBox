# scripts/ 索引

> 所有脚本按用途分类。**A 组被 Dockerfile COPY 进镜像，位置不能动**；其余分组仅作索引（不强制移动，避免破坏 `dirname $BASH_SOURCE/..` 相对路径和交接文档里的引用）。

## A. 生产运行时（被 `docker/Dockerfile.runtime` COPY，勿移动）
| 脚本 | 用途 |
|---|---|
| `runtime-entrypoint.sh` | Runtime 容器入口：起 ClawTune sidecar、KB 拉取、result/observation 回刷、最终上传 |
| `clawtune-sidecar-entrypoint.sh` | 起 ClawTune sidecar 后台进程（observe-only + hook-only） |
| `artifact-uploader.py` | 增量上传 trace/artifact 到 ingester（HMAC token、幂等） |
| `kb-flush.py` | runtime 侧把 span+bridge join 后的 observation 签名 POST 到控制面 KB（fail-open） |

## B. 构建（镜像 / 主机装配 / 校验）
| 脚本 | 用途 |
|---|---|
| `build-kubernetes-images.sh` | 构建控制面/运行时/tool-bridge 镜像（`CLAWTUNE_ROOT` 必须指向 ClawTune checkout） |
| `build-kata-firecracker-arm64.sh` | 构建 Kata+Firecracker 主机装配（plan/apply/status） |
| `build-swe-rebench-arm64.py` | ARM64 SWE-ReBench 任务镜像工厂（入口，实现在 `clawbox/images/arm64.py`） |
| `validate_clawtune_integration.py` | 校验 ClawTune checkout 布局/插件/sidecar 契约（构建前置门） |
| `rebuild-control-plane-image.sh` | 后台重建+推送控制面镜像（真机用） |
| `rebuild-runtime-image.sh` | 重建+推送 runtime 镜像（真机用） |
| `rebuild-swe-rebench-tool-overlay.sh` | 把新 tool-bridge 二进制打进任务镜像 overlay（真机用） |
| `remote-rebuild-all.sh` / `remote-rebuild-all-mirror.sh` | 真机全量重建助手（含镜像源切换） |

## C. 主机引导 / 基础设施（Kunpeng 单机，需 root/sudo）
| 脚本 | 用途 |
|---|---|
| `bootstrap-openeuler-arm64.sh` | openEuler 主机 bootstrap：K8s/containerd/Kata/两块盘 devmapper（plan/apply/status） |
| `audit-kata-firecracker-arm64.sh` | FC-0 审计门：ARM64 ELF/版本/配置/内核/block-rootfs/shim 门禁 |
| `setup-devmapper-openeuler-arm64.sh` | 生产 LVM thin pool 初始化（显式确认双盘） |
| `linux-deploy.sh` | 通用 Linux 部署助手 |
| `install-shim-nofile-wrapper.sh` | P3：kata shim RLIMIT_NOFILE 提升包装（需 sudo，未在真机执行） |
| `install-clawbox-devmapper-udev-rule.sh` | P0 修复：devmapper 快照 udev 规则（防探测卡死） |
| `sync-target-repo.sh` | 同步 ClawBox 仓库到目标机 |

## D. 主机验证 / 冒烟 / 探测
| 脚本 | 用途 |
|---|---|
| `arm64-kata-smoke.sh` | FC-3/FC-4 实况门：双 kata-fc VM + runc 攻击者隔离验证 |
| `collect-node-capacity.py` | 节点容量/预留计算（产出 ConfigMap） |
| `probe-kata-guest.sh` | guest 可行性探测：cgroup v2 读写、BPF/BTF/内核头、caps（支持 `--no-caps`） |

## E. 跑任务 / 基准 / 规模
| 脚本 | 用途 |
|---|---|
| `run-swe-rebench.sh` | 通过 M1 API 提交 SWE-ReBench 数据集 |
| `scale-swe-rebench.sh` | 负载阶梯 1/2/4/8/16/32（thin-pool 停车门） |
| `single-image-scale.sh` | M0 旧路径并发压测（直接写 CR） |
| `collect-run-evidence.sh` | 收集 run 证据（CR/result/trace） |
| `evidence-manifest.py` | 生成证据清单（release manifest） |

## F. M1 managed 真机操作（smoke 栈：`clawbox-m1-api` + `clawbox-m1-dispatcher`）
| 脚本 | 用途 |
|---|---|
| `m1-concurrent.sh` | 提交 N 个并发真实任务（改 `N=`）并盯到终态 |
| `m1-conc-status.sh` / `m1-conc-agents.sh` / `m1-conc-finalize.sh` / `m1-conc-final.sh` / `m1-conc-patch.sh` | 并发 run 的 CR/agent/finalize/patch 状态 |
| `m1-status.sh` | M1 栈状态（容器/healthz/dispatcher 日志） |
| `m1-live-smoke.sh` / `m1-fix2.sh` / `m1-fix-run.sh` | 重建/修复 smoke 栈容器 |
| `m1-realtask.sh` / `m1-realtask-watch.sh` / `m1-kb-submit.sh` | 提交+盯真实任务（KB 路径） |
| `m1-p0-verify.sh` / `m1-p0-joincheck.sh` | P0 验收：execution_id join rate / DeepSeek spans / patch_status |
| `m1-realpatch.sh` / `m1-sqlcheck.sh` / `m1-extract-patch.sh` | 从 tool VM 提取 patch / 检查 sql.py / git status |
| `m1-evidence.sh` / `m1-final-evidence.sh` | 证据收集 |
| `m1-multitenant.sh` | **多租户模拟提交**：K 租户 × N 任务（见 `clawbox/benchmark/multitenant.py`） |

## G. 远程诊断 / 清理（真机）
| 脚本 | 用途 |
|---|---|
| `remote-disk-clean.sh` | 磁盘清理（docker prune / 删 swerebench x86_64 镜像） |
| `remote-m1-recreate.sh` | 重建 M1 smoke 栈容器 |
| `remote-p0-preflight.sh` | P0 前置检查 |
| `remote-p0-live-capture.sh` | 实况捕获 |
| `remote-p0-remove-stale-snapshot.sh` | 清理 devmapper 陈旧快照（免密 dmsetup） |
| `remote-p0-restart-containerd.sh` | 重启 containerd（免密 systemctl） |

## H. 诊断 / 排查
| 脚本 | 用途 |
|---|---|
| `check-agent-log.sh` / `check-prefix.sh` | agent 工具行为统计 / 前缀检查 |
| `diag-fd.sh` / `diag-ingester.sh` | 文件描述符诊断 / ingester 诊断 |
| `dump-answer.sh` / `dump-result.sh` | 从 ingester 读 answer/result |
| `kb-live-status.py` | KB 实况状态 |
| `ssh-255-probe.sh` / `ssh-255-probe-inpod.sh` | SSH EOF（255）瞬态探测 |
| `oclaw-find-patch.sh` / `oclaw-patch-verify.mjs` | OpenClaw patch 定位/校验 |
| `verify-managed-image.sh` / `verify-read-fix.sh` / `verify-runtime-patch.sh` | 镜像/补丁验证 |

## I. Legacy / 旧路径 / 命名混乱（勿在新工作中引用）
| 脚本 | 说明 |
|---|---|
| `e2e.sh` / `e2e.py` | 旧 scheduler/allocator/controller 控制面 e2e（`CONTROLLER_BACKEND=subprocess`）；非 M1/M0 生产路径 |
| `test-multitenancy.sh` / `test-multitenancy.ps1` | **名字误导**：实际只跑 `test_phase2_kb.py + test_phase3_chain.py`，与"多租户"无关；多租户真入口是 `m1-multitenant.sh` |

## J. 数据 / 辅助
| 文件 | 说明 |
|---|---|
| `problem-scim2-13.txt` | 测试用问题文本（`m1-concurrent.sh` 引用） |

> **约定**：A 组被镜像引用；真机操作脚本通常通过 `scp` 到 `/tmp/` 或 `~/ClawBox/scripts/` 运行，很多假设已 ssh 到目标机。新脚本请先归到上述某类，不要继续平铺。
