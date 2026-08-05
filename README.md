# claw-k8s — OpenClaw 多租户一键并发（Kata Containers + Firecracker）

单台 Linux ARM64 上，用 Kubernetes + Kata + Firecracker 并发起多个一次性 OpenClaw
实例。**一个 Pod = 一个完整 ClawTune 实例**（OpenClaw + 插件 + sidecar + 独立临时 KB），
跑一次任务就退出。

## 最快路径（照抄）

前置：宿主机已装好 Kata + Firecracker 的 `kata-fc` handler；ClawTune 源码在
`/path/to/ClawTune`；外部 OpenAI 兼容 LLM 可用。

```bash
# 1) 宿主机自检（只检查不改系统）
./deploy/kata-firecracker/check-host.sh

# 2) 准备构建上下文 + 构建推送镜像
ln -s /path/to/ClawTune clawtune
docker build -f docker/Dockerfile.runner -t registry.local/openclaw-runner:latest .
docker push registry.local/openclaw-runner:latest

# 3) 一键并发跑 4 个（默认就是 4 个并发）
IMAGE=registry.local/openclaw-runner:latest \
TASK_MESSAGE="Use the shell to print claw-cloud-ok" \
OPENAI_BASE_URL=http://<llm-host>:8000/v1 \
OPENAI_API_KEY=<你的key> \
OPENCLAW_MODEL=<模型名> \
./deploy/kata-firecracker/run.sh
```

想先试 1 个：加 `PARALLELISM=1 COMPLETIONS=1`；想跑 8 个：加 `PARALLELISM=8 COMPLETIONS=8`。

完整傻瓜式步骤、每步说明、验证隔离和报错对照表见：
**[deploy/kata-firecracker/README.md](deploy/kata-firecracker/README.md)**

## 目录

```
deploy/kata-firecracker/   # RuntimeClass / Indexed Job / run.sh / check-host.sh / README
docker/Dockerfile.runner   # ARM64 runner 镜像（OpenClaw + 插件 + sidecar + base KB）
scripts/run-once.sh        # 容器入口：一个租户跑一次
```
