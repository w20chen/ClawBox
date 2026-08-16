# 部署细节：Runtime Pod + Tool Pod（Kata VM）

每个租户拥有一组独立资源：一个 Runtime Pod 和一个 Tool Pod。两个 Pod 都通过
同一个可配置的 Kata RuntimeClass 运行在独立 microVM 中。鲲鹏/openEuler 首轮
默认使用 `kata-qemu`；`kata-fc` 只应在 ARM64 主机门禁通过后启用。

## 边界与支持的工具

Runtime Pod 包含 OpenClaw `2026.7.1-2`、ClawTune plugin 和本地 ClawTune
scheduler sidecar。OpenClaw 使用内置 SSH sandbox backend 连接租户专属的 Tool
Service。Tool Pod 拥有独立的 PID namespace、根文件系统和临时 workspace。
两个 Pod 都不挂载 Docker socket，也不挂载 Kubernetes service-account token。

安装的 OpenClaw schema 把 `exec`、`process` 通过 SSH 执行，`read`、`write`、
`edit`、`apply_patch` 和 sandbox 媒体读取通过远程文件系统桥接执行。SSH
sandbox 使用 Tool Pod 本地的可写 workspace（`workspaceAccess: rw`），不是与
Runtime Pod 共享的挂载。Tool 镜像包含 Python 3 和 `patch`，远程文件系统桥接
需要这两个组件。

第一版刻意拒绝 `browser`：当前 OpenClaw SSH backend 不支持 sandbox browser
容器。`canvas`、`nodes`、`cron`、`gateway` 也一并拒绝，因为它们属于宿主/控制
能力而非 Tool 文件系统命令。plugin/MCP tools 不会被自动隔离，每个都需要单独
审查后才能放行。provider 提供的 `web_search`/`web_fetch`、消息工具、
session/subagent 控制、memory indexing、模型/媒体推理仍然属于 Runtime/Gateway
或外部服务的职责；本模式不声称它们在 Tool Pod 内执行。只有访问
sandbox-relative 媒体文件时才走远程文件系统桥接。

## 构建

保持 `ClawTune` 与 ClawBox 互为同级目录。在 ClawBox 目录下，用 BuildKit
named context 传入 ClawTune 源码；不要创建指向主 build context 外的软链接：

```bash
test -f ../ClawTune/packages/clawtune-plugin/package.json
docker build -f docker/Dockerfile.runtime \
  --build-context clawtune=../ClawTune \
  -t registry.example/claw-runtime:latest .
docker build -f docker/Dockerfile.tool-sandbox \
  -t registry.example/claw-tool:latest .
docker push registry.example/claw-runtime:latest
docker push registry.example/claw-tool:latest
```

`registry.example` 是占位符，构建前替换为真实仓库地址。

## Secrets

LLM Secret 只创建一次，脚本只引用它、从不打印它的内容：

```bash
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
printf '%s' 'https://llm.example/v1' >"$tmp_dir/openai-base-url"
printf '%s' "$OPENAI_API_KEY" >"$tmp_dir/openai-api-key"
printf '%s' 'model-name' >"$tmp_dir/openclaw-model"
kubectl -n agents create secret generic tenant-a-llm \
  --from-file=openai-base-url="$tmp_dir/openai-base-url" \
  --from-file=openai-api-key="$tmp_dir/openai-api-key" \
  --from-file=openclaw-model="$tmp_dir/openclaw-model"
```

生产环境应带外创建租户专属 SSH Secret（客户端 + host 两组 Ed25519 keypair）：

```bash
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
ssh-keygen -q -t ed25519 -N '' -f "$tmp_dir/id_ed25519"
ssh-keygen -q -t ed25519 -N '' -f "$tmp_dir/ssh_host_ed25519_key"
kubectl -n agents create secret generic tenant-a-tool-ssh \
  --from-file=id_ed25519="$tmp_dir/id_ed25519" \
  --from-file=id_ed25519.pub="$tmp_dir/id_ed25519.pub" \
  --from-file=ssh_host_ed25519_key="$tmp_dir/ssh_host_ed25519_key" \
  --from-file=ssh_host_ed25519_key.pub="$tmp_dir/ssh_host_ed25519_key.pub"
```

不带 `--ssh-secret` 时，`cell.sh deploy` 会在临时目录生成 demo key，上传
`claw-<tenant>-ssh`，然后立即删除本地副本。`cell.sh delete` 只删除打了
`claw.openai.com/demo-key=true` 标记的 demo key，绝不会删除通过 `--ssh-secret`
引用的生产 SSH Secret 或 LLM Secret。

## 部署与删除

租户 ID 必须是小写 Kubernetes DNS label，最长 40 字符；非法 ID 会被拒绝，
不会被当作 shell 文本执行。

两个 Pod 使用 `--runtime-class` 指定的现有 RuntimeClass。部署脚本只检查它，
不会创建一个没有对应 containerd handler 的空壳 RuntimeClass：

```bash
bash deploy/cell.sh deploy \
  --namespace agents \
  --tenant tenant-a \
  --runtime-image registry.example/claw-runtime:latest \
  --tool-image registry.example/claw-tool:latest \
  --llm-secret tenant-a-llm \
  --ssh-secret tenant-a-tool-ssh \
  --runtime-class kata-qemu \
  --llm-egress-cidr 203.0.113.10/32 \
  --llm-egress-port 443
```

标准的 NetworkPolicy 不能按 FQDN 放行。把 LLM endpoint 解析为一个稳定且
尽量窄的 CIDR，或者使用受控的 egress proxy，然后显式传入 CIDR。部署入口
拒绝 IPv4 和 IPv6 默认路由（`0.0.0.0/0`、`::/0`）。

Tool egress 默认全部拒绝。经过审查的工作负载可以通过 `--tool-egress-cidr`
和 `--tool-egress-port` 获得一个显式 CIDR/端口：

```bash
bash deploy/cell.sh deploy ... \
  --tool-egress-cidr 198.51.100.20/32 \
  --tool-egress-port 443
```

```bash
bash deploy/cell.sh delete --namespace agents --tenant tenant-a
```

## 验证

```bash
python deploy/test_render.py  # 需要 PyYAML
bash deploy/smoke-test.sh \
  --namespace agents --tenant tenant-a --other-tenant tenant-b
```

Smoke test 会让 OpenClaw 真实执行 shell 和文件工具，并验证：两个不同的 Pod、
两个 Pod 的 runtimeClassName 均为指定值、hostname/PID namespace/文件系统
不同、远程 `exec` 和 `read/write/edit/apply_patch` 生效、没有 Docker socket、
Tool 中没有长期凭据、跨租户 SSH 被拒绝。加 `--verify-delete` 会在测试后删除
该 cell 并验证受管资源清理。

## ClawTune 降级与后续 telemetry 接口

sidecar 仍运行在 Runtime Pod 内（loopback），保留 lifecycle hooks、trace
correlation、LLM proxy 和 advisory prediction。plugin 使用
`executionBackend: hook-only`；cgroup、affinity、NUMA 和 stage-2 eBPF 采集
全部关闭。Runtime sidecar 无法可信地拿到 Tool 的 PID/cgroup 范围，因此现有
行为记录 absent/unattributed 的 scope 和 unavailable 的 stage-2 telemetry，
不伪造任何测量值，也不需要改动公共 schema。

后续可选的 Tool telemetry agent 应接受一次性执行 token、`execution_id`、
tenant/runtime 身份和命令摘要；返回 JSON Schema 定义的 Tool Pod UID 加上
cgroup-v2 路径或 root PID/PID namespace inode，并显式说明可用性原因；然后
按 `execution_id` 返回有界的 CPU、RSS/峰值内存、I/O、退出码和单调时间戳。
公共字段必须先加入 ClawTune JSON Schema，再补 producer、consumer 和兼容性
测试。这些刻意不在 MVP 范围内。
