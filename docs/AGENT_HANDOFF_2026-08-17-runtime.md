# ClawBox 目标机交接报告（2026-08-17 · 深夜 · Kata 运行时破局）

更新时间：2026-08-17 深夜（UTC）  
交接基线：`main` 最新，本 session 已推送 `0abc1c1`（root 运行）+ `c3ea48e`（去掉容器级提权限制）。  
上一份背景：`docs/AGENT_HANDOFF_2026-08-17.md`（控制面镜像/任务镜像构建、部署、substrate 阶段结论仍全部有效）。  
目标主机：openEuler 24.03 LTS-SP1、aarch64（Kunpeng 128 核/2.0 TiB）、单节点 Kubernetes 1.35.7、Kata 3.31.0 + Firecracker 1.12.1、`weitianc@193.124.7.2`（hostname `hostname-txyuq.foreman.pxe`）。

## 0. 最重要的结论（当前状态）

**Tool pod 的「启动即 exit 1」已彻底修复，cell 首次推进到 `RuntimeRunning`。** 阻塞了多轮的核心问题 = **Kata guest agent 写的 Secret/ConfigMap 卷数据目录权限是 `0000`**，非 root 读不了；root 可行（微 VM 内 root ≠ 宿主机 root，VM 即隔离边界）。修复已合入 `clawbox/cell/manifests.py` 并重新部署。

当前实测状态（本 session 最后一次 run）：

- probe-m（精确复刻新 tool manifest 的独立 pod）：**`1/1 Running`，日志 `tool bridge ready address=0.0.0.0:2222 workdir=/testbed arch=arm64`**。
- cell `single-001`：**`phase=RuntimeRunning`**，`reason=RuntimeJobCreated`，`toolReadyAt=2026-08-17T05:16:05Z`，`runtimeStartedAt=05:16:07Z`。
- runtime job `single-001-runtime-9xxzl`：镜像 `runtime-arm64:dev` 拉取成功（8.8s）、容器 Created + Started。60s 观察窗内尚未到终态。

**预期走向**：runtime entrypoint 会先配好 openclaw/SSH sandbox（指向 tool 的 SSH 服务），随后 agent 尝试连 LLM——而 LLM secret 是占位符，**预期 runtime 会在 LLM 环节失败**（这对并发压测是"正常失败"，我们要验证的是 tool 桥 + runtime 编排本身工作）。下个 session 第一步就是确认这个终态转换是否干净（详见 §3）。

## 1. 本 session 完成的工作（均已推送/部署）

### 1.1 三个控制面镜像构建 + 部署（已完成）
- `127.0.0.1:5000/clawbox/tool-bridge-arm64:dev`（digest `sha256:d451b796…`）
- `127.0.0.1:5000/clawbox/runtime-arm64:dev`（digest `sha256:9b0d4f8b…`）
- `127.0.0.1:5000/clawbox/control-plane-arm64:dev`（最新重建 digest `sha256:fb54b92e…`，含 root 修复）
- CRD（`deploy/sandboxtask-crd.yaml`，已去掉 `additionalProperties:false`）、RBAC、ingester（`/data` emptyDir 挂 SQLite）、cell-controller 全部部署 Running。
- `deploy/cell-controller.yaml` / `trace-ingester.yaml` 镜像已指向 `127.0.0.1:5000/clawbox/...:dev`，`imagePullPolicy: Always`。

### 1.2 首个 SWE-ReBench arm64 任务镜像（已完成，tool-bridge 已打进镜像）
- 任务：`15five__scim2-filter-parser-13`
- **当前使用的镜像**：`127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:bdf4637498a4b765f0e91333ff292c226bd011a31c220d76609daff43e2c39fd`（887MB，**内置 `/usr/local/bin/tool-bridge`**，chown /testbed→10001，PATH 含 testbed conda env）
- 旧镜像 `…@sha256:ef4a5559…` 是**没**内置 bridge 的早期版本（probe-f/g 用过），勿再用于 cell。
- mapping：`/data/swe-rebench-arm64-map.json`；`normalize_harness_image` 的 `recipe_revision` 含 bridge 二进制 sha256。

### 1.3 Kata Secret 卷 root 修复（本 session 最大突破，详见 §2）
- 提交 `0abc1c1`：tool_pod / runtime_job `securityContext` 改为 `runAsUser:0, runAsGroup:0, fsGroup:10001, seccompProfile:RuntimeDefault`，并注入 `GIT_CONFIG_COUNT=1 / GIT_CONFIG_KEY_0=safe.directory / GIT_CONFIG_VALUE_0=*`。
- 提交 `c3ea48e`：**容器级去掉 `allowPrivilegeEscalation:false` 和 `capabilities:{drop:["ALL"]}`**（只留 `readOnlyRootFilesystem`）——否则 root 不生效（见 §2 的 probe-j 陷阱）。

## 2. Kata Secret 卷问题完整档案（probe 证据链）

### 现象演进
- 单容器 pod（Secret/ConfigMap 卷）能起（probe-a/b/c/f/g，`sleep 300`），但 tool-bridge 一跑就 exit 1：`read host key: open /var/run/secrets/tool-ssh/ssh_host_ed25519_key: permission denied`。
- 一度怀疑 key 格式/路径/权限，逐项排查后确认是**文件权限**问题。

### 根因（probe-k/probe-l 决定性证据）
Kata guest agent 在无共享文件系统模式下把 Secret/ConfigMap 卷落成 kubelet 式布局，但**时间戳数据目录权限是 `0000`**：

```
drwxrwsrwt 3 root 10001  140  .            # 挂载点本身 1777，没问题
d--------- 2 root 10001  100  ..2026_08_17_05_08_26.1399962773   # ← 权限 0000！
lrwxrwxrwx 1 root 10001   32  ..data -> ..2026_08_17_05_08_26.1399962773
lrwxrwxrwx 1 root 10001   27  ssh_host_ed25519_key -> ..data/ssh_host_ed25519_key
```

- `..data` 指向的目录 mode=`d---------`（0000），连 group 都没权限 → **uid 10001 无法进入** → EACCES（probe-l 的 `cat` 报 Permission denied）。
- root 走 DAC 旁路，能读（probe-k 的 `cat` 输出 key、EXIT=0）。`fsGroup`/`defaultMode` 都救不了 0000 目录。

### 致命陷阱（probe-j vs probe-k）
- probe-j：pod 级 `runAsUser:0` **但容器级带 `allowPrivilegeEscalation:false` + `capabilities:{drop:["ALL"]}`** → 实际仍以镜像 `USER 10001` 跑 → 依旧 EACCES。
- probe-k：pod 级 `runAsUser:0`、**无容器级 securityContext** → `id` 显示 `uid=0(root)`，读 key 成功。
- 结论：**Kata agent 在禁止提权 + 丢 CAP 时拒绝 setuid 到 0，回退镜像 USER**。要让 root 生效必须同时去掉这两个容器级字段。

### 修复形态（现仓库代码）
- pod 级：`{"runAsUser": 0, "runAsGroup": 0, "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}}`（不再有 `runAsNonRoot`）。
- 容器级：tool `{"readOnlyRootFilesystem": False}`；runtime `{"readOnlyRootFilesystem": True}`。**没有** allowPrivilegeEscalation / capabilities。
- 两个容器都注入 `GIT_CONFIG_COUNT=1, GIT_CONFIG_KEY_0=safe.directory, GIT_CONFIG_VALUE_0=*`：root 跑 git 访问 uid-10001 所有的 `/testbed` 会触发 "dubious ownership"，env 方式全局规避（不依赖 HOME/配置文件）。
- 安全性论证：root 只在 guest 微 VM 内生效，Kata/FC 是隔离边界，guest root 无法逃逸到宿主机；对 benchmark 编排可接受。

### probe 清单（`/tmp/probe-*.yaml` 在目标机 /tmp，仓库外）
| probe | 配置 | 结果 |
|---|---|---|
| e | init+task 共享 emptyDir | 容器 create ENOENT（多容器卷共享在无 shared_fs 的 Kata 上不可用）|
| i | uid 10001 + tool-bridge | exit 1：read host key permission denied |
| j | runAsUser 0 + 容器级禁提权/drop ALL | 仍 permission denied（agent 回退镜像 USER）|
| k | runAsUser 0 + 无容器级 securityContext | `uid=0`，cat key 成功 ← **决定性** |
| l | uid 10001 | cat Permission denied（`..data` 目录 0000）|
| m | 精确复刻新 tool manifest | `tool bridge ready`，Running ← **修复验证** |

## 3. 下一步（下个 session 按序执行）

### 3.1 确认 single-001 的终态（最优先）
```bash
kubectl get sandboxtasks -n clawbox-benchmarks
kubectl get pods -n clawbox-benchmarks -l clawbox.openai.com/cell=single-001
# 若 runtime pod 还在：
kubectl logs single-001-runtime-xxxxx -n clawbox-benchmarks --tail=80
# controller 视角（看 reason / 错误）：
kubectl -n clawbox-system logs deployment/clawbox-cell-controller --tail=100
```
- 期望：runtime 因 LLM 占位符失败 → 状态机 `RuntimeRunning → Collecting → Failed/Cleaned`，reason 形如 `RuntimeFailed`（agent 连不上 LLM 属预期）。
- **红旗**：若 runtime 卡在 Secret/ConfigMap 读取（`/var/run/secrets/tool-ssh/…`、`/prompt/problem_statement` 读不了）→ 那是新的权限问题，先贴日志。

### 3.2 清理调试残留
```bash
kubectl delete pod probe-m -n clawbox-benchmarks --wait=false   # probe-m 仍 Running
kubectl delete secret probe-auth -n clawbox-benchmarks --wait=false
kubectl delete sandboxtask single-001 -n clawbox-benchmarks --wait=false   # 若还在
```

### 3.3 单 cell 干净 E2E（验证 tool→runtime 全链路）
```bash
DIGEST=127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:bdf4637498a4b765f0e91333ff292c226bd011a31c220d76609daff43e2c39fd
bash scripts/single-image-scale.sh --tool-image "$DIGEST" --problem-file /tmp/problem.txt \
  --llm-egress-cidr 100.64.0.0/10 --count 1 --wait-seconds 300
```
- `/tmp/problem.txt` 已在目标机（多次 run 未报缺文件）；若不存在先补。
- 判据：tool 日志 `tool bridge ready`；cell 到 RuntimeRunning；runtime 只在 LLM 环节失败 → 单 cell 编排成立。

### 3.4 并发压测（本 session 的最终目标）
```bash
bash scripts/single-image-scale.sh --tool-image "$DIGEST" --problem-file /tmp/problem.txt \
  --llm-egress-cidr 100.64.0.0/10 --count 4 --wait-seconds 1800 --delete
# 再 8 / 16 / 32
```
- 注意 `single-image-scale.sh` 判成功按 `outcome`（Cleaned+Failed 都算失败）；`--delete` 清 cell。
- devmapper pool `clawbox/fc-pool` 2682.73g、Data% 0.26 —— 空间充足；并发高时留意 Data% 和 `ctr -n k8s.io snapshots --snapshotter devmapper ls` 快照数。

### 3.5 杂项
- **calico-typha 是否自愈未确认**（上上 session 遗留，先 `kubectl -n kube-system get pods` 看）。
- 128 任务镜像构建可并行推进（命令见 §5.3），与并发测试互不阻塞。

## 4. 目标机环境事实（更新到本次）

- 代理：shell 有 `socks5h://127.0.0.1:1080`（github 等被墙源）；**访问集群命令须带 NO_PROXY 或 unset 代理**（否则 kubectl/python client 把 193.124.7.2:6443 丢进 SOCKS → TLS 超时）。
- 本地 registry：`127.0.0.1:5000`（`insecure_skip_verify` 信任块**已存在**，勿再 append 同名表头，会 TOML duplicate 崩 containerd）。
- 镜像构建必须的 env（缺一不可）：
  - `GOPROXY=https://goproxy.cn,direct`
  - `NPM_REGISTRY=https://registry.npmmirror.com`
  - `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`
  - `APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn`（脚本内部剥 scheme 强制 http，node:bookworm-slim 无 CA）
  - SWE-ReBench 管线额外：`HF_ENDPOINT=https://hf-mirror.com`、`CLAWBOX_GIT_PROXY=socks5h://127.0.0.1:1080`（github 走 host 网络）
- **Kata 无共享文件系统**（`shared_fs: None`，virtio-fs 被 FC-0 禁）：Secret/ConfigMap 卷是 agent 在 guest 内落盘（§2 的 0000 权限问题由此而来）；emptyDir 是 guest 本地。
- 控制面镜像 `imagePullPolicy: Always`（:dev 标签防陈旧缓存复用）。
- `ctr -n k8s.io images pull --snapshotter devmapper` 预拉单平台镜像报 "no unpack platforms defined" 是**已知非致命**（单平台 manifest 无 platform 行），single-image-scale.sh 已 best-effort 处理。

## 5. 关键命令速查

### 5.1 重建 control-plane 并热更新（只改 manifests.py 时用）
```bash
cd ~/ClawBox && git pull || git -c http.proxy=socks5h://127.0.0.1:1080 pull
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
docker build --platform linux/arm64 --pull --build-arg PIP_INDEX_URL="$PIP_INDEX_URL" \
  -f docker/Dockerfile.control-plane -t 127.0.0.1:5000/clawbox/control-plane-arm64:dev .
docker push 127.0.0.1:5000/clawbox/control-plane-arm64:dev
kubectl -n clawbox-system rollout restart deployment/clawbox-cell-controller
kubectl -n clawbox-system rollout status deployment/clawbox-cell-controller --timeout=120s
```

### 5.2 确定性诊断（避免 controller 10~16s 清理竞态）
用独立 probe 复刻目标 pod，无清理竞态：
```bash
# 复刻工具：/tmp/probe-m.yaml（root + 无容器级 securityContext + Secret 卷 + 真命令）是现成模板
kubectl apply -f /tmp/probe-m.yaml; sleep 12
kubectl get pod probe-m -n clawbox-benchmarks; kubectl logs probe-m -n clawbox-benchmarks --tail=30
```

### 5.3 构建更多任务镜像
```bash
export HF_ENDPOINT=https://hf-mirror.com PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  CLAWBOX_GIT_PROXY=socks5h://127.0.0.1:1080
python3 -m clawbox.images.swerebench --dataset-id nebius/SWE-rebench \
  --dataset-revision 4ece23ba02fe8b68858e430134adddfd64d6f0f4 \
  --selection <多任务json> --expected-count N --swebench-root /src/SWE-bench-fork \
  --registry 127.0.0.1:5000/clawbox --mapping /data/swe-rebench-arm64-map.json \
  --tool-bridge-binary .artifacts/tool-bridge-arm64/tool-bridge --push --fail-fast
```
（入口必须是 `python3 -m clawbox.images.swerebench`；`scripts/build-swe-rebench-arm64.py` 是 arm64.py 的 `--tasks` CLI，参数不同。）

### 5.4 诊断 kubectl 语法备忘
- `kubectl logs <pod> -n <ns>` / `kubectl describe pod <pod> -n <ns>`（**不要** `kubectl describe pod pod/<name>`，会报 "no need to specify resource type"）。
- 事件：`kubectl -n clawbox-benchmarks get events --sort-by=.lastTimestamp | tail -20`。
- containerd 容器退出：`sudo journalctl -u containerd --since -10m | grep -iE 'exit|kata|shim'`。

## 6. 本 session 提交清单（`main`，全部已推送）

```
c3ea48e cell: drop allowPrivilegeEscalation/drop-ALL on tool+runtime containers   ← 关键
0abc1c1 cell: run tool/runtime as root in Kata microVM (secret volume data dirs are mode 0000) ← 关键
8859f8e deploy: imagePullPolicy Always for control-plane dev tag
6563c22 cells: single-container pods; bake bridge into task image
edad3eb single-image-scale: judge success by outcome, not just phase
11aa577 single-image-scale: auto-dump cell status and events on failure
93980a2 single-image-scale: wait for stale cell deletion to finish
3c725e7 cell: mount shared emptyDirs at identical paths in init and task   ← 已被 6563c22 取代，勿回退
7f59704 single-image-scale: delete stale target-named cells before creating
cfe164e cell: replace Secret/ConfigMap volumes with guest-local emptyDirs   ← 已被 6563c22 取代，勿回退
29167be single-image-scale: make ctr pre-pull best-effort
ef27260 single-image-scale: specify --platform for ctr devmapper pre-pull
4d6c039 ingester: mount emptyDir at /data for the dev SQLite backend
dc9413a CRD: drop additionalProperties:false next to properties
45d82d8 deploy: point control-plane manifests at the local registry images
38e82ec arm64 images: accept single-platform push in push_immutable proof
6093b06 swerebench: expose testbed conda env on PATH for the tool user
92ce8c0 swerebench: opt-in host network + github git proxy for restricted networks
257cbab swerebench: use fork's real API (build_env_images batch, then build_instance_image)
```
架构演进注意：`cfe164e`（emptyDir 方案）和 `3c725e7`（同路径挂载）都是**已被取代**的过渡方案；当前架构 = `6563c22` 单容器 pod + `0abc1c1/c3ea48e` root 运行。

## 7. 本 session 新增坑清单（按重要度）

1. **Kata Secret 卷数据目录权限 0000**（`..data` → `d---------`）：非 root 必 EACCES；`fsGroup`/`defaultMode` 无效；唯一正解 = 以 root 跑（§2）。
2. **`allowPrivilegeEscalation:false` + `drop ALL` 会让 Kata agent 忽略 pod 级 `runAsUser:0`**，回退镜像 USER（probe-j 陷阱）。要让 root 生效必须去掉这两个字段。
3. **root 跑 git 的 "dubious ownership"**：任务镜像 `/testbed` 是 uid 10001 所有，root 跑 git 会报错；用 `GIT_CONFIG_COUNT/KEY_n/VALUE_n` env 全局 `safe.directory=*`，不依赖 HOME。
4. **诊断要抢在清理前**：cell 失败后 controller ~10-16s 就 Cleaned 删 pod；`--wait-seconds 0` 时脚本立刻 dump 的是**旧事件**。确定性诊断一律用独立 probe（§5.2）。
5. **kubectl describe 语法**：`kubectl describe pod <name> -n <ns>`，不要 `pod/<name>`。
6. 镜像构建、apt/npm/pip/github 代理、devmapper、CRD 结构校验等坑见 `AGENT_HANDOFF_2026-08-17.md` 与 repo memory，本次不再重复。
