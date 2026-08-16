# ClawBox 目标机交接报告（2026-08-17）

更新时间：2026-08-17（UTC）  
交接基线：`main` 最新，本 session 推送 `532d7f9`（冒烟修复）、`fc33969`/`28abfba`（check-host 修复）、以及镜像源构建参数 commit（`GOPROXY`/`NPM_REGISTRY`/`PIP_INDEX_URL`）  
上一份背景：`docs/AGENT_HANDOFF_2026-08-16.md`（产品目标、架构、硬边界、bootstrap 机制仍全部有效）  
目标主机：openEuler 24.03 LTS-SP1、Kunpeng/aarch64、单节点 Kubernetes、Kata + Firecracker，`weitianc@193.124.7.2`（hostname `hostname-txyuq.foreman.pxe`）

## 0. 最重要的结论

**FC-0 ~ FC-5 全部在真机通过，substrate 阶段正式收官。** 上一个 session 遗留的所有阻塞（devmapper 元数据、kata hybrid_vsock panic、httpd applet、NetworkPolicy egress、泄漏的数百个 VM、控制面孤儿进程）均已解决并记录于本报告。

当前状态：

- 本地 registry `127.0.0.1:5000` 全链路打通（docker push → `crictl pull` OK）。
- Docker 25.0.3（native arm64）+ Buildx v0.15.1 就绪，镜像构建已从 legacy builder 切换到 BuildKit。
- 控制面在多次 containerd 宕机后已恢复 Running。
- **唯一卡点**：镜像构建的 `go mod download` 访问 `proxy.golang.org` 超时（`dial tcp 142.250.73.81:443: i/o timeout`）。**已把 `GOPROXY`/`NPM_REGISTRY`/`PIP_INDEX_URL` 构建参数支持合入仓库**，下个 session 设环境变量重跑即可。

剩余工作（未完成，不能声称完成）：

1. 构建 3 个控制面镜像（tool-bridge / runtime / control-plane）；
2. 构建 128 个 SWE-ReBench ARM64 任务镜像 + mapping；
3. 部署控制面（CRD/RBAC/ingester/容量 ConfigMap/cell-controller）+ Secrets；
4. SandboxTask 单任务端到端；
5. 1/2/4/8/16/32 并发压测。

## 1. 目标机现状（已验证事实）

- **主机**：openEuler 24.03 (LTS-SP1)，aarch64，128 逻辑核，2.0 TiB RAM（空闲约 1.9 TiB），KVM 可用。
- **Kubernetes**：1.35.7 单节点 control-plane，`kubectl get nodes` = Ready；节点已打 `clawbox.openai.com/firecracker-ready=true`。
- **containerd**：2.3.4，`/etc/containerd/config.toml` 为 **config v4**，`systemctl is-active` = active；devmapper pool `clawbox/fc-pool` 正常；`kata-fc-arm64` handler 正常。
- **Kata/Firecracker**：`/opt/kata` FC-0 审计 **18/18 PASS**（3.31.0 / 1.12.1，dial=1000/reconnect=30000，default_vcpus=2/max=32）。
- **静态 gate**：`deploy/check-host.sh --runtime-class kata-fc-arm64` **19/19 PASS**（普通用户直接跑即可，脚本已 sudo-aware）。
- **live gate**：`scripts/arm64-kata-smoke.sh --runtime-class kata-fc-arm64` **全 PASS**（namespace `clawbox-stage0-26471`：双 VM Ready、Runtime→Tool 经 Service 连通、attacker 隔离、boot ID 不同、≥2 firecracker、无 qemu、devmapper snapshot 回收）。
- **Docker**：25.0.3 native arm64；`/etc/docker/daemon.json` 配了 registry-mirrors（`docker.1panel.live`、`dockerpull.org`）；buildx v0.15.1 装在 `~/.docker/cli-plugins/docker-buildx`。
- **本地 registry**：容器 `clawbox-registry`（`--restart=always`，`-p 127.0.0.1:5000:5000`）运行中；**containerd config 里已存在 `127.0.0.1:5000` 的 `insecure_skip_verify = true` 信任块，不要再 append**。
- **构建源**：
  - ClawTune：`~/ClawTune`，rev `108fb8d7254ba7c13c0f9d5d691ab8bf92adb15e`；
  - 任务 selection：`~/ClawTune/swe_rebench/tasks.json`（128 条，含 `instance_id`/`docker_image`）；
  - SWE-bench fork：`/src/SWE-bench-fork`，pinned `980d0cca8aa4e73f1d9f894e906370bef8c4de8a`；
  - Python：3.12.13。
- **控制面 pod**：etcd / kube-apiserver / kube-controller-manager / kube-scheduler 均 `1/1 Running`；`calico-typha` 仍 `CrashLoopBackOff`（27 次，下个 session 先确认是否自愈；它是 apiserver 下游）；calico-node / kube-proxy / coredns 正常。

## 2. 本 session 完成的修复（均已推送）

### `scripts/arm64-kata-smoke.sh`（commit `532d7f9`）
1. **串行建 Pod**（attacker → runtime → tool，各等 Ready）：修复并发 unpack 导致 devmapper `target snapshot already exists`。
2. **默认镜像 `busybox:1.36`**：alpine:3.22 的 busybox 没有 httpd applet（Exit 127）。
3. **`wait_pod` 失败自动打 logs**。
4. **新增 `tool-egress` NetworkPolicy**（tool→runtime TCP + DNS）：default-deny 会丢 tool 的 egress 响应（SYN-ACK），任何到 tool 的连接都无法完成。
5. **tool 的 readinessProbe 改为 guest 内 exec**（`wget 127.0.0.1:8080 | grep aarch64`）：不依赖 host→VM 回包。

### `deploy/check-host.sh`（commits `fc33969`, `28abfba`）
- 特权探测（/dev/kvm、ctr、containerd config dump、FC-0 audit）走 `host_command`（非 root 时 `sudo -n`），普通用户跑不再误报。
- **坑**：`timeout` 是外部程序，不能调用 shell 函数——必须 `host_command timeout 5 ctr version`（把 timeout 放函数内）。

### Dockerfiles + 构建脚本（本交接 commit）
- `docker/Dockerfile.tool-bridge`：`ARG GOPROXY=https://proxy.golang.org,direct`，`RUN GOPROXY="${GOPROXY}" go mod download`。
- `docker/Dockerfile.runtime`：`ARG NPM_REGISTRY` / `ARG PIP_INDEX_URL`，`npm ci --registry=...`、`npm install -g ... --registry=...`、`pip install --index-url ...`。
- `docker/Dockerfile.control-plane`：`ARG PIP_INDEX_URL`，`pip install --index-url ...`。
- `scripts/build-kubernetes-images.sh`：从环境变量读取并透传这三个参数（默认官方源，行为不变）。**注意：这些改动只在本地（Windows）验证过语法，未在目标机 docker 上跑过。**

## 3. 大事件：泄漏与孤儿进程（下个 session 可能再遇到）

### 3.1 数百个 Kata VM 泄漏
- 现象：`ps` 里 ~490 个 `containerd-shim-kata-v2` + 244 个 `firecracker`，docker run 报 shim `connection refused`。
- 根因：调试期多次失败冒烟 run 的孤儿 sandbox 未回收；`systemctl restart containerd` 时 Kata shim/firecracker **逃逸 systemd cgroup-kill**，永久存活。
- 清理（已验证）：`crictl rmp -f`（非 kube-system 的 sandbox）→ `pkill -9 -f containerd-shim-kata-v2` + `pkill -9 -f jailer` + `pkill -9 -f firecracker`。
- 教训：冒烟 gate 失败路径必须保证 sandbox/VM 回收。

### 3.2 控制面孤儿进程（CrashLoopBackOff）
- 现象：etcd/apiserver 等 CrashLoopBackOff，etcd 报 `listen tcp 193.124.7.2:2380: bind: address already in use`；但 `kubectl` 仍能用（孤儿 apiserver 还在服务）。
- 根因：containerd 宕机时 runc shim 逃逸 cgroup-kill，**最早的 etcd/apiserver 进程变孤儿、仍占 2379/2380/6443**；kubelet 反复拉起新容器但绑不上端口。
- 修复（已验证）：`pkill -9 -f 'kube-controller-manager'`（**进程名>15字符必须 `-f`**）+ `kill -9 <旧 shim PID>` + `crictl rm` 旧容器 + `systemctl restart kubelet`。恢复后 etcd/apiserver/controller-manager/scheduler 全部 1/1 Running。

### 3.3 containerd config 两连崩
- 现象：append 后 `systemctl restart containerd` 失败、socket 消失，`toml: table ... already exists`。
- 根因：config v4 默认内容**已含** `[plugins."io.containerd.cri.v1".registry]` 及 `configs."127.0.0.1:5000".tls`，append 同名表 = TOML duplicate。
- 教训：改 containerd 配置前先 `grep` 现网配置，改完先 `sudo containerd config dump >/dev/null` 预检再 restart；`ls -t ...bak* | head -1` 可能选到坏备份，要按内容/时间确认。

## 4. 下一个 session 立即行动：控制面镜像构建

```bash
cd ~/ClawBox && git pull

# 网络受限，必须设镜像源（三选一组合，以下为国内镜像）
export GOPROXY=https://goproxy.cn,direct
export NPM_REGISTRY=https://registry.npmmirror.com
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# 如果直连可用，也可以走代理（本机 socks5h://127.0.0.1:1080）：
# export HTTPS_PROXY=socks5h://127.0.0.1:1080
# export NO_PROXY=localhost,127.0.0.1,10.96.0.0/12,192.168.0.0/22,193.124.7.2

REGISTRY=127.0.0.1:5000/clawbox TAG=dev PUSH=1 \
  bash scripts/build-kubernetes-images.sh
```

预期产物（全部推到本地 registry）：`tool-bridge-arm64:dev`、`runtime-arm64:dev`、`control-plane-arm64:dev`；静态二进制落到 `.artifacts/tool-bridge-arm64/tool-bridge`。

可能的后续卡点与对策：
- `npm ci` / `npm install -g openclaw@2026.7.1-2`：npmmirror 是否有该版本；没有则换官方 registry + 代理。
- `pip install`：清华源一般可行。
- `apt-get update`（runtime 镜像）：Debian 源可能慢，必要时加 `--build-arg` 方式换源（当前 Dockerfile 未支持 apt 镜像，可后续加 `APT_MIRROR`）。
- 单独排障（不重跑整脚本）：
  ```bash
  docker build --platform linux/arm64 \
    --build-arg GOPROXY=https://goproxy.cn,direct \
    -f docker/Dockerfile.tool-bridge \
    -t 127.0.0.1:5000/clawbox/tool-bridge-arm64:dev .
  ```
- 验证：`docker image inspect --format '{{.Architecture}}' <img>` = arm64；`file .artifacts/tool-bridge-arm64/tool-bridge` 为 aarch64 ELF。

## 5. 剩余路线图

### 5.1 三个控制面镜像（见第 4 节）

### 5.2 128 个 SWE-ReBench ARM64 镜像
```bash
# 先装工厂依赖（用上面 PIP_INDEX_URL）
python3 -m pip install -e '.[images]'

python3 scripts/build-swe-rebench-arm64.py \
  --dataset-id nebius/SWE-rebench \
  --dataset-revision 4ece23ba02fe8b68858e430134adddfd64d6f0f4 \
  --selection ~/ClawTune/swe_rebench/tasks.json \
  --swebench-root /src/SWE-bench-fork \
  --registry 127.0.0.1:5000/clawbox \
  --mapping /data/swe-rebench-arm64-map.json \
  --tool-bridge-binary .artifacts/tool-bridge-arm64/tool-bridge \
  --push --fail-fast
```
- 无本地 parquet，用 HF 路径（`--dataset-id/--dataset-revision`）；用户已 `export HF_ENDPOINT=https://hf-mirror.com`。
- 契约测试：`/testbed` 可写、sh/git/patch、tool-bridge `--self-test`、依赖/测试命令能启动。
- 产物：mapping JSON，每条含 registry digest + `linux/arm64` platform 证明。

### 5.3 部署控制面
```bash
# 1) 先把两个 manifest 的镜像从占位符改成本地 registry
#    deploy/cell-controller.yaml:  image: registry.example.com/clawbox/control-plane-arm64:dev
#    deploy/trace-ingester.yaml:   image: registry.example.com/clawbox/control-plane-arm64:dev
#    → 127.0.0.1:5000/clawbox/control-plane-arm64:dev（或用真实 digest）

kubectl apply -f deploy/sandboxtask-crd.yaml
kubectl apply -f deploy/control-plane-rbac.yaml
kubectl apply -f deploy/trace-ingester.yaml
python3 scripts/collect-node-capacity.py --configmap | kubectl apply -f -
kubectl apply -f deploy/cell-controller.yaml
```
- Secrets：`clawbox-control-plane`（模板 `deploy/control-plane-secret.example.yaml`）、`clawbox-llm`（模板 `deploy/swe-rebench-secret.example.yaml`，需 llm-api-key / llm-upstream-base-url / llm-model / openclaw-model-ref 四个 key）。
- ingester 生产建议 PostgreSQL（当前 SQLite 仅开发用，无 PVC 会随 Pod 丢失）。

### 5.4 单任务端到端
```bash
TASK_ID=demo TOOL_IMAGE='127.0.0.1:5000/clawbox/swe-rebench-arm64:<task>@sha256:...' \
LLM_EGRESS_CIDR=<llm 出口 CIDR> bash deploy/cell.sh deploy --task demo \
  --tool-image "$TOOL_IMAGE" --problem "..." --llm-egress-cidr <CIDR>
kubectl get sandboxtasks -n clawbox-benchmarks -w
```
- 状态机：Queued → Admitted → ToolStarting → ToolReady → RuntimeRunning → Collecting → Succeeded/Failed/TimedOut → Cleaned。

### 5.5 压测
```bash
bash scripts/scale-swe-rebench.sh --tasks ~/ClawTune/swe_rebench/tasks.json \
  --arm64-map /data/swe-rebench-arm64-map.json --llm-egress-cidr <CIDR>
```
- 阶梯 1/2/4/8/16/32，每阶段前后看 devmapper 压力，任一失败即停。

## 6. 踩坑清单（快速参考）

| 坑 | 现象 | 对策 |
|---|---|---|
| NetworkPolicy 缺 egress | 到 tool 的连接全超时、probe 卡 Ready | tool 必须有 egress 规则（响应是 egress）；探针用 guest 内 exec |
| pkill 进程名>15字符 | `pkill kube-controller-manager` 匹配 0 | 必须 `-f` 匹配完整命令行 |
| 外部程序不能调 shell 函数 | `timeout 5 host_command ...` command not found | `host_command timeout 5 ...` |
| containerd config append 崩 | `toml: table ... already exists`、socket 消失 | 先 grep 现网配置；`config dump` 预检；只加缺失子表 |
| crictl -o json | python 按 list 处理报错 | 返回 `{"items":[...]}` |
| Kata shim 逃逸 cgroup-kill | containerd 重启后孤儿 VM 累积 | 失败路径必须清理；pkill -9 兜底 |
| systemd 重启 containerd 连坐 runc 容器 | 控制面 CrashLoopBackOff | kubelet 自动恢复；孤儿占端口则 pkill + 重启 kubelet |
| docker 用 legacy builder | `--platform=$BUILDPLATFORM` 解析失败 | 装 buildx（~/.docker/cli-plugins） |
| go/npm/pip 源不通 | 构建 RUN 超时/连接失败 | 用 `GOPROXY`/`NPM_REGISTRY`/`PIP_INDEX_URL` 环境变量（已支持） |

## 7. 权威验证记录（2026-08-17）

```text
# FC-0 artifact audit
18 pass, 0 fail
# deploy/check-host.sh（普通用户直接跑）
Summary: 19 pass, 0 warn, 0 fail
# scripts/arm64-kata-smoke.sh（namespace=clawbox-stage0-26471）
namespace=clawbox-stage0-26471
pod/attacker condition met / pod/runtime condition met / pod/tool condition met
PASS runtimeClass=kata-fc-arm64 runtime_arch=aarch64 tool_arch=aarch64
PASS Runtime -> Tool networking and attacker -> Tool isolation
PASS distinct guest boot IDs
PASS static sizing, guest-local emptyDir, and no hostPath/PVC storage
PASS Firecracker host processes and containerd launch evidence
PASS namespace cleanup and active devmapper snapshot reclamation
# 本地 registry E2E
docker push 127.0.0.1:5000/clawbox/test:busybox  →  crictl pull OK
# 控制面（修复孤儿后）
etcd / kube-apiserver / kube-controller-manager / kube-scheduler = 1/1 Running
```

## 8. 备注

- 仓库本地测试：`python3 -m pytest`（27 passed 基线）应在改动后仍绿；Dockerfile 改动只被一个断言锁定（`Dockerfile.tool-bridge` 含 `CGO_ENABLED=0 GOOS=linux GOARCH=arm64`，已保留）。
- 如果下个 session 重启 containerd：务必确认控制面在 ~60-90s 内恢复，且用 `ss -ltnp | grep -E ':(2379|2380|6443)'` 检查没有孤儿占端口。
- `stage0-passed` bootstrap 标记：本 session 是手动跑 gate，未走 bootstrap apply（apply 在已建 devmapper 上重跑会因 owned-pool 检查失败），不创建该标记不影响后续手动流程。
