# ClawBox

ClawBox 把 OpenClaw 与 ClawTune 交付到 Kubernetes。每个租户由两个 Pod 组成，
两个 Pod 都通过 `kata-fc` RuntimeClass 运行在独立的 Kata Containers +
Firecracker microVM 中——这是本仓库唯一支持的部署方式。

这个仓库不包含模型，也不会部署 LLM。OpenClaw 通过 HTTP 调用一个外部的
OpenAI-compatible LLM endpoint。

## 架构

```text
                         external LLM API
                                ^
                                | HTTPS（固定 egress）
                                |
  +-----------------------------+-----------------------------+
  | Runtime Pod = Firecracker microVM (kata-fc)                |
  |                                                           |
  |  OpenClaw Gateway/runtime                                 |
  |  ClawTune plugin                                          |
  |  ClawTune scheduler sidecar (127.0.0.1)                   |
  +----------------------------+------------------------------+
                               |
                               | SSH, tenant-specific Service
                               v
  +-----------------------------------------------------------+
  | Tool Pod = Firecracker microVM (kata-fc)                  |
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

- Linux，`/dev/kvm` 可用；
- 一个可用的 Kubernetes 集群（containerd 运行时）；
- 节点上已安装 Kata Containers + Firecracker，且 containerd 配置了
  `kata-fc` runtime handler（见 `deploy/runtimeclass.yaml` 注释）；
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
  ClawBox/
```

下面所有命令都在 ClawBox 根目录执行。

## 第一步：检查宿主机

部署前先做只读自检（不改系统），重点确认 KVM、Kata/Firecracker、`kata-fc`
handler 和 RuntimeClass：

```bash
bash deploy/check-host.sh
```

看到 `FAIL` 先解决，看到 `WARN` 一般可忽略。

## 第二步：构建并推送镜像

Docker 不会可靠地跟随指向构建上下文外部的软链接，因此不要创建
`clawtune -> ../ClawTune`。构建命令保留当前仓库作为主 context，并用 BuildKit
named context 单独传入 ClawTune 源码：

```bash
test -f ../ClawTune/packages/openclaw-plugin/package.json
test -f ../ClawTune/packages/openclaw-plugin/package-lock.json
test -f ../ClawTune/packages/openclaw-plugin/README.md
test -f ../ClawTune/services/scheduler/pyproject.toml

export RUNTIME_IMAGE=registry.example.com/claw/runtime:latest
export TOOL_IMAGE=registry.example.com/claw/tool:latest

docker build -f docker/Dockerfile.runtime \
  --build-context clawtune=../ClawTune \
  -t "$RUNTIME_IMAGE" .

docker build -f docker/Dockerfile.tool-sandbox -t "$TOOL_IMAGE" .

docker push "$RUNTIME_IMAGE"
docker push "$TOOL_IMAGE"
```

`registry.example.com` 只是文档占位符，请替换成节点能访问、且当前用户有 push
权限的真实镜像仓库。两个镜像的职责不同：

- Runtime 镜像包含 OpenClaw、ClawTune plugin 和本地 scheduler sidecar；
- Tool 镜像只包含非 root SSH executor 和 shell/文件工具需要的基础命令；
- Tool 镜像不包含 LLM key、OpenClaw token 或租户业务凭据。

## 第三步：创建 namespace 和 LLM Secret

以下示例创建租户 `tenant-a`。租户 ID 只能使用小写字母、数字和 `-`，最长
40 个字符：

```bash
export NAMESPACE=agents
export TENANT_ID=tenant-a
export LLM_SECRET=tenant-a-llm

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml |
  kubectl apply -f -
```

LLM Secret 必须包含三个 key：`openai-base-url`、`openai-api-key`、
`openclaw-model`。不要把 API key 直接写入 manifest 或提交到仓库：

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
必须告诉脚本 Runtime 可以连接哪个 LLM CIDR 和端口。推荐给 LLM 配置固定出口
IP，或让 Runtime 访问一个固定地址的 egress proxy。例如 LLM 地址是
`203.0.113.10:443`：

```bash
export LLM_EGRESS_CIDR=203.0.113.10/32
export LLM_EGRESS_PORT=443
```

不要使用 `0.0.0.0/0` 或 `::/0`；部署脚本会拒绝这两个默认路由。若域名对应
多个或经常变化的 IP，应使用稳定的 egress proxy。

## 第五步：先渲染并检查 manifest

`render` 不访问 Kubernetes，也不会创建资源：

```bash
bash deploy/cell.sh render \
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

两个 Pod 固定使用 `kata-fc` RuntimeClass。`deploy` 会先确认 RuntimeClass
存在，缺失时自动 apply `deploy/runtimeclass.yaml`：

```bash
bash deploy/cell.sh deploy \
  --namespace "$NAMESPACE" \
  --tenant "$TENANT_ID" \
  --runtime-image "$RUNTIME_IMAGE" \
  --tool-image "$TOOL_IMAGE" \
  --llm-secret "$LLM_SECRET" \
  --llm-egress-cidr "$LLM_EGRESS_CIDR" \
  --llm-egress-port "$LLM_EGRESS_PORT"
```

最简单的 smoke/demo 部署可以不传 `--ssh-secret`：脚本会在临时目录生成一组
租户专属 SSH key，创建 `claw-<tenant>-ssh` Secret，然后立即删除本地文件。
重复执行同一命令会更新同一租户的 Deployment，并复用已有 demo SSH Secret。

生产环境应提前创建 SSH Secret，并通过 `--ssh-secret` 引用。完整 key 格式和
创建命令见 [deploy/README.md](deploy/README.md#secrets)。

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

预期会看到两个不同的 Pod：`claw-tenant-a-runtime-...` 和
`claw-tenant-a-tool-...`。两个 Pod 的 `runtimeClassName` 都应为 `kata-fc`，
宿主机上能看到对应的 Firecracker 进程。

## 第八步：运行 smoke test

完整的跨租户测试需要两个 tenant cell。可以复用同一个 LLM Secret，但每个
租户会拥有独立的 SSH Secret、Deployment、Service 和 workspace：

```bash
bash deploy/cell.sh deploy \
  --namespace "$NAMESPACE" \
  --tenant tenant-b \
  --runtime-image "$RUNTIME_IMAGE" \
  --tool-image "$TOOL_IMAGE" \
  --llm-secret "$LLM_SECRET" \
  --llm-egress-cidr "$LLM_EGRESS_CIDR" \
  --llm-egress-port "$LLM_EGRESS_PORT"

bash deploy/smoke-test.sh \
  --namespace "$NAMESPACE" \
  --tenant tenant-a \
  --other-tenant tenant-b
```

Smoke test 会验证：

1. Runtime 和 Tool 是两个不同的 Pod；
2. 两个 Pod 的 runtimeClassName 都是 `kata-fc`；
3. hostname、PID namespace 和 filesystem 不同；
4. OpenClaw 的 shell 输出来自 Tool Pod；
5. 工具创建的文件只出现在 Tool workspace；
6. `read/write/edit/apply_patch` 确实经过远程 backend；
7. 两个 Pod 都没有 Docker socket；
8. Tool Pod 没有 LLM key 或 OpenClaw 长期 token；
9. `tenant-a` Runtime 无法连接 `tenant-b` Tool Service。

该测试会真实调用 LLM，因此 LLM 必须可达，且模型需要具备可靠的工具调用
能力。

## 删除租户

```bash
bash deploy/cell.sh delete \
  --namespace "$NAMESPACE" \
  --tenant tenant-a
```

删除操作会清理该 tenant ID 的 Deployment、Service、NetworkPolicy，以及由
脚本生成并打了标记的 demo SSH Secret。它不会删除：

- LLM Secret；
- 通过 `--ssh-secret` 引用的生产 SSH Secret；
- 其他 tenant ID 的资源；
- `kata-fc` RuntimeClass。

Runtime 和 Tool workspace 当前都是 `emptyDir`，删除 Pod/Deployment 后其中的
文件不能恢复。

## Tool Pod 需要访问互联网怎么办？

Tool egress 默认完全关闭。如果某个任务确实需要下载依赖或访问固定服务，可
显式放行一个经过审查的 CIDR 和端口：

```bash
bash deploy/cell.sh deploy ... \
  --tool-egress-cidr 198.51.100.20/32 \
  --tool-egress-port 443
```

这不是域名 allowlist。目标 IP 漂移时仍应使用稳定的 egress proxy。

## 本地静态验证

没有 Kubernetes 集群时，仍可检查 manifest：

```bash
python deploy/test_render.py

bash -n \
  deploy/cell.sh \
  deploy/smoke-test.sh \
  deploy/check-host.sh \
  scripts/runtime-entrypoint.sh \
  scripts/tool-entrypoint.sh \
  scripts/tool-command.sh
```

`test_render.py` 需要 PyYAML。

## 常见问题

### Pod 一直处于 Pending

检查节点资源、镜像拉取权限以及 `kata-fc` RuntimeClass / containerd handler：

```bash
kubectl -n "$NAMESPACE" describe pod <pod-name>
kubectl get runtimeclass
bash deploy/check-host.sh
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
  cell.yaml                     Runtime + Tool 两个 Deployment（均 kata-fc）
  cell.sh                       render/deploy/delete 唯一入口
  runtimeclass.yaml             kata-fc RuntimeClass（deploy 自动 apply）
  check-host.sh                 Kata/Firecracker 宿主机自检
  smoke-test.sh                 真实集群隔离测试
  test_render.py                本地 YAML/安全断言
  openclaw-sandbox.example.json OpenClaw sandbox 配置参考
  README.md                     部署细节、Secret 与边界说明
docker/
  Dockerfile.runtime            Runtime 镜像
  Dockerfile.tool-sandbox       Tool 镜像
scripts/
  runtime-entrypoint.sh         Runtime 容器入口
  tool-entrypoint.sh            Tool 容器入口（sshd）
  tool-command.sh               Tool SSH ForceCommand
```

更详细的边界、生产 SSH Secret 和 ClawTune 说明见
[deploy/README.md](deploy/README.md)。
