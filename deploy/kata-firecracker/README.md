# 一键并发跑 OpenClaw 多租户（Kata Containers + Firecracker）

> 一句话：**一个 Pod = 一个完整的 ClawTune 实例**（OpenClaw + 插件 + Python sidecar +
> 独立临时 KB + 一次性任务）。宿主机配好后，**第 4 步一条命令就能并发跑 4 个**。

链路：

```
Kubernetes
  → containerd
    → Kata RuntimeClass (kata-fc)
      → Firecracker microVM
        → 单容器：OpenClaw + 插件 + sidecar + 临时 KB
```

---

## 0. 一次性前置条件（宿主机）

- Linux ARM64（Kunpeng 为主）；
- KVM 可用（`/dev/kvm` 存在）；
- 单节点 Kubernetes + containerd；
- **Kata Containers + Firecracker 已装好，且 containerd 里配置了 `kata-fc` handler**
  （这是唯一需要人工保证的宿主机项，见 `runtimeclass.yaml` 里的注释）；
- 能 push/pull 的镜像仓库（例：`registry.local`）；
- 外部 OpenAI 兼容 LLM API（所有实例共用）；
- 宿主机有 `docker`、`kubectl`、`envsubst`（gettext）。

---

## 1. 宿主机自检（30 秒，只检查不改系统）

```bash
./deploy/kata-firecracker/check-host.sh
```

看到 `FAIL` 先解决（尤其是 `/dev/kvm`、`kata-fc` handler、`firecracker`）。
看到 `WARN` 一般可忽略。

缺 `envsubst` 装一下：

```bash
# openEuler / RHEL
sudo dnf install -y gettext
# Debian / Ubuntu
sudo apt-get install -y gettext-base
```

---

## 2. 准备镜像构建上下文（一次性）

让 `ClawTune` 和 `claw-k8s` 互为同级目录。不要创建指向 build context
外部的软链接；使用 BuildKit named context 传入 ClawTune 源码：

```bash
# 在 claw-k8s 仓库根目录执行
test -f ../ClawTune/packages/openclaw-plugin/package.json
test -f ../ClawTune/services/scheduler/pyproject.toml
```

---

## 3. 构建并推送 runner 镜像（一次性）

```bash
docker build -f docker/Dockerfile.runner \
  --build-context clawtune=../ClawTune \
  -t registry.local/openclaw-runner:latest .

docker push registry.local/openclaw-runner:latest
```

镜像内容（不需要你手动装任何东西）：

| 组件 | 说明 |
| --- | --- |
| Node.js + OpenClaw 2026.7.1 | 官方 npm 包 |
| ClawTune 插件 (agent-scheduler) | 构建好的 `dist/` |
| Python sidecar | `clawtune-sidecar` 0.2.0 + 依赖 |
| 只读 base KB | 每个租户启动时拷贝成独立 `kb.sqlite` |
| `run-once.sh` | 容器入口 |

镜像**不含**任何 LLM 模型，LLM 走 HTTP（外部 API）。

## 4. 一键并发跑（重点，照抄）

在仓库根目录，把下面 5 个变量换成你的，然后直接运行：

```bash
IMAGE=registry.local/openclaw-runner:latest \
TASK_MESSAGE="Use the shell to print claw-cloud-ok" \
OPENAI_BASE_URL=http://<llm-host>:8000/v1 \
OPENAI_API_KEY=<你的key> \
OPENCLAW_MODEL=<模型名> \
./deploy/kata-firecracker/run.sh
```

**默认就是 4 个并发**（`PARALLELISM=4`、`COMPLETIONS=4`）。

- 只想先试 1 个：加 `PARALLELISM=1 COMPLETIONS=1`
- 想跑 8 个：加 `PARALLELISM=8 COMPLETIONS=8`
- 想换任务：改 `TASK_MESSAGE`

脚本自动完成：检查变量 → 建/更新 RuntimeClass → 建 Secret（key 不进命令行）→
创建 Indexed Job → 等 Pod 起来 → 等任务结束 → 打印**所有 Pod 的日志**。
Job 不会自动删除，方便你复查。

## 5. 一个 Pod 里到底是什么（为什么「像手动跑一个 ClawTune」）

每个 Pod = 一个独立 Firecracker microVM，里面只有一个容器，容器内就是
一套完整可自跑的 ClawTune 环境：

1. 读 `TENANT_ID`（来自 Job 的 completion index）、`TASK_MESSAGE`、LLM 配置；
2. 建 `/state/<TENANT_ID>/`，把 base KB 拷贝成该租户独立的 `kb.sqlite`；
3. 后台启动 **sidecar**（监听 `127.0.0.1:8765`，用这份独立 KB）；
4. 等 sidecar `GET /health/ready` 就绪；
5. 配置 **OpenClaw**：`plugins install --link` 装插件 → `config patch` 指向本 VM
   sidecar → `onboard` 把 provider 指到本 VM 的 sidecar LLM 代理；
6. 执行一次 `openclaw agent --local --message "$TASK_MESSAGE"`（非交互、跑完即退）；
7. 输出一行 JSON 结果（`tenant_id / status / elapsed_s / exit_code`），
   trap 杀掉 sidecar，Pod 结束。

也就是说：你手动跑 ClawTune 要做的事（起 sidecar、装插件、配 provider、跑一次 agent）
全部被自动化进这一个容器，每个租户互不共享 KB、互不干扰，只有外部 LLM 是共用的。

## 6. 看结果 / 验证隔离

```bash
kubectl get pods -l job-name=claw-runner -o wide     # 4 个 Pod，不同 TENANT_ID
kubectl logs <pod>                                   # 每行最后是 JSON 结果
kubectl describe pod <pod>                           # 确认 runtimeClassName: kata-fc
ps aux | grep firecracker                            # 宿主机上能看到多个 Firecracker 进程
```

预期：

- 4 个 Pod 各有独立的 `TENANT_ID`（0, 1, 2, 3）；
- 每个 Pod 一个独立 Firecracker sandbox；
- 每个实例日志里是**自己**的 `/state/<id>/kb.sqlite`，没有共享 KB 路径；
- 4 个实例同时打同一个外部 LLM API；
- 每个实例只执行一次后 Pod 状态 `Completed`；
- 一个实例失败不影响其他实例（`backoffLimit: 0`）。

> `/state` 是 `emptyDir`，随 Pod 生命周期存在；Pod 被删后日志/状态即销毁（这是设计意图：
> 一次性租户）。要保留现场就别删 Job。

## 7. 清理 / 重跑

```bash
kubectl delete job claw-runner
kubectl delete secret claw-llm
```

重跑 = 直接再执行一遍第 4 步的命令（Secret 会自动重建）。

## 8. 报错对照表

| 现象 | 原因 / 处理 |
| --- | --- |
| `check-host.sh` 有 FAIL | 先解决，重点是 `/dev/kvm`、`kata-fc` handler、`firecracker`、`RuntimeClass` |
| Pod `ImagePullBackOff` | 镜像没 push 到 `registry.local`，或仓库/节点拉不到 |
| Pod 一直 `Pending` | 节点没 Ready、资源不够、或 `kata-fc` RuntimeClass 找不到对应 handler |
| 日志 `sidecar not ready within ~10s` | 看 `/state/<id>/logs/sidecar.log`（容器内），多半是依赖没装全 |
| 日志 `LLM endpoint unreachable ...` | `OPENAI_BASE_URL` / `OPENAI_API_KEY` 不对，或 LLM 服务没起 |
| 日志 `openclaw agent failed (exit N)` | 看 `/state/<id>/logs/openclaw.log`：模型名不对、插件问题、超时等 |
| 任务很久不结束 | 检查 LLM 是否可达、`OPENCLAW_TIMEOUT`（默认 300s）是否太小 |

> 想在任何 Pod 里进去手查：`kubectl exec -it <pod> -- sh`（容器是 root，可看
> `/state/<id>/logs/` 和 `/opt/clawtune/`）。
