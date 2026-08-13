# ClawBox

ClawBox 是一个面向 Linux 的多租户工具执行与资源调度系统。它会在执行命令前预测资源需求，分配 CPU 和内存额度，把命令发送到指定的 Docker Tool 容器，并在执行结束后回收资源、记录结果和更新 ClawTune 知识库。

## 你将得到什么

成功部署后，Linux 主机上会运行以下服务：

- PostgreSQL：保存任务、租户、资源租约和知识库状态。
- Tenant Scheduler：接收任务并使用 ClawTune 预测资源需求。
- Allocator：检查容量和租户额度，防止同一份资源被重复分配。
- Controller：为 workspace 创建或复用固定的 Docker Tool 容器。
- Node Agent：读取当前机器的 CPU、NUMA 和系统状态。
- Tool Agent：在隔离的 Tool 容器中执行实际命令并返回结果。

一次任务大致会经历：

```text
提交命令
  → 预测 CPU、内存和时间
  → 分配资源
  → 找到对应的 Tool 容器
  → 执行命令
  → 收集结果
  → 更新租户知识库
  → 释放资源
```

## 第一次部署

### 第 1 步：准备 Linux 主机

主机需要 Linux、Docker Engine、Docker Compose v2、cgroup v2、Git、curl、OpenSSL 和 Python 3。

检查 Docker：

```bash
docker version
docker compose version
docker info >/dev/null && echo "Docker 正常"
```

检查 cgroup v2：

```bash
stat -fc %T /sys/fs/cgroup
```

预期输出 `cgroup2fs`。如果 `docker info` 提示没有权限：

```bash
sudo usermod -aG docker "$USER"
```

执行后注销 Linux 用户并重新登录，再次运行 `docker info`。

### 第 2 步：准备两个项目

ClawBox 和 ClawTune 必须放在同一个父目录下：

```text
~/ClawBox/
~/ClawTune/
```

检查目录：

```bash
test -f ~/ClawBox/docker-compose.yml && echo "ClawBox 正常"
test -f ~/ClawTune/services/scheduler/pyproject.toml && echo "ClawTune 正常"
```

如果还没有 ClawTune：

```bash
cd ~
git clone https://github.com/w20chen/ClawTune.git
```

这一步让 Scheduler 直接加载已有的 ClawTune 预测逻辑。

### 第 3 步：更新代码

```bash
cd ~/ClawBox
git pull
```

这会拉取最新代码和部署脚本，不会删除已有的 `.env` 或数据库。

### 第 4 步：一键部署和验证

```bash
cd ~/ClawBox
bash scripts/linux-deploy.sh all
```

脚本会依次完成：

1. 检查 Linux、Docker、Compose、cgroup v2 和 ClawTune。
2. 自动读取主机的 NUMA 和逻辑 CPU 数量。
3. 创建 `.env`，自动生成服务密钥、执行授权密钥和数据库密码。
4. 构建控制面和 Tool Agent 镜像。
5. 启动 PostgreSQL 与控制面服务。
6. 等待服务健康，并确认 ClawTune 可用。
7. 创建一个真实 Docker Tool 容器并执行 `print(42)`。
8. 检查命令输出、知识库更新和资源释放。

第一次构建需要下载基础镜像和 Python 依赖，耗时取决于网络速度。

成功时，最后会看到：

```text
PASS: Docker execution, telemetry, KB update, and lease release succeeded
```

至此，Phase 3 的本机 Docker 链路已经运行成功。

## 分步部署

如果希望每完成一步就检查一次，可以不用 `all`，按下面顺序执行。

### 1. 生成配置

```bash
bash scripts/linux-deploy.sh init
```

这一步会检查主机、自动探测 NUMA CPU、创建 `.env`。已有配置和密钥不会被覆盖，密钥也不会打印到终端。

查看非敏感配置：

```bash
grep -E 'CONTROLLER_BACKEND|CONTROLLER_DOCKER_NETWORK|TOOL_IMAGE|NUMA_CAPACITY|RESERVED_CPU_FRACTION' .env
```

不要把 `.env` 提交到 Git。

### 2. 构建并启动

```bash
bash scripts/linux-deploy.sh deploy
```

这一步会构建 Docker 镜像、创建内部网络、启动数据库和控制面，然后等待健康检查。如果 ClawTune 没有正确加载，脚本会停止并显示错误。

### 3. 执行验收任务

```bash
bash scripts/linux-deploy.sh verify
```

这一步会提交一个测试任务，创建或复用 Tool 容器，执行真实 Python 命令，然后检查知识库更新和资源释放。

## 日常使用

### 查看运行状态

```bash
bash scripts/linux-deploy.sh status
```

它会显示所有容器，并访问各个服务的健康接口。

### 查看实时日志

```bash
bash scripts/linux-deploy.sh logs
```

按 `Ctrl+C` 退出日志，不会停止服务。

### 更新并重新部署

```bash
cd ~/ClawBox
git pull
bash scripts/linux-deploy.sh deploy
bash scripts/linux-deploy.sh verify
```

脚本会复用已有 `.env` 和 PostgreSQL 数据。

### 停止服务

```bash
bash scripts/linux-deploy.sh down
```

这会停止并删除运行容器和网络，但保留 PostgreSQL 数据卷。下次部署后，已有任务和知识库数据仍然存在。

不要随意执行：

```bash
docker compose down -v
```

`-v` 会删除数据库卷及其中的数据。

## 修改自动配置

默认预留 5% CPU 给系统和控制面：

```env
RESERVED_CPU_FRACTION=0.05
```

修改 `.env` 后重新部署：

```bash
nano .env
bash scripts/linux-deploy.sh deploy
```

脚本通常会自动探测 NUMA，不需要手工填写。查看机器 NUMA：

```bash
for node in /sys/devices/system/node/node[0-9]*; do
  printf '%s: ' "$(basename "$node")"
  cat "$node/cpulist"
done
```

如果需要覆盖，在 `.env` 中填写每个 NUMA 节点的逻辑 CPU 数量。例如四个 NUMA、每个 80 个逻辑 CPU：

```env
NUMA_CAPACITY=0:80,1:80,2:80,3:80
```

这里填写的是数量，不是 CPU ID 列表。

## 开发者本地检查

如果只想检查 Python 代码，不启动 Docker 控制面：

```bash
cd ~/ClawBox
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
bash scripts/e2e.sh
bash scripts/test-multitenancy.sh
```

这会运行单元测试、多租户安全测试和本机执行链。`scripts/e2e.sh` 使用本地测试后端，不等于 Docker 隔离；正式 Docker 验收请使用 `linux-deploy.sh verify`。

## 常见问题

### Docker 没有权限

如果看到 `permission denied while trying to connect to the Docker daemon`：

```bash
sudo usermod -aG docker "$USER"
```

注销并重新登录后重试。

### 找不到 ClawTune

确认目录是：

```text
~/ClawBox
~/ClawTune
```

不要把 ClawTune 放进 `~/ClawBox` 内部。

### Scheduler 显示 degraded

先查看日志：

```bash
bash scripts/linux-deploy.sh logs
```

再检查 ClawTune 是否挂载成功：

```bash
docker compose -p clawbox exec tenant-scheduler \
  test -f /opt/clawtune/services/scheduler/src/tool_resource/runtime_kb.py \
  && echo "ClawTune 挂载正常"
```

### 端口已被占用

默认端口是 `8080` Scheduler、`8081` Allocator、`8082` Controller、`8083` Node Agent。

检查占用：

```bash
sudo ss -lntp | grep -E ':8080|:8081|:8082|:8083'
```

### 部署失败后从哪里看原因

```bash
bash scripts/linux-deploy.sh status
bash scripts/linux-deploy.sh logs
```

脚本在健康检查或验收失败时也会自动打印相关服务的最近日志。

如果第一次部署出现 PostgreSQL `duplicate key ... pg_type_typname_nsp_index`，说明运行的是较早版本，多个服务同时建表发生了竞态。更新代码后直接重新部署即可，不需要删除数据库卷：

```bash
git pull
bash scripts/linux-deploy.sh deploy
bash scripts/linux-deploy.sh verify
```

## 当前范围

当前阶段适合：

- Linux 单机 Docker 部署与功能验证
- 多租户知识库隔离
- 租户 CPU quota 和并发控制
- sticky workspace 到 Tool 容器
- 执行授权、防篡改和防重放
- 任务结果持久化与知识库更新

仍在后续阶段完成：

- 新控制面的 Kubernetes Controller backend
- Kubernetes Guaranteed QoS 和 NUMA placement 验证
- Kata Containers / Firecracker 接入新执行链
- 完整 `claw-launch`、cgroup v2 与 eBPF 可信遥测
- 面向生产环境的高可用、mTLS、迁移和自动故障恢复

当前 Docker Controller 会挂载 Docker socket，因此只应部署在受信任的内网主机，不要把 Controller API 暴露到公网。

## 进阶文档

- [Phase 3 架构、安全边界和故障语义](docs/PHASE3.md)
- [现有 ClawTune 组件与新架构映射](docs/IMPLEMENTATION_MAPPING.md)
- [原 Kubernetes/Kata 部署说明](deploy/README.md)
