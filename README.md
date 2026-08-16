# ClawBox

ClawBox 在 Kubernetes 上并发运行隔离的 OpenClaw 租户。当前 MVP 的主路径是：

- 每个 SWE-Rebench 任务对应一个独立 OpenClaw runtime。
- Pod 使用可配置的 Kata RuntimeClass；鲲鹏/openEuler 首轮默认 `kata-qemu`，验证后可切换 `kata-fc`。
- SWE-Rebench 任务代码来自任务自身的 Docker 镜像。
- OpenClaw、插件和 sidecar 复用 ClawTune 生成的 runtime bundle。
- 当前先跑通并发部署；预测算法、共享 KB、自定义调度和 eBPF 不在主路径中。

## 本地一键运行

鲲鹏/openEuler 部署前必须先完成 [ARM64 阶段 0/1 门禁](docs/OPENEUER_ARM64.md)。

### 前置条件

- Linux、Kubernetes、Docker 和 Python 3。
- Helm；脚本用 Kata 官方 chart 安装节点运行时。
- 宿主机及 Kubernetes 节点都能访问 `/dev/kvm`。
- `~/ClawBox` 和 `~/ClawTune` 位于同一父目录。
- 一个 OpenAI-compatible 模型服务及 API key。

API key 文件中只放 key 本身：

```bash
printf '%s' 'your-api-key' > ~/llm-api-key.txt
chmod 600 ~/llm-api-key.txt
```

### 首次运行

如果当前没有集群（包括刚删除了旧 Docker-driver Minikube），运行：

```bash
cd ~/ClawBox
bash scripts/local-kata-swe.sh \
  --bootstrap-minikube \
  --api-key-file ~/llm-api-key.txt \
  --base-url https://api.example.com/v1 \
  --model provider-model
```

该选项会通过 Ubuntu `apt` 安装 KVM/libvirt，创建 8 CPU、16 GiB 的 kvm2 Minikube，
然后自动进入具有 `libvirt` 权限的新进程、安装 Kata 并运行任务。它要求宿主机已经存在
`/dev/kvm`，无需手动注销登录。

已有能够访问 `/dev/kvm` 的 Kubernetes 集群时，运行：

```bash
cd ~/ClawBox
bash scripts/local-kata-swe.sh \
  --api-key-file ~/llm-api-key.txt \
  --base-url https://api.example.com/v1 \
  --model provider-model \
  --install-kata
```

脚本会自动：

1. 生成 ClawTune runtime bundle。
2. 构建并导入本地 bundle 镜像。
3. 使用 Kata 官方 Helm chart 安装运行时，并应用 namespace、RBAC 和 NetworkPolicy。
4. 创建或更新 LLM Secret。
5. 使用所选 RuntimeClass 启动一个真实 Kata smoke Pod。
6. 运行一个 SWE-Rebench 任务。

支持普通 containerd、k3s、kind、minikube 和 k3d。本地运行不需要远程镜像仓库。
如果 Kata 已经安装，可以省略 `--install-kata`。

### 并发运行

首次成功后，Secret、bundle 和镜像会被复用：

```bash
bash scripts/local-kata-swe.sh \
  --sample 8 \
  --parallelism 4 \
  --cpu 4 \
  --memory 8Gi
```

每个并发任务都拥有独立的 Kata VM；只有显式选择并验证 `kata-fc` 时才使用 Firecracker。请确保节点有足够的 CPU 和内存。

### 状态与日志

```bash
bash scripts/local-kata-swe.sh status
bash scripts/local-kata-swe.sh logs
bash scripts/local-kata-swe.sh smoke
```

任务失败时，一键脚本会自动输出最近的 Pod、事件、描述和容器日志。

## 常用选项

```text
--tasks PATH          指定任务 JSON
--sample N            选择任务数量，默认 1
--parallelism N       最大并发数，默认 1
--cpu VALUE           每个任务的 CPU，默认 2
--memory VALUE        每个任务的内存，默认 4Gi
--runtime-class NAME  已安装的 Kata RuntimeClass，默认 kata-qemu
--rebuild             ClawTune 更新后重建 bundle 和镜像
--skip-smoke          跳过 Kata smoke test
--install-kata        使用官方 Kata Deploy Helm chart 安装节点运行时
--bootstrap-minikube  安装 Ubuntu KVM/libvirt 并创建 kvm2 Minikube
```

查看全部参数：

```bash
bash scripts/local-kata-swe.sh --help
```

## 常见问题

### Kata Pod 启动失败

检查脚本自动打印的 `kubectl describe pod` 输出。若出现 handler 不存在或
`FailedCreatePodSandBox`，需要先在 containerd 中配置所选 Kata handler；创建
`deploy/runtimeclass.yaml` 本身不会安装运行时。直接重新运行首次命令并加
`--install-kata`，或传入机器上实际存在的 `--runtime-class`。若 Minikube 节点看不到 `/dev/kvm`，需先用支持 KVM 的 VM driver
（例如 `kvm2`）重建 Minikube。

### ClawTune bundle 权限错误

此前若使用过 `sudo ... runner prepare`：

```bash
sudo chown -R "$(id -u):$(id -g)" ~/ClawTune/swe_rebench/.runtime
```

之后始终以普通用户运行一键脚本。

### 查看某个失败任务

```bash
kubectl -n clawbox-benchmarks get jobs,pods
bash scripts/local-kata-swe.sh logs
```

## 文档

- [Kubernetes + Kata/Firecracker 并发部署细节](docs/CONCURRENT_KATA_SWE.md)
- [Phase 3 架构与安全边界](docs/PHASE3.md)
- [ClawTune 与 ClawBox 组件映射](docs/IMPLEMENTATION_MAPPING.md)
- [原始租户 cell 部署说明](deploy/README.md)

## 开发验证

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
python3 deploy/test_render.py
```
