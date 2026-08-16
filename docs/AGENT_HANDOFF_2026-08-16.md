# ClawBox Firecracker-first 技术交接报告

更新时间：2026-08-16（Asia/Shanghai）  
交接基线：`main` / `526bb28`（`network issue`）  
目标主机：openEuler 24.03 LTS-SP1、Kunpeng/aarch64、单节点 Kubernetes、Kata + Firecracker  
报告目的：让接手 agent 不依赖历史对话，也能继续完成真机安装、修复剩余缺陷并执行最终验收。

## 0. 最重要的结论

这个仓库已经完成 Firecracker-first 的主要代码骨架和本地测试，但**尚未完成目标 Kunpeng 主机上的真实 Firecracker 验收**。不能把“27 个本地测试通过”等同于项目最终完成。

当前真实进度停在 bootstrap 下载 containerd 阶段：目标机访问 GitHub 很慢，28.3 MiB 的 containerd 包速度约 15 KiB/s，预计约 32 分钟；用户在下载到 12% 时按了 `Ctrl+C`。当时尚未执行 devmapper 磁盘初始化。当前脚本的临时下载会随退出被删除，所以这 12% 不能复用。

接手后的最高优先级是：

1. 为 bootstrap 和 Kata/Firecracker 构建脚本实现安全的持久化下载缓存与断点续传；
2. 修复 devmapper 已创建后 bootstrap 无法安全重跑的幂等性问题；
3. 在目标机继续 bootstrap，逐一通过 FC-0 至 FC-5；
4. 再构建 ARM64 控制面、Runtime、Tool Bridge 和 128 个 SWE-ReBench 镜像；
5. 最后部署 Cell 控制器、ingester，跑单任务和 1/2/4/8/16/32 阶梯压测。

## 1. 产品目标与不可改变的边界

ClawBox 的目标是在一台约 320 逻辑核的 Kunpeng ARM64 主机上，为每个 SWE-ReBench 任务创建一个隔离 Cell。每个 Cell 固定包含两个独立的 Firecracker microVM：

```text
SandboxTask CR
  ├─ Tool Pod / Kata-Firecracker VM
  │    ├─ 原生 linux/arm64 SWE-ReBench 任务镜像
  │    └─ 静态 ARM64 Tool Bridge（SSH/2222）
  └─ Runtime Job/Pod / Kata-Firecracker VM
       ├─ OpenClaw Runtime
       └─ ClawTune 原生 Kubernetes sidecar（observe-only）
```

控制面流程：

```text
任务清单 + ARM64 映射
        ↓
Benchmark Launcher
        ↓
SandboxTask CRD
        ↓
Cell Controller ── AtomicAdmission / FixedProfileSizer
        ├─ Secret / ConfigMap / Service / NetworkPolicy
        ├─ 先创建并等待 Tool Pod Ready
        ├─ 再创建 Runtime Job
        └─ 清理所有子资源
        ↓
Trace/Result Ingester（网络上传，不使用共享盘）
```

硬边界：

- 宿主机、guest、Tool Bridge、控制面镜像和 SWE-ReBench 镜像全部必须是原生 ARM64。
- 唯一生产 RuntimeClass/handler 是 `kata-fc-arm64`。
- 禁止 QEMU、TCG、binfmt、qemu-user 或其他跨架构 fallback。
- Kata 固定为 `3.31.0`，Firecracker 固定为 `1.12.1`；Kata 至少 3.31.0 是安全门槛。
- containerd 目标为 `2.3.4`，runc 为 `1.5.1`，Kubernetes 为 `1.35.x`，Calico 为 `3.32.1`。
- Firecracker 根文件系统必须走 containerd devmapper block snapshot；生产环境禁止 loopback devmapper。
- Runtime Pod 与 Tool Pod 不共享 `hostPath`、PVC、RWX 或宿主文件系统。
- Tool Pod 不获得 LLM Secret、上传 token 或 SSH client private key。
- Runtime Pod 不获得 Tool host private key，只获得 client private key及固定 host public key。
- ClawTune 当前只能是 `observe-only` / `hook-only`，cgroup、affinity、NUMA 控制全部关闭。
- 资源不足时任务必须停留在 `Queued`，不能只创建半个 Cell。
- 不允许自动执行 `kubeadm reset`，不允许自动接管不属于 ClawBox 的 Kubernetes 集群。

## 2. 目标主机的已知事实

用户已在目标机确认：

- OS：openEuler 24.03 (LTS-SP1)
- 架构：aarch64
- Kernel：`6.6.0-72.0.0.76.oe2403sp1.aarch64`
- API advertise address：`193.124.7.2`
- Pod CIDR：目标值 `192.168.0.0/22`
- Service CIDR：`10.96.0.0/12`
- 当前发行版 containerd：`1.6.22.27.oe2403sp1`
- Kubernetes：尚未安装/初始化
- firewalld：inactive
- SELinux：Permissive
- active swap：0（系统盘仍有 swap LV，bootstrap 会持久禁用 swap）
- kubelet `maxPods` 目标：512
- system reserved：`cpu=4,memory=8Gi,ephemeral-storage=20Gi`
- kube reserved：同上

磁盘：共 11 块约 2.9 TiB NVMe。系统盘是 `/dev/nvme4n1`，其 `nvme4n1p3` 属于 `vg_sda`，承载 `/` 和 swap，绝对不能操作。`nvme5n1` 和 `nvme7n1` 有旧分区，也没有被选用。

已选择并人工审查：

- devmapper data device：`/dev/nvme0n1`，序列号 `034XETD9Q8005492`
- devmapper metadata device：`/dev/nvme1n1`，序列号 `034XETD9Q8004022`

对这两块盘的检查结果：

- 都是 2.91 TiB whole disk；
- GPT 表存在，但表内无分区；
- `blkid -p` 只看到 `PTTYPE=gpt`，没有文件系统；
- 不属于现有 LVM VG；
- `mdadm --examine` 只看到 GPT protective MBR（type `ee`），未发现 RAID superblock；
- `findmnt` 无结果；
- `/sys/class/block/*/holders` 为空；
- bootstrap storage `plan` 已通过。

用户已经明确选择这两块盘。最终破坏性授权字符串必须保持为：

```text
/dev/nvme0n1,/dev/nvme1n1
```

## 3. 真机 bootstrap 的真实历史和当前状态

最初目标机遗留了旧 QEMU bootstrap 的 `/var/lib/clawbox-bootstrap/versions.env`：

- `RUNTIME_CLASS=kata-qemu-runtime-rs`
- `POD_CIDR=192.168.0.0/16`
- 缄缺 `FIRECRACKER_VERSION`、`MAX_PODS`、`SYSTEM_RESERVED`、`KUBE_RESERVED`、`STATE_SCHEMA_VERSION`

但主机没有 `/etc/kubernetes/admin.conf`，也没有成功的 Firecracker stage-0。因此代码增加了受限迁移：只有旧状态无 schema、旧 runtime 恰为 `kata-qemu-runtime-rs`、目标恰为 `kata-fc-arm64`，并且无 `admin.conf`、无 `stage0-passed`，才允许整份未初始化状态迁移。已完成的主机仍严格拒绝迁移。

目标机运行当前脚本时已经看到：

```text
WARN: completing incomplete pre-stage0 state; missing fields: ...
WARN: migrating uninitialized QEMU state to Firecracker; changed fields: ...
```

这两个 WARN 是预期行为。旧状态会备份为：

```text
/var/lib/clawbox-bootstrap/backups/versions.env.before-schema-2
```

网络诊断：

- `getent ahosts github.com` 正常解析 IPv4；
- 普通用户 `curl` GitHub 曾超时；
- `sudo curl` GitHub 返回 HTTP 200；
- 无 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY`；
- GitHub、pkgs.k8s.io、raw.githubusercontent.com、ghcr.io、quay.io、registry.k8s.io 基础路由均有 HTTP 响应；
- 对 Calico 的具体 raw 文件曾单次超时，说明跨境/CDN链路不稳定；
- commit `526bb28` 已把 endpoint probe 从单次 HEAD 改成 range GET，并增加 `--retry 4 --retry-all-errors`；正式 bootstrap 下载也增加同样的全错误重试。

之后（2026-08-16 UTC 夜间）借助 `HTTPS_PROXY=http://127.0.0.1:1080` 的 apply 几乎走完了全部阶段，真实进度大幅前移：containerd 2.3.4/runc 1.5.1 安装完成、Kubernetes 1.35.7 单节点初始化完成、Calico 3.32.1（VXLAN + NetworkPolicy）就绪、devmapper thin-pool 创建成功（`clawbox/fc-pool`，数据盘 `/dev/nvme0n1` 90%PVS、元数据盘 `/dev/nvme1n1` 8G，容量 2682.73g）、`kata-fc-arm64` RuntimeClass/handler 生效。静态 gate 全绿（19 pass, 0 warn, 0 fail，含 FC-0 14/14）。**但 stage-0 在线冒烟 gate 失败**，`runtime`/`tool` 两个 Kata Pod 卡在 `ContainerCreating`：

```text
failed to create containerd task: failed to create shim task:
Others("failed to handle message try init runtime instance
  0: load config
  1: Firecracker hypervisor can not support 128 vCPUs")
```

根因（已修复并提交）：

- Kata runtime-rs 在 `configuration-fc-arm64.toml` 未显式设置 `default_vcpus`/`default_maxvcpus` 时，`CpuInfo::adjust_config()` 会把 `default_maxvcpus` 填成宿主机核数（本机 128），而 Firecracker 插件校验上限是 32（`MAX_FIRECRACKER_VCPUS`），配置加载即失败；
- 该错误发生在 shim `load config` 阶段，不经过 Pod 资源 sizing，因此与 Pod 的 100m CPU 请求无关；
- FC-0 审计原先不检查这两个键，带病 `/opt/kata` 树通过审计后被 bootstrap“复用”，冒烟持续失败；
- 修复：`build-kata-firecracker-arm64.sh` 现在把 `default_vcpus = 2`、`default_maxvcpus = 32` 写死进生成的配置（可用 `CLAWBOX_FC_DEFAULT_VCPUS`/`CLAWBOX_FC_MAX_VCPUS` 覆盖，限 [1,32]，且 `default_vcpus <= default_maxvcpus`）；`audit-kata-firecracker-arm64.sh`（FC-0）新增三项检查，带病树会 FAIL，从而触发 bootstrap 重新组装而非复用。

第二次 apply（拉取修复后）的进展与新的失败点：

- 旧 `/opt/kata` 被新 FC-0 检查正确判为带病（15 pass/2 fail：`default_maxvcpus = 0`、`default_vcpus=1 > 0`）→ 自动重建，新树 FC-0 **17/17 全绿**（`default_vcpus = 2`、`default_maxvcpus = 32`）；
- devmapper pool 重建成功（FC-2 ready），静态 gate **19/19 全绿**；
- **stage-0 在线冒烟再次失败**，错误已从 vCPU 校验变成 devmapper 快照：

```text
failed to create containerd container: failed to create snapshot
"clawbox-fc--pool-snap-493" (dev: 3) from "clawbox-fc--pool-snap-1" (dev: 1): no data available
```

根因（已修复并提交，见 P0-5）：apply 整盘重建 thin pool 时，containerd 的 devmapper 快照元数据（`/var/lib/containerd/io.containerd.snapshotter.v1.devmapper/`，在 `/var/lib/containerd` 下持久化）没有随 pool 一起清空，仍引用旧 pool 的设备 ID（alpine 基础快照 `snap-1` = dev 1）；新 pool 里没有该 origin 设备，内核 dm-thin 返回 `-ENODATA`。修复：`setup-devmapper-openeuler-arm64.sh apply` 在重建 pool、写完 drop-in/`containerd config dump` 之后、启动 containerd 之前清空该目录（content store 保留，镜像本地重新解包，无需重下载）。

目标机当前状态：

- 两块 NVMe 已被 ClawBox 拥有（PV/VG/pool 已建）；由于 P0-2 的 owned-pool 幂等重跑尚未实现，重跑前必须再次手动 `vgremove`/`pvremove`（见第 20 节）；
- Kubernetes/Calico 已初始化，`initialize_cluster`/`install_calico` 幂等；
- `/opt/kata` 已是修复后的 17/17 树（无需重建）；
- containerd devmapper 快照元数据仍指向旧 pool（当前带病）；按第 20 节快速路径清空后直接重跑冒烟，或拉取 P0-5 修复后走一次完整 apply 自愈；
- `clawbox.openai.com/firecracker-ready` 标签在冒烟失败后已被移除，`stage0-passed` 不存在。

## 4. bootstrap 机制详解

权威脚本：`scripts/bootstrap-openeuler-arm64.sh`。

### 4.1 模式

- `plan`：只读主机检查、网络/CIDR检查、磁盘检查、打印精确变更计划。
- `apply`：要求 sudo 和两块显式授权磁盘，执行完整安装。
- `status`：查看服务、版本、Kata audit、devmapper、节点、RuntimeClass 和 Pod。

### 4.2 前置检查

`common_preflight` 强制：Linux、openEuler、ARM64、`/dev/kvm` 字符设备、cgroup v2、所需命令、版本格式、唯一 RuntimeClass、至少 4 CPU/8 GiB RAM、`/var` 至少 30 GiB 空闲、CIDR 与现有路由不重叠。若已有 `admin.conf` 但没有 ClawBox owner marker，则拒绝接管。

`validate_storage_devices` 委托 `setup-devmapper-openeuler-arm64.sh plan`，现在会在任何宿主修改前拒绝占位符、分区、挂载盘、loop、系统盘、项目盘和带 holder 的盘。

### 4.3 状态与漂移

状态目录：`/var/lib/clawbox-bootstrap`，schema 当前为 2。`versions.env` 保存 Kubernetes/containerd/runc/Calico/Kata/Firecracker/RuntimeClass/CIDR/容量配置。一般情况下任何已记录值与请求值不同都会 fail closed。唯一特例是前述“未初始化旧 QEMU 状态 → Firecracker”。

`stage0-passed` 只有静态 host gate 和 live smoke 都成功后才创建。节点 ready label 也只有 gate 期间才添加，gate 失败会移除。

### 4.4 apply 顺序

实际顺序：

1. sudo/KVM/冲突 runtime 检查；
2. 只读验证两块磁盘；
3. 状态漂移/旧 QEMU 状态迁移检查；
4. 创建状态和备份目录；
5. 检查 GitHub、Kubernetes repo、Calico raw、GHCR、Quay、registry.k8s.io；
6. 写入目标状态；
7. 安装 RPM 前置依赖、chronyd、内核模块、sysctl、NetworkManager Calico 排除规则；禁用 firewalld/swap；SELinux 持久 permissive；
8. 下载并校验官方 containerd/runc ARM64 制品，安装 containerd 2.x 默认配置和 systemd unit；
9. 安装 kubelet/kubeadm/kubectl，配置 crictl；
10. 配置 kubelet maxPods/reserved/eviction；
11. 若无 `admin.conf` 则 kubeadm init；生成调用用户 kubeconfig；
12. 安装 Calico operator/CRD/Installation（VXLAN、BGP disabled），移除单节点 control-plane taint；
13. audit 或构建 Kata + Firecracker；
14. 对两块授权盘创建 LVM thin pool，并配置 containerd devmapper；
15. 再次 audit，确认 handler 和 plugin，创建 RuntimeClass；
16. 运行 `deploy/check-host.sh` 和 `scripts/arm64-kata-smoke.sh`；
17. 成功后创建 `stage0-passed` 并打印 status。

### 4.5 下载安全

- containerd 和 runc 均下载官方 archive 与官方 checksum，再执行 `sha256sum -c`。
- Kata 3.31.0 ARM64 archive 使用代码内固定 SHA256：`42a7e67a2c2bf3e97a615c99a293b2bc01ea9c84111fc2bf4abeedb7adc9c2ac`。
- 覆盖 Kata 版本时必须显式提供经审查的 `KATA_ARCHIVE_SHA256`。
- Firecracker archive 使用官方 sidecar checksum 或 `SHA256SUMS`。
- 不应改成不可信镜像并跳过校验。可以引入缓存或可信代理，但最终内容必须由上述官方/固定摘要验证。

## 5. FC-0 至 FC-5

### FC-0：`audit-kata-firecracker-arm64.sh`

只读审计 `/opt/kata`：Firecracker/jailer 必须是可执行 aarch64 ELF 且版本 1.12.1；Kata shim 必须是 aarch64 且版本匹配；配置必须明确选择 Firecracker；禁止 virtio-fs；要求 `static_sandbox_resource_mgmt=true`、`disable_guest_empty_dir=false`；kernel 必须是 ARM64；rootfs 必须是 block image，不能走 initrd；配置中的 Firecracker/jailer 路径必须指向刚审计的二进制。成功可写 `firecracker-audit.env`。

### FC-1：`build-kata-firecracker-arm64.sh`

只允许在 native ARM64 上运行。下载 Kata arm64 static archive 和 Firecracker aarch64 release；把 Firecracker/jailer 装入 stage tree；从 Kata release 自带 runtime-rs Firecracker config 生成 `/opt/kata/share/defaults/kata-containers/configuration-fc-arm64.toml`；规范化 path/jailer/static sizing/guest emptyDir；运行 FC-0。`install` 会先备份旧 `/opt/kata`，然后替换并创建 `/usr/local/bin/containerd-shim-kata-v2` symlink。

### FC-2：`setup-devmapper-openeuler-arm64.sh`

目标 VG/LV：`clawbox/fc-pool`。data LV 默认在 data PV 上取 `90%PVS`；metadata LV 默认在 metadata PV 上取 16 GiB；转换成 thin pool；配置 pool usage threshold 80%；生成 containerd v3 drop-in，handler 使用 `devmapper`，base image size 为 64 GB；systemd unit 在 containerd/kubelet 前激活 pool。

### FC-3/FC-4：`deploy/check-host.sh`

静态 fail-closed gate：openEuler/ARM64/KVM/cgroup v2；containerd 2.x；devmapper plugin `ok`；handler 存在且使用 devmapper 和审计配置；containerd 中不得残留 QEMU/cloud-hypervisor handler；Kata audit；RuntimeClass handler/overhead；节点必须 ARM64 且有 Firecracker-ready label；NetworkPolicy API 可用。

### FC-5：`scripts/arm64-kata-smoke.sh`

创建临时 namespace，启动 Tool、Runtime 两个 `kata-fc-arm64` Pod，另起非 Kata attacker Pod；检查两 VM 都是 ARM64、boot ID 不同、Runtime 可经 Service 访问 Tool、attacker 被 NetworkPolicy 阻断、没有 hostPath/PVC、宿主至少有两个 Firecracker 进程、没有 QEMU 进程、containerd journal 含 Firecracker 证据。删除 namespace 后，devmapper active snapshot 数必须回落到测试前基线。

## 6. containerd 与 devmapper 配置

`deploy/containerd-firecracker.toml` 是 containerd 2.x config v3 drop-in：

- runtime type：`io.containerd.kata.v2`
- runtime name：`kata-fc-arm64`
- snapshotter：`devmapper`
- ConfigPath：`/opt/kata/share/defaults/kata-containers/configuration-fc-arm64.toml`
- `privileged_without_host_devices=true`
- pool name 最终由脚本从 LVM dm_name 动态替换
- `discard_blocks=true`
- `discard_unpacked_layers=false`

`deploy/runtimeclass-firecracker.yaml` 的 name 与 handler 都为 `kata-fc-arm64`；固定 overhead 为 250m CPU/256Mi RAM；只调度到 `kubernetes.io/arch=arm64` 且 `clawbox.openai.com/firecracker-ready=true` 的节点。

## 7. SandboxTask 与 Cell Controller

CRD：`deploy/sandboxtask-crd.yaml`，group `clawbox.openai.com`，version `v1alpha1`，namespaced。spec 创建后不可修改。核心字段：immutable `toolImage@sha256`、problem、base commit/hint、LLM Secret、LLM CIDR/port、Tool egress CIDR、profile、任务和命令 timeout、输出上限。

状态机：

```text
Queued → Admitted → ToolStarting → ToolReady → RuntimeRunning → Collecting
                                                          ├→ Succeeded
                                                          ├→ Failed
                                                          └→ TimedOut
                                                               ↓
                                                            Cleaned
```

`clawbox/cell/controller.py` 的关键机制：

- finalizer：`clawbox.openai.com/cell-cleanup`；
- 所有子资源带 controller ownerReference；
- `_ensure` 遇到同名对象只接受 owner UID 匹配者，拒绝接管攻击者预建资源；
- 重建 admission 时从所有非终态 CR status 恢复 reservation；
- 先原子保留整个双 VM Cell 的预算，再创建资源；
- 先生成 secret/config/service/network policies，再创建 Tool Pod；
- Tool readiness 未通过时绝不创建 Runtime Job；
- Job 成功后进入 Collecting；runtime 只有收到中央完整 receipt 才会退出 0；
- cleanup 先 foreground 删除 Runtime Job 和 Tool Pod，确认 workload 消失后才删除认证、Service、ConfigMap 和 NetworkPolicy，避免 sidecar 最终上传时 Secret/网络被提前撤走。

`clawbox/cell/app.py` 当前是单副本、每 2 秒 list/reconcile 的简单轮询控制器，不是 watch/informer。它启动时要求唯一一个 ready ARM64 Firecracker node，并固定 placement 到该节点。

## 8. 容量模型

`clawbox/cell/capacity.py` 定义 CPU millicores、memory bytes、storage bytes、pod slots 四维 ResourceVector。

固定 profile：

- small：Runtime 1 CPU/2 GiB/4 GiB；Tool 2 CPU/4 GiB/12 GiB；
- medium：Runtime 2 CPU/4 GiB/8 GiB；Tool 4 CPU/8 GiB/24 GiB；
- large：Runtime 4 CPU/8 GiB/16 GiB；Tool 8 CPU/16 GiB/48 GiB；
- sidecar：250m/512 MiB/1 GiB；
- 每个 VM overhead：250m/256 MiB；
- 总量再加 10% safety；Pod 数固定 2。

`KubernetesNodeCapacityProvider` 统计 ready ARM64 Firecracker 节点 allocatable，减去非 Cell、非终态 Pod request，并用 devmapper baseline 限制 storage。它对 restartable init sidecar、普通 init container 和 Pod overhead 做近似 Kubernetes scheduler 的 request 计算。`collect-node-capacity.py --configmap` 只有在 KVM 可用、thin pool 存在且没有 active SandboxTask 时才输出基线 ConfigMap。

## 9. Tool Pod / Tool Bridge

Tool Pod 使用任务镜像本身作为主容器。init container 从专用 ARM64 Tool Bridge 镜像复制静态二进制到 Pod-local `emptyDir`，然后任务镜像以 UID/GID 10001 执行该二进制。Tool Pod不挂载 LLM 和上传凭证。

`toolbridge/main.go` 是最小 SSH server：

- 只接受用户 `executor`；
- 只接受任务专属 Ed25519 public key；
- 固定 workdir `/testbed`；
- 只支持 SSH `session` + `exec`，不支持 shell/其他 channel；
- 每条命令 `/bin/sh -lc`，单独 process group；
- timeout 后先 SIGTERM，5 秒后 SIGKILL；
- stdout/stderr 各受总输出限制；
- 最大并发默认 4；
- 记录 command SHA256、耗时、exit code、timeout、输出字节/截断、user/system CPU、max RSS；
- 审计写 `/testbed/.clawbox/tool-bridge.jsonl`；
- `--self-test` 要求编译目标为 arm64。

## 10. Runtime Pod、OpenClaw 和 ClawTune

Runtime Job 的主容器和 restartable init sidecar 使用同一个 ARM64 runtime image。两者共享 Pod-local `/state` emptyDir，不与 Tool VM共享文件系统。

`runtime-entrypoint.sh`：

- 生成严格 SSH known_hosts，使用任务专属 client key连接 Tool Service；
- 等待 ClawTune sidecar `/health/ready`；
- link/enable ClawTune OpenClaw plugin；
- patch OpenClaw：SSH sandbox backend、`workspaceRoot=/testbed`、禁 elevated、限制工具；
- plugin 走 localhost sidecar，observe、fail-open、hook-only、无 cgroup/affinity/NUMA；
- OpenClaw 的 LLM 请求发到 localhost sidecar proxy，再由 sidecar访问真实 upstream；
- benchmark 完成后收集 final answer、`git diff --binary`、Tool Bridge trace 和日志，写 `result.json`；
- 写 `.runtime-complete`，等待 sidecar写 `.upload-complete`；300 秒没有 receipt 则任务失败；
- 最终进程 exit code保持 agent exit code。

`clawtune-sidecar-entrypoint.sh`：

- 强制导出 observe-only 环境；
- 后台周期运行 artifact uploader；
- 启动 ClawTune sidecar；
- 看到 `.runtime-complete` 后停止 sidecar和周期 uploader；
- 执行一次 `artifact-uploader --once --require-result`；
- 中央 receipt 完整才写 `.upload-complete`，否则写 `.upload-failed`。

## 11. Trace/result ingester

认证 token 在 controller 内以 HMAC-SHA256 生成，claims 包含 task_id、expiry 和固定 scope `trace:write result:write`。token 为任务专属且有效期为 task timeout + 900 秒。

`artifact-uploader.py`：

- 递归读取 trace 文件；
- 每块 512 KiB；
- chunk ID 由 task/path/offset/content digest 派生；
- 本地 offsets 原子保存，实现 Pod 内重试；
- 上传 result、final trace marker，再读取 receipt；
- 文件锁防止周期 uploader 与最终 uploader 并发。

`clawbox/ingester/app.py`：

- 校验 Bearer token、base64 和 SHA256；
- 禁止 absolute path/`..` 路径逃逸；
- chunk ID 和 path/offset/final 有幂等/冲突检测；
- result 对 task_id 不可变，相同 payload可幂等重传，不同 payload返回 409；
- receipt 只有 result 与 final marker 都存在时 `complete=true`；
- archive read API 由 control-plane service token保护；
- 生产应使用 PostgreSQL。默认 SQLite 只适合开发，且 K8s manifest 未挂 PVC，使用 SQLite 会随 Pod丢失。

## 12. ARM64 镜像工厂

`build-kubernetes-images.sh` 构建三类 ARM64 镜像：Tool Bridge、Runtime、control-plane；要求 native ARM64 host和 ARM64 Docker daemon，发现 qemu/rosetta binfmt立即失败。Runtime Dockerfile用外部 ClawTune checkout作为 build context，构建 plugin，安装 OpenClaw 和 ClawTune Python sidecar。

SWE-ReBench 有两条工厂路径：

- `clawbox/images/arm64.py`：消费显式 build context/Dockerfile recipe；
- `clawbox/images/swerebench.py`：消费完整 SWE-ReBench dataset和固定 SWE-bench fork。

固定 dataset revision：`4ece23ba02fe8b68858e430134adddfd64d6f0f4`。固定 harness revision：`980d0cca8aa4e73f1d9f894e906370bef8c4de8a`。默认要求 selection 正好 128 个任务。

契约测试要求 `/testbed` 存在且可写，shell/git/patch可用，Tool Bridge可运行，测试命令至少能启动。镜像必须以 UID 10001 使用；发布后必须获得 registry digest并证明 manifest包含 `linux/arm64`。mapping 保存 original image → ARM64 digest、recipe revision、platform和 status。构建失败写 `unsupported-arm64`，launcher严格禁止 fallback。

## 13. Benchmark launcher 与 scale

`clawbox/benchmark/kubernetes.py`：读取 task清单和 ARM64 mapping；缺少任何 supported immutable mapping就整体拒绝；preflight验证 namespace、LLM Secret四个 key、RuntimeClass handler/overhead和 CRD；并行创建 SandboxTask；用 run_id构造稳定名字；同名重试只有 spec完全一致才接受；轮询直到 `Cleaned`。

`scale-swe-rebench.sh` 默认依次跑 1、2、4、8、16、32 并发，每阶段前后都运行 devmapper status，任一任务或 thin-pool gate失败立即停止。320 核只是目标容量，不是允许直接跳到高并发的理由。

## 14. 旧控制面代码的定位

`clawbox/scheduler`、`allocator`、`controller`、`node_agent`、`tool_agent`、旧的 `common/models.py` lease/grant/observation协议和 `docker-compose.yml` 是早期多服务控制面，仍保留用于 API/开发兼容，但**不是 SWE-ReBench 生产执行路径**。不要把旧 `controller/kubernetes_backend.py` 的单 Pod/Job逻辑重新接入 benchmark。新功能应扩展 SandboxTask、CellReconciler、CellSizer、NodeCapacityProvider或 PlacementPolicy。

## 15. 安全机制汇总

- 设备破坏必须提供两个 exact canonical device的 `--confirm-erase`。
- 保护 root和代码 checkout backing chain；拒绝 partition/children/mount/holder/loop。
- 配置覆盖前备份到 `/var/lib/clawbox-bootstrap/backups`。
- 版本和状态漂移 fail closed；完成节点不自动迁移。
- RuntimeClass 只在 artifact、devmapper和 handler均通过后创建。
- 节点 ready label 在 gate失败时删除。
- 任务镜像必须 immutable ARM64 digest。
- CR spec immutable，子资源 owner UID验证，finalizer显式清理。
- ServiceAccount token默认不挂载到工作 Pod。
- 容器 non-root、drop ALL capabilities、RuntimeDefault seccomp；Runtime rootfs只读。
- NetworkPolicy default deny，Runtime只允许 DNS、Tool、ingester、LLM CIDR；Tool只允许 DNS和显式任务 egress。
- SSH key和上传 token按任务隔离，host key固定验证。
- Trace chunk checksum、idempotence、不可变 result和最终 receipt闭环。

## 16. 已验证内容

开发机最近验证：

```text
python -m pytest --basetemp ../../.pytest-tmp
27 passed
```

测试分布：DB init 1、ingester 1、Kubernetes/backend/Firecracker contracts 16、phase1 protocol 3、phase2 KB 3、phase3 chain 3。Bash syntax和 `git diff --check` 也通过。已知 warnings是 FastAPI `on_event` deprecation和 Starlette/httpx compatibility warning，不影响当前 test result，但以后要迁移 lifespan和新版 TestClient依赖。

本地测试覆盖：状态迁移字符串/安全条件、唯一 Firecracker RuntimeClass、双 Pod manifest、Secret projection、NetworkPolicy、原子 admission、Pod request、owner adoption拒绝、controller状态顺序、upload token、ingester幂等、ARM64 image/build契约、Tool Bridge审计字段等。

## 17. 未验证和未完成内容

以下都不能声称完成：

- 目标机 containerd 2.3.4/runc 1.5.1安装完成；
- Kubernetes 1.35/Calico正常；
- Kata 3.31.0 ARM64 release内的 runtime-rs Firecracker配置实际能通过脚本归一化；
- Firecracker 1.12.1真实启动；
- containerd 2.3.4 config v3 drop-in语法在该发行版上真实可用；
- devmapper plugin状态为 `ok`；
- FC-0 至 FC-5真机通过；
- ARM64 Runtime/Tool Bridge/control-plane镜像构建推送；
- 128 个 SWE-ReBench ARM64镜像构建与mapping；
- PostgreSQL ingester生产部署；
- SandboxTask单任务端到端；
- 1/2/4/8/16/32并发压测；
- 失败清理和 snapshot回收在真实环境下长期稳定。

## 18. 必须优先修复的缺陷

### P0-1：下载不持久、不能跨 bootstrap 重跑续传

当前 bootstrap `download()` 写 `${TMP_DIR}`，EXIT trap会删除；Kata builder也使用独立 temp。慢速网络下 Ctrl+C 或单次失败会丢失已下载部分。下一 agent应：

- 建立例如 `/var/cache/clawbox-bootstrap/downloads` 的 root-only cache；
- cache key至少包含完整 URL的安全 hash，不能只用 basename造成碰撞；
- 下载到 `.part`，使用 curl `--continue-at -`；
- 下载完成后原子 rename；
- checksum失败必须删除对应坏 cache，绝不能复用；
- containerd、runc、Kata、Firecracker都复用同一机制；
- 保留官方 checksum验证；
- 支持用户显式清理单个坏 cache，但不要自动清空全部缓存；
- 为中断、resume、checksum mismatch和同 basename不同 URL增加测试。

### P0-2：devmapper 创建后 bootstrap 重跑不幂等

当前 storage `validate_device` 要求 whole disk无 children/holders。首次成功创建 LVM 后，这两块盘会有 holder，下一次 bootstrap开头的 storage plan就会拒绝。因此如果 FC-2之后、FC-5之前失败，无法直接安全重跑。

修复方向：

- 在 state中记录 canonical data/meta device、VG、pool和 dm_name；提升 state schema；
- read-only识别“这两个 PV恰好属于 ClawBox-owned VG/pool”的状态；
- 若 ownership、PV UUID、VG/LV结构、设备路径全部匹配，则 plan报告 `already initialized/owned`，apply跳过 pvcreate/vgcreate/lvcreate，只 reconcile drop-in/systemd/plugin；
- 若只部分创建或结构不匹配，fail closed并打印人工诊断，不自动删除 LVM；
- 不得把普通“有 holder”放宽成任意可接受；只接受有明确 owner state且结构完全匹配的既有 ClawBox pool；
- `--confirm-erase`只用于首次初始化，重用 owned pool不应再次擦盘；
- 增加首次 plan/apply、owned rerun、foreign VG、partial VG、device swap、state丢失等测试。

### P0-3：状态文件写入过早，表达的是 intent而非完成事实

bootstrap在下载和宿主修改前后较早写完整 `versions.env`，导致失败后看起来像“host owns”整个目标配置，实际可能只完成一部分。旧 QEMU迁移问题就是此模型的表现。

建议改成：

- `desired.env` 保存请求；
- `installed.env` 或 stage markers保存每阶段已成功事实；
- 每阶段完成后原子更新；
- retry根据阶段和真实探测收敛；
- stage0完成后才锁定完整版本漂移；
- migration必须有 schema和明确函数，不能靠删除状态。

### P0-4：Firecracker vCPU 配置未固定导致 128 vCPU 校验失败（已修复）

Kata runtime-rs 对未设置的 `default_vcpus`/`default_maxvcpus` 会用宿主机核数填充 `default_maxvcpus`（Kunpeng 128），而 Firecracker 上限 32，`load config` 即报 `Firecracker hypervisor can not support 128 vCPUs`。FC-0 原先不检查该键，导致 bootstrap 复用带病 `/opt/kata` 树并在在线冒烟持续失败。

已在 `scripts/build-kata-firecracker-arm64.sh` 生成配置时固定 `default_vcpus = 2`、`default_maxvcpus = 32`（`CLAWBOX_FC_DEFAULT_VCPUS`/`CLAWBOX_FC_MAX_VCPUS` 可覆盖，限 [1,32]），并在 `scripts/audit-kata-firecracker-arm64.sh`（FC-0）新增：

- `default_vcpus` 必须在 [1,32]；
- `default_maxvcpus` 必须在 [1,32]；
- `default_vcpus <= default_maxvcpus`。

遗留观察：`static_sandbox_resource_mgmt = true` 下，Pod 带 CPU 请求时 VM 按 `overhead_vcpus + 请求` sizing（大 profile Tool 8 CPU → 约 9 vCPU，远小于 32），`default_vcpus` 只是无 sizing 信息时的回退值。若未来单 Pod 请求超过 32 CPU，需在 capacity/placement 层拦截，不能让 Kata 生成超限配置。

### P0-5：pool 重建后 containerd devmapper 快照元数据陈旧导致 ENODATA（已修复）

apply 整盘重建 thin pool（`vgremove`/`pvremove` + 重新 lvcreate/lvconvert）后，containerd 的 devmapper snapshotter 元数据（`/var/lib/containerd/io.containerd.snapshotter.v1.devmapper/`，持久化在 `/var/lib/containerd`，不在 pool 内）不会自动失效，仍引用旧 pool 的 thin 设备 ID。Kata sandbox 从陈旧的父快照（如 alpine 基础 `snap-1` = dev 1）创建新快照时，内核 dm-thin 因 origin 设备不存在返回 `-ENODATA`，报：

```text
failed to create snapshot "clawbox-fc--pool-snap-493" (dev: 3) from "clawbox-fc--pool-snap-1" (dev: 1): no data available
```

已在 `scripts/setup-devmapper-openeuler-arm64.sh apply` 中，于重建 pool、写完 drop-in/`containerd config dump` 之后、`systemctl start containerd` 之前：`systemctl stop containerd` → `rm -rf /var/lib/containerd/io.containerd.snapshotter.v1.devmapper` → 启动。content store（blob）保留，镜像本地重新解包，无需重新下载。

注意：该清理对 apply 是无条件的（apply 目前只允许 VG 不存在时运行，即必然重建 pool）；未来 P0-2 加入 owned-pool 幂等重跑时，重跑路径必须跳过此清理。

### P1：生产清单仍含 placeholder/tag

`deploy/cell-controller.yaml` 和 `trace-ingester.yaml` 使用 `registry.example.com/...:dev`；必须替换为真实 immutable ARM64 digest。Secret example也必须替换。生产 ingester必须使用持久 PostgreSQL，不能用容器内 SQLite。

### P1：真实下载和 registry可达性

中国网络下 GitHub release/raw速度和稳定性差。可以支持 `HTTPS_PROXY`、企业缓存或离线 artifact目录，但必须保持 checksum验证。还应对 pkgs.k8s.io、GHCR、Quay、registry.k8s.io和镜像 pull做独立 preflight和清晰诊断。

### P1：controller并发/HA限制

当前 controller单副本轮询，没有 leader election、watch/informer或事务化全局 reservation。单节点初始验证可接受，但扩展到多 controller/多 node前必须重新设计一致性。不要简单把 replicas改成 2。

### P1：NetworkPolicy/LLM endpoint模型

当前只允许一个 `llmEgressCIDR`；DNS解析后的多 IP/CDN变化可能导致任务断网。需要由运维明确固定 egress gateway/CIDR，不要为了方便开放 `0.0.0.0/0`。Tool任务若需要拉依赖，也必须显式配置 tool egress CIDR。

### P2：文档编码

现有 README/部分 docs中的 Unicode树线和箭头在某些 Windows/终端读取时出现乱码。内容不影响代码，但建议统一 UTF-8并避免不一致换行。

## 19. 接手后的推荐实施顺序

1. 确认目标机 `/var/lib/clawbox-bootstrap`、`/etc/kubernetes`、containerd版本、两块盘仍未变化；不要假设上次中断点。
2. 实现并测试持久化 resumable cache，提交推送，目标机 `git pull`。
3. 修复 devmapper owned rerun幂等性，再继续破坏性 FC-2；否则后段失败会非常难恢复。
4. 目标机重新运行 bootstrap apply并耐心完成 containerd/runc下载。
5. Kubernetes/Calico阶段失败时收集 `systemctl status`、`journalctl`、`kubectl events`；禁止 kubeadm reset。
6. FC-1前确认 Kata/Firecracker大包也走缓存；预计比 containerd更慢。
7. FC-2前最后一次确认 NVMe序列号；创建后立刻记录 `pvs/vgs/lvs/dmsetup/containerd config dump`。
8. 逐一保存 FC-0/FC-2/FC-3/FC-4/FC-5输出，不要只依赖 bootstrap末尾摘要。
9. stage0成功后再构建/推送三类项目镜像并替换 K8s placeholder。
10. 部署 namespace/RBAC/CRD/ingester/capacity/controller。
11. 用一个简单 ARM64工具镜像先跑一个 SandboxTask，验证 trace/result receipt和cleanup。
12. 构建 128个 SWE-ReBench ARM64镜像；unsupported任务必须显式记录，不能fallback。
13. 跑 1/2/4/8/16/32阶梯压测，每阶存档 CPU、内存、VM boot latency、调度延迟、thin-pool data/meta、snapshot和任务完整性。

## 20. 下一次目标机命令

代码已包含 128-vCPU 修复（P0-4）和 devmapper 快照元数据清理（P0-5）。**当前目标机 /opt/kata 已是修复树（FC-0 17/17），Kubernetes/Calico/pool 均已就绪**，优先走快速路径，不必整盘重来：

```bash
cd ~/ClawBox && git pull

# 快速路径：只清 containerd 的 devmapper 快照元数据（仍指向旧 pool 的设备 ID）
sudo systemctl stop containerd
sudo rm -rf /var/lib/containerd/io.containerd.snapshotter.v1.devmapper
sudo systemctl start containerd

# 冒烟失败时 bootstrap 已移除 ready 标签；补回后直接重跑权威 live gate
kubectl label nodes --all clawbox.openai.com/firecracker-ready=true --overwrite
bash scripts/arm64-kata-smoke.sh --runtime-class kata-fc-arm64
```

预期：containerd 启动后快照器为空，alpine 从 content store 本地重新解包（无需下载）；三个 Pod Ready，两个 Kata VM 正常启动（不再报 ENODATA）。

若仍要完整走一遍 apply（自愈验证 P0-5 已进代码）：与上次相同，先手动拆除 pool 再 apply（P0-2 幂等重跑尚未实现）：

```bash
sudo vgremove -y clawbox || true
sudo pvremove -y /dev/nvme0n1 /dev/nvme1n1 || true

sudo bash scripts/bootstrap-openeuler-arm64.sh status

lsblk -e7 -o NAME,PATH,SIZE,TYPE,FSTYPE,MOUNTPOINTS

sudo env HTTPS_PROXY=http://127.0.0.1:1080 HTTP_PROXY=http://127.0.0.1:1080 \
     NO_PROXY=localhost,127.0.0.1,193.124.7.2,192.168.0.0/22,10.96.0.0/12 \
  bash scripts/bootstrap-openeuler-arm64.sh apply \
  --devmapper-data-device /dev/nvme0n1 \
  --devmapper-meta-device /dev/nvme1n1 \
  --confirm-erase /dev/nvme0n1,/dev/nvme1n1
```

预期：`install_kata_firecracker` 首步 FC-0 审计通过（/opt/kata 已是修复树）→ 复用不重建；devmapper 重建后 setup-devmapper 自动清空快照元数据（P0-5）→ 静态 gate 全绿 → 在线冒烟两个 Kata Pod 成功启动。

另开终端观察：

```bash
sudo ps -eo pid,etime,cmd | grep -E '[c]url|bootstrap-openeuler'
sudo ss -tpn | grep ':443'
```

如果失败，必须保存“最近一个 `==>` 到 ERROR”的完整输出。不要删除 `versions.env`，不要运行 `kubeadm reset`，不要 `wipefs`。`vgremove`/`pvremove` 仅用于上述明确场景（当前 bootstrap 的 storage plan 不接受带 holders 的盘）。

## 21. 最终验收证据清单

项目最终完成至少需要归档：

- git revision和所有镜像 immutable digest；
- `versions.env`、stage markers、`firecracker-audit.env`；
- Firecracker/jailer/shim/kernel/rootfs的 file/version/path证据；
- containerd 2.x version、config dump、devmapper plugin `ok`；
- `pvs/vgs/lvs/dmsetup`与thin-pool data/meta使用率；
- RuntimeClass完整 YAML；
- 节点 arch/KVM/ready label；
- FC-5两个不同 boot ID、两个 Firecracker进程、无 QEMU、NetworkPolicy隔离、snapshot回收；
- 三类 ClawBox ARM64镜像及 Tool Bridge self-test；
- 128任务mapping、每个 recipe revision/status/digest；
- 单个 SandboxTask完整状态流和所有子资源清理；
- ingester中的 result、patch、final answer、trace、Tool Bridge audit及 complete receipt；
- 1/2/4/8/16/32每个并发阶段的成功率、耗时、资源和thin-pool指标；
- 失败场景下没有泄漏 Pod、Job、Secret、Service、NetworkPolicy或active snapshot。

## 22. 文件导航

宿主与 Firecracker：

- `scripts/bootstrap-openeuler-arm64.sh`：总入口、状态、版本、安装顺序、stage0。
- `scripts/audit-kata-firecracker-arm64.sh`：FC-0只读审计。
- `scripts/build-kata-firecracker-arm64.sh`：FC-1组装/安装。
- `scripts/setup-devmapper-openeuler-arm64.sh`：FC-2磁盘/LVM/containerd。
- `deploy/check-host.sh`：静态 host/handler/runtime gate。
- `scripts/arm64-kata-smoke.sh`：真实双 VM gate。
- `deploy/containerd-firecracker.toml`、`containerd-clawbox.service`、`runtimeclass-firecracker.yaml`、`calico-installation.yaml`：宿主配置模板。

Cell控制面：

- `deploy/sandboxtask-crd.yaml`：API contract。
- `clawbox/cell/controller.py`：状态机、幂等、cleanup。
- `clawbox/cell/manifests.py`：Pod/Job/Secret/Service/NetworkPolicy渲染。
- `clawbox/cell/capacity.py`：资源模型和 admission。
- `clawbox/cell/app.py`：单副本 reconcile loop。
- `deploy/control-plane-rbac.yaml`、`cell-controller.yaml`：RBAC和Deployment。

Runtime/Tool/trace：

- `toolbridge/main.go`：静态SSH bridge。
- `scripts/runtime-entrypoint.sh`：OpenClaw任务执行和最终握手。
- `scripts/clawtune-sidecar-entrypoint.sh`：sidecar生命周期。
- `scripts/artifact-uploader.py`：chunk/result上传。
- `clawbox/ingester/app.py`、`auth.py`：中央持久化API和token。
- `docker/Dockerfile.runtime`、`Dockerfile.tool-bridge`、`Dockerfile.control-plane`：ARM64镜像。

镜像/benchmark/scale：

- `clawbox/images/arm64.py`：通用native ARM64 factory。
- `clawbox/images/swerebench.py`：固定dataset/harness的128任务factory。
- `scripts/build-swe-rebench-arm64.py`：CLI wrapper。
- `clawbox/benchmark/kubernetes.py`：mapping-only SandboxTask launcher。
- `scripts/run-swe-rebench.sh`、`scale-swe-rebench.sh`：运行和阶梯压测。
- `scripts/collect-node-capacity.py`：主机inventory和容量baseline。

旧兼容控制面：

- `clawbox/scheduler`、`allocator`、`controller`、`node_agent`、`tool_agent`、`docker-compose.yml`：保留但不用于生产benchmark Cell。

测试与说明：

- `tests/`：27项本地测试。
- `docs/OPENEUER_ARM64.md`：生产runbook。
- `docs/CONCURRENT_KATA_SWE.md`：Cell并发模型。
- `docs/IMPLEMENTATION_MAPPING.md`：目标到文件映射。
- `docs/PHASE3.md`：旧/新路径边界。

## 23. 给接手 agent 的一句话任务定义

不要重写上层控制器；先让 openEuler/Kunpeng ARM64 主机上的 `Kata 3.31.0 + Firecracker 1.12.1 + containerd 2.3.4 devmapper` 以可恢复、可重跑、可审计方式真实通过双 VM smoke，再完成 ARM64镜像、中央上传和并发压测。任何 QEMU或 x86 fallback、任何绕过checksum、任何自动清盘/重置、任何未证明的“已完成”都不接受。
