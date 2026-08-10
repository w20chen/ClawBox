# claw-k8s

`claw-k8s` 是 ClawTune 的 Kubernetes 交付仓库。它负责把 OpenClaw、ClawTune
plugin、ClawTune scheduler sidecar，以及 OpenClaw 使用的工具执行环境部署到
Kubernetes。

这个仓库不包含模型，也不会部署 LLM。OpenClaw 通过 HTTP 调用一个外部的
OpenAI-compatible LLM endpoint。

## 我应该使用哪种模式？

仓库目前保留两种互不覆盖的部署模式：

| 模式 | 适用场景 | 每个租户的结构 | 生命周期 |
| --- | --- | --- | --- |
| `deploy/two-sandbox` | 新部署、租户隔离、验证远程工具执行 | 一个 Runtime Pod + 一个 Tool Pod | 长期运行的 Deployment |
| `deploy/kata-firecracker` | 旧版批量实验、一次性 benchmark、验证 Kata/Firecracker | 一个 Pod 内同时运行 OpenClaw、ClawTune 和工具 | 执行一次任务后结束的 Indexed Job |

如果你只是想把当前版本部署起来，请使用 **`two-sandbox`**。它是现在的主路径。

只有在以下情况下才使用 `kata-firecracker`：

- 你需要复现原有的一体化运行方式；
- 你已经在节点上安装并配置了 Kata Containers + Firecracker；
- 你要并发执行一批一次性任务，而不是维护长期租户实例。

## 双沙盒模式是什么？

每个租户拥有一组独立资源：

```text
                         external LLM API
                                ^
                                |
                                | HTTPS
                                |
  +-----------------------------+-----------------------------+
  | Runtime Pod                                               |
  |                                                           |
  |  OpenClaw Gateway/runtime                                 |
  |  ClawTune plugin                                          |
  |  ClawTune scheduler sidecar (127.0.0.1)                   |
  +----------------------------+------------------------------+
                               |
                               | SSH, tenant-specific Service
                               v
  +-----------------------------------------------------------+
  | Tool Pod                                                  |
  |                                                           |
  |  non-root SSH executor                                    |
  |  independent workspace, PID namespace and root filesystem |
  +-----------------------------------------------------------+
```

OpenClaw 的以下工具会通过内置 SSH sandbox backend 在 Tool Pod 中执行：

- `exec`、`process`
- `read`、`write`、`edit`、`apply_patch`
- sandbox-relative media 文件读取

以下能力不在 Tool Pod 中执行：

- `browser`：当前 OpenClaw SSH backend 不支持 sandbox browser；
- `web_search`、`web_fetch`：由 Runtime 或外部 provider 执行；
- 模型推理、消息、session/subagent control、memory indexing；
- 未经单独审查的 plugin/MCP tools。

Runtime 和 Tool 都不挂载 Docker socket，不使用 privileged，也不自动挂载
Kubernetes ServiceAccount token。

## 前置条件

部署主机需要：

- Linux；
- 一个可用的 Kubernetes 集群；
- `kubectl`，并且当前 context 指向目标集群；
- Docker 或其他能构建 OCI 镜像的工具；
- `envsubst`：Debian/Ubuntu 安装 `gettext-base`，RHEL/openEuler 安装 `gettext`；
- 一个集群节点可以访问的镜像仓库；
- 一个 OpenAI-compatible LLM endpoint；
- `ClawTune` 源码 checkout。

集群的 CNI 必须支持 Kubernetes `NetworkPolicy`。如果 CNI 不实现
NetworkPolicy，manifest 虽然可以创建，但租户网络隔离不会真正生效。

推荐让两个仓库处于同一级目录：

```text
/work/
  ClawTune/
  claw-k8s/
```

下面所有命令都在 `claw-k8s` 根目录执行。

## 第一步：检查源码目录

Docker 不会可靠地跟随指向构建上下文外部的软链接，因此不要创建
`clawtune -> ../ClawTune`。下面的构建命令保留当前仓库作为主 context，并用
BuildKit named context 单独传入 ClawTune 源码。

确认两个仓库互为同级目录，并且所需文件存在：

```bash
test -f ../ClawTune/packages/openclaw-plugin/package.json
test -f ../ClawTune/packages/openclaw-plugin/package-lock.json
test -f ../ClawTune/packages/openclaw-plugin/README.md
test -f ../ClawTune/services/scheduler/pyproject.toml
```

这些 `test` 命令成功时不会打印任何内容。`ClawTune` 是源码目录，不是命令，
不要在 shell 中直接执行 `clawtune`。

## 第二步：构建并推送双沙盒镜像

先把下面两个值替换成节点能够访问、且当前用户有 push 权限的真实镜像仓库。
`registry.example.com` 只是文档占位符，不能直接使用：

```bash
export RUNTIME_IMAGE=registry.example.com/claw/runtime:two-sandbox
export TOOL_IMAGE=registry.example.com/claw/tool:two-sandbox

docker build -f docker/Dockerfile.runtime \
  --build-context clawtune=../ClawTune \
  -t "$RUNTIME_IMAGE" .

docker build -f docker/Dockerfile.tool-sandbox -t "$TOOL_IMAGE" .

docker push "$RUNTIME_IMAGE"
docker push "$TOOL_IMAGE"
```

两个镜像的职责不同：

- Runtime 镜像包含 OpenClaw、ClawTune plugin 和本地 scheduler sidecar；
- Tool 镜像只包含非 root SSH executor 和 shell/文件工具需要的基础命令；
- Tool 镜像不包含 LLM key、OpenClaw token 或租户业务凭据。

## 第三步：创建 namespace 和 LLM Secret

以下示例创建租户 `tenant-a`。租户 ID 只能使用小写字母、数字和 `-`，最长
40 个字符。

```bash
export NAMESPACE=agents
export TENANT_ID=tenant-a
export LLM_SECRET=tenant-a-llm

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml |
  kubectl apply -f -
```

LLM Secret 必须包含三个 key：

- `openai-base-url`：例如 `https://llm.example.com/v1`；
- `openai-api-key`；
- `openclaw-model`：OpenClaw 使用的模型 ID。

不要把 API key 直接写入 manifest 或提交到仓库：

```bash
export OPENAI_BASE_URL=https://llm.example.com/v1
export OPENAI_API_KEY='replace-me'
export OPENCLAW_MODEL='replace-me'

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

printf '%s' "$OPENAI_BASE_URL" >"$tmp_dir/openai-base-url"
printf '%s' "$OPENAI_API_KEY" >"$tmp_dir/openai-api-key"
printf '%s' "$OPENCLAW_MODEL" >"$tmp_dir/openclaw-model"

kubectl -n "$NAMESPACE" create secret generic "$LLM_SECRET" \
  --from-file=openai-base-url="$tmp_dir/openai-base-url" \
  --from-file=openai-api-key="$tmp_dir/openai-api-key" \
  --from-file=openclaw-model="$tmp_dir/openclaw-model" \
  --dry-run=client -o yaml |
  kubectl -n "$NAMESPACE" apply -f -
```

## 第四步：确定 LLM egress CIDR

标准 Kubernetes NetworkPolicy 不能按域名放行，只能按 IP/CIDR 放行。部署时
必须告诉脚本 Runtime 可以连接哪个 LLM CIDR 和端口。

推荐方式是给 LLM 配置一个固定出口 IP，或者让 Runtime 访问一个固定地址的
egress proxy。例如 LLM 地址是 `203.0.113.10:443`，则使用：

```bash
export LLM_EGRESS_CIDR=203.0.113.10/32
export LLM_EGRESS_PORT=443
```

不要使用 `0.0.0.0/0` 或 `::/0`；部署脚本也会拒绝这两个默认路由。若域名
对应多个或经常变化的 IP，应使用稳定的 egress proxy，而不是手工维护易漂移
的 `/32` 列表。

## 第五步：先渲染并检查 manifest

`render` 不访问 Kubernetes，也不会创建资源：

```bash
bash deploy/two-sandbox/cell.sh render \
  --namespace "$NAMESPACE" \
  --tenant "$TENANT_ID" \
  --runtime-image "$RUNTIME_IMAGE" \
  --tool-image "$TOOL_IMAGE" \
  --llm-secret "$LLM_SECRET" \
  --llm-egress-cidr "$LLM_EGRESS_CIDR" \
  --llm-egress-port "$LLM_EGRESS_PORT" \
  >"${TENANT_ID}.rendered.yaml"

kubectl apply --dry-run=client -f "${TENANT_ID}.rendered.yaml"
```

渲染文件会引用 Secret 名称，但不会包含 Secret 内容。

## 第六步：部署一个租户

最简单的 smoke/demo 部署可以不传 `--ssh-secret`。脚本会在临时目录生成一组
租户专属 SSH key，创建 `claw-<tenant>-ssh` Secret，然后立即删除本地文件：

```bash
bash deploy/two-sandbox/cell.sh deploy \
  --namespace "$NAMESPACE" \
  --tenant "$TENANT_ID" \
  --runtime-image "$RUNTIME_IMAGE" \
  --tool-image "$TOOL_IMAGE" \
  --llm-secret "$LLM_SECRET" \
  --llm-egress-cidr "$LLM_EGRESS_CIDR" \
  --llm-egress-port "$LLM_EGRESS_PORT"
```

重复执行同一命令会更新同一租户的 Deployment，并复用已有 demo SSH Secret；
不会轮换密钥，也不会覆盖其他 tenant ID 的资源。

生产环境应提前创建 SSH Secret，并通过 `--ssh-secret` 引用。完整 key 格式和
创建命令见 [双沙盒详细文档](deploy/two-sandbox/README.md#secrets)。

## 第七步：查看状态和日志

```bash
kubectl -n "$NAMESPACE" get deployment,pod,service,networkpolicy \
  -l "claw.openai.com/tenant-id=$TENANT_ID" -o wide

kubectl -n "$NAMESPACE" rollout status \
  "deployment/claw-${TENANT_ID}-tool" --timeout=180s

kubectl -n "$NAMESPACE" rollout status \
  "deployment/claw-${TENANT_ID}-runtime" --timeout=300s

kubectl -n "$NAMESPACE" logs \
  "deployment/claw-${TENANT_ID}-tool" --tail=100

kubectl -n "$NAMESPACE" logs \
  "deployment/claw-${TENANT_ID}-runtime" --tail=100
```

预期会看到两个不同的 Pod：

- `claw-tenant-a-runtime-...`
- `claw-tenant-a-tool-...`

Tool Service 名为 `claw-tenant-a-tool`，只允许同一 tenant ID 的 Runtime Pod
访问 SSH 端口 `2222`。

## 第八步：运行 smoke test

完整的跨租户测试需要两个 tenant cell。可以复用同一个 LLM Secret，但每个
租户会拥有独立的 SSH Secret、Deployment、Service 和 workspace：

```bash
bash deploy/two-sandbox/cell.sh deploy \
  --namespace "$NAMESPACE" \
  --tenant tenant-b \
  --runtime-image "$RUNTIME_IMAGE" \
  --tool-image "$TOOL_IMAGE" \
  --llm-secret "$LLM_SECRET" \
  --llm-egress-cidr "$LLM_EGRESS_CIDR" \
  --llm-egress-port "$LLM_EGRESS_PORT"

bash deploy/two-sandbox/smoke-test.sh \
  --namespace "$NAMESPACE" \
  --tenant tenant-a \
  --other-tenant tenant-b
```

Smoke test 会验证：

1. Runtime 和 Tool 是两个不同的 Pod；
2. hostname、PID namespace 和 filesystem 不同；
3. OpenClaw 的 shell 输出来自 Tool Pod；
4. 工具创建的文件只出现在 Tool workspace；
5. `read/write/edit/apply_patch` 确实经过远程 backend；
6. 两个 Pod 都没有 Docker socket；
7. Tool Pod 没有 LLM key 或 OpenClaw 长期 token；
8. `tenant-a` Runtime 无法连接 `tenant-b` Tool Service。

该测试会真实调用 LLM，因此 LLM 必须可达，且模型需要具备可靠的工具调用
能力。

## 删除租户

```bash
bash deploy/two-sandbox/cell.sh delete \
  --namespace "$NAMESPACE" \
  --tenant tenant-a
```

删除操作会清理该 tenant ID 的 Deployment、Service、NetworkPolicy，以及由
脚本生成并打了标记的 demo SSH Secret。它不会删除：

- LLM Secret；
- 通过 `--ssh-secret` 引用的生产 SSH Secret；
- 其他 tenant ID 的资源。

Runtime 和 Tool workspace 当前都是 `emptyDir`，删除 Pod/Deployment 后其中的
文件不能恢复。

## RuntimeClass 怎么选择？

不传 RuntimeClass 参数时，两个 Pod 都使用集群默认 runtime，通常是 runc：

```bash
bash deploy/two-sandbox/cell.sh deploy ...
```

Runtime 和 Tool 可以分别指定：

```bash
bash deploy/two-sandbox/cell.sh deploy ... \
  --runtime-class kata-fc \
  --tool-runtime-class gvisor
```

也可以都使用已有的 `kata-fc`：

```bash
bash deploy/two-sandbox/cell.sh deploy ... \
  --runtime-class kata-fc \
  --tool-runtime-class kata-fc
```

脚本只引用 RuntimeClass，不负责安装 runc、gVisor、Kata 或 Firecracker。先用
下面的命令确认名称存在：

```bash
kubectl get runtimeclass
```

## Tool Pod 需要访问互联网怎么办？

Tool egress 默认完全关闭。如果某个任务确实需要下载依赖或访问固定服务，可
显式放行一个经过审查的 CIDR 和端口：

```bash
bash deploy/two-sandbox/cell.sh deploy ... \
  --tool-egress-cidr 198.51.100.20/32 \
  --tool-egress-port 443
```

这不是域名 allowlist。目标 IP 漂移时仍应使用稳定的 egress proxy。

## ClawTune 在双沙盒中的限制

ClawTune sidecar 仍在 Runtime Pod 内本地运行，可以保留：

- tool lifecycle hooks；
- trace correlation；
- LLM proxy；
- advisory prediction。

但它不能直接观察另一个 Tool Pod 的 PID/cgroup，所以当前版本：

- 使用 `executionBackend: hook-only`；
- 不伪造 Tool Pod 的资源数据；
- resource scope 记录为 absent/unattributed；
- Stage-2 telemetry 记录为 unavailable；
- cgroup、affinity、NUMA 和 eBPF 测量在这个模式下关闭。

远程 telemetry agent、warm pool、Operator/CRD 和自动扩缩容都不属于当前
MVP。

## 旧版 Kata/Firecracker 一体化模式

旧模式没有被双沙盒模式替换。它使用 Kubernetes Indexed Job，每个 Pod/VM
中同时运行 OpenClaw、ClawTune 和工具，执行一次任务后退出。

先检查主机：

```bash
bash deploy/kata-firecracker/check-host.sh
```

构建旧 runner 镜像：

```bash
export RUNNER_IMAGE=registry.example.com/claw/runner:latest
docker build -f docker/Dockerfile.runner \
  --build-context clawtune=../ClawTune \
  -t "$RUNNER_IMAGE" .
docker push "$RUNNER_IMAGE"
```

运行一次任务：

```bash
IMAGE="$RUNNER_IMAGE" \
TASK_MESSAGE='Use the shell to print claw-cloud-ok' \
OPENAI_BASE_URL="$OPENAI_BASE_URL" \
OPENAI_API_KEY="$OPENAI_API_KEY" \
OPENCLAW_MODEL="$OPENCLAW_MODEL" \
PARALLELISM=1 \
COMPLETIONS=1 \
bash deploy/kata-firecracker/run.sh
```

详细宿主机要求和排错见
[Kata/Firecracker 文档](deploy/kata-firecracker/README.md)。

## 本地静态验证

没有 Kubernetes 集群时，仍可检查双沙盒 manifest：

```bash
python deploy/two-sandbox/test_render.py

bash -n \
  deploy/two-sandbox/cell.sh \
  deploy/two-sandbox/smoke-test.sh \
  scripts/two-sandbox/runtime-entrypoint.sh \
  scripts/two-sandbox/tool-entrypoint.sh \
  scripts/two-sandbox/tool-command.sh
```

`test_render.py` 需要 PyYAML。

## 常见问题

### Pod 一直处于 Pending

检查节点资源、镜像拉取权限以及指定的 RuntimeClass 是否存在：

```bash
kubectl -n "$NAMESPACE" describe pod <pod-name>
kubectl get runtimeclass
```

### Runtime Pod 不 Ready

先看 Runtime 日志，再看容器内的 sidecar/onboard 日志：

```bash
kubectl -n "$NAMESPACE" logs deployment/claw-tenant-a-runtime
kubectl -n "$NAMESPACE" exec deployment/claw-tenant-a-runtime -- \
  find /state/tenant-a/logs -maxdepth 1 -type f -print
```

常见原因是 LLM Secret key 缺失、LLM egress CIDR/端口错误、模型 ID 错误，
或者 Tool SSH Secret 格式不正确。

### Tool Pod 不 Ready

```bash
kubectl -n "$NAMESPACE" logs deployment/claw-tenant-a-tool
kubectl -n "$NAMESPACE" describe pod \
  -l 'app.kubernetes.io/component=tool,claw.openai.com/tenant-id=tenant-a'
```

重点检查 SSH Secret 是否包含客户端和 host 两组 Ed25519 keypair。

### LLM 域名有多个 IP

不要把 Runtime egress 放宽到整个互联网。使用固定出口 IP 的 LLM gateway 或
egress proxy，然后只放行该 proxy 的 CIDR/端口。

### Tool 中无法联网

这是默认安全行为。只有明确需要时才使用 `--tool-egress-cidr`，并尽量使用
`/32` 和单一端口。

## 目录结构

```text
deploy/
  two-sandbox/
    cell.yaml                    双沙盒资源模板
    cell.sh                      render/deploy/delete 入口
    smoke-test.sh                真实集群隔离测试
    test_render.py               本地 YAML/安全断言
    openclaw-sandbox.example.json
    README.md                    双沙盒设计和 Secret 细节
  kata-firecracker/
    job.yaml                     旧版 Indexed Job
    runtimeclass.yaml            kata-fc RuntimeClass 示例
    run.sh                       旧版一键运行入口
    check-host.sh                Kata/Firecracker 宿主机检查
docker/
  Dockerfile.runtime             双沙盒 Runtime 镜像
  Dockerfile.tool-sandbox        双沙盒 Tool 镜像
  Dockerfile.runner              旧版一体化 runner 镜像
scripts/
  two-sandbox/                   双沙盒容器启动脚本
  run-once.sh                    旧版一次性任务入口
```

更详细的双沙盒边界、生产 SSH Secret 和 telemetry 说明见
[deploy/two-sandbox/README.md](deploy/two-sandbox/README.md)。
