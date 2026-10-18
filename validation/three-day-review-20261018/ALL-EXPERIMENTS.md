# 全部实验与探索臂（73 个 arm/attempt）

资格、训练和功能检查在 Other_runs.csv，不混作正式实验。每个条目的窗口吞吐和操作次数都来自统一观察窗口；已失败和取消数据仍保留。全部参数见 Configurations.csv / all-config-fields.csv。

## sqlglot-admission-c160-r2 / default-resident

开始：2026-10-16T09:00:29.248156+08:00；run=cancelled，保存的 arm=timed_out；证据 ID `db5d8a3b12-attempt-8a708bf08bbe4870-fbc275ccf0ba34938204b6c8`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 160 | 1800.00 | 0 | 0.00000 | — | 0.4611 | — | — |

旧日志未分类命令吞吐（替代，非应用 Tool 口径）：1.7689 次/s。

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 13.75/100.07 | 225.88/236.74 | 225.88/236.74 | — | 0/0/0 | 0 | 1.07 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 1152.00 | —/— | 1152.00 | 0.00 | — | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=off，idle=none/None，borrow=None，snapshot=incremental-cow/tiered，placement=os_default，runtime=off。

解释：CPU 采样峰值超过记录 CPU 容量，保留异常值，不能当作可信瞬时利用率。 存在未标注 execution_scope 的旧 tool_completed；未分类命令吞吐作为替代，不能全算应用 Tool。

保存的失败原因：`arm exceeded hard timeout of 1800 seconds`

配置：`/home/weitianc/clawbox-baseline-audit-20261016-a91-v4/sqlglot-results/sqlglot-admission-c160-r2/experiment.yaml`

事件：`/home/weitianc/clawbox-baseline-audit-20261016-a91-v4/sqlglot-results/sqlglot-admission-c160-r2/attempts/attempt-8a708bf08bbe4870/fbc275ccf0ba34938204b6c8/events/fbc275ccf0ba34938204b6c8.jsonl`

结果：`/home/weitianc/clawbox-baseline-audit-20261016-a91-v4/sqlglot-results/sqlglot-admission-c160-r2/arms/fbc275ccf0ba34938204b6c8.json`

## sqlglot-admission-c160-r2 / predicted-resident

开始：2026-10-16T09:33:16.240148+08:00；run=cancelled，保存的 arm=running；证据 ID `db5d8a3b12-attempt-8a708bf08bbe4870-9a707ad43baa952add85cdb5`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 160 | 107.79 | 0 | 0.00000 | — | 0.3618 | — | — |

旧日志未分类命令吞吐（替代，非应用 Tool 口径）：0.0742 次/s。

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 27.81/52.03 | 47.95/88.12 | 47.95/88.12 | — | 0/0/0 | 0 | 0.40 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 960.00 | 800.00/896.00 | 896.00 | 384.00 | 192.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=none/None，borrow=None，snapshot=incremental-cow/tiered，placement=load_aware，runtime=off。

解释：arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec 存在未标注 execution_scope 的旧 tool_completed；未分类命令吞吐作为替代，不能全算应用 Tool。

配置：`/home/weitianc/clawbox-baseline-audit-20261016-a91-v4/sqlglot-results/sqlglot-admission-c160-r2/experiment.yaml`

事件：`/home/weitianc/clawbox-baseline-audit-20261016-a91-v4/sqlglot-results/sqlglot-admission-c160-r2/attempts/attempt-8a708bf08bbe4870/9a707ad43baa952add85cdb5/events/9a707ad43baa952add85cdb5.jsonl`

结果：`缺失`

## sqlglot-admission-c160-r3 / default-resident

开始：2026-10-16T10:49:54.926161+08:00；run=cancelled，保存的 arm=timed_out；证据 ID `09e961448c-attempt-0db73b7447284389-35443c55ba5432aab321dc0b`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 160 | 3600.00 | 0 | 0.00000 | — | 0.4125 | — | — |

旧日志未分类命令吞吐（替代，非应用 Tool 口径）：1.6778 次/s。

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 8.53/73.17 | 217.19/243.37 | 217.19/243.37 | — | 0/0/0 | 0 | 0.85 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 1152.00 | —/— | 1152.00 | 0.00 | — | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=off，idle=none/None，borrow=None，snapshot=incremental-cow/tiered，placement=os_default，runtime=off。

解释： 存在未标注 execution_scope 的旧 tool_completed；未分类命令吞吐作为替代，不能全算应用 Tool。

保存的失败原因：`arm exceeded hard timeout of 3600 seconds`

配置：`/home/weitianc/clawbox-baseline-audit-20261016-a91-v4/sqlglot-results/sqlglot-admission-c160-r3/experiment.yaml`

事件：`/home/weitianc/clawbox-baseline-audit-20261016-a91-v4/sqlglot-results/sqlglot-admission-c160-r3/attempts/attempt-0db73b7447284389/35443c55ba5432aab321dc0b/events/35443c55ba5432aab321dc0b.jsonl`

结果：`/home/weitianc/clawbox-baseline-audit-20261016-a91-v4/sqlglot-results/sqlglot-admission-c160-r3/arms/35443c55ba5432aab321dc0b.json`

## sqlglot-admission-c160-r3 / predicted-resident

开始：2026-10-16T11:52:41.248237+08:00；run=cancelled，保存的 arm=running；证据 ID `09e961448c-attempt-0db73b7447284389-e737d137679c562389b4482b`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 160 | 120.96 | 0 | 0.00000 | — | 0.4134 | — | — |

旧日志未分类命令吞吐（替代，非应用 Tool 口径）：0.0744 次/s。

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 25.92/45.97 | 56.33/96.83 | 56.33/96.83 | — | 0/0/0 | 0 | 0.46 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 960.00 | 800.00/896.00 | 896.00 | 384.00 | 192.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=none/None，borrow=None，snapshot=incremental-cow/tiered，placement=load_aware，runtime=off。

解释：arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec 存在未标注 execution_scope 的旧 tool_completed；未分类命令吞吐作为替代，不能全算应用 Tool。

配置：`/home/weitianc/clawbox-baseline-audit-20261016-a91-v4/sqlglot-results/sqlglot-admission-c160-r3/experiment.yaml`

事件：`/home/weitianc/clawbox-baseline-audit-20261016-a91-v4/sqlglot-results/sqlglot-admission-c160-r3/attempts/attempt-0db73b7447284389/e737d137679c562389b4482b/events/e737d137679c562389b4482b.jsonl`

结果：`缺失`

## pilot-admission-c16 / predicted-100

开始：2026-10-17T02:44:34.608495+08:00；run=cancelled，保存的 arm=running；证据 ID `605a24fc65-attempt-ce1c4d164fc448c6-fd2a1f6dcab63deb7ce4094a`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 16 | 694.54 | 0 | 0.00000 | 0.4319 | 0.2131 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 1.71/9.36 | 8.79/10.95 | 14.74/15.94 | 2.64 | 26/11/10 | 0 | 11.75 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 20.00 | 14.00/18.00 | 18.00 | 128.00 | 64.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c16/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c16/attempts/attempt-ce1c4d164fc448c6/fd2a1f6dcab63deb7ce4094a/events/fd2a1f6dcab63deb7ce4094a.jsonl`

结果：`缺失`

## pilot-admission-c16-v2 / predicted-100

开始：2026-10-17T03:08:32.821166+08:00；run=cancelled，保存的 arm=running；证据 ID `20313abc03-attempt-0820f2557dff4410-f42af09da856ac01a3cf8da7`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 16 | 944.35 | 0 | 0.00000 | 0.0625 | 0.0858 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 1.48/11.09 | 12.24/15.25 | 17.17/19.71 | 3.50 | 28/13/8 | 0 | 11.22 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 20.00 | 14.00/18.00 | 18.00 | 128.00 | 64.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c16-v2/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c16-v2/attempts/attempt-0820f2557dff4410/f42af09da856ac01a3cf8da7/events/f42af09da856ac01a3cf8da7.jsonl`

结果：`缺失`

## pilot-admission-c8-v3 / fixed-64

开始：2026-10-17T03:53:08.654763+08:00；run=succeeded，保存的 arm=succeeded；证据 ID `c8abd9be98-attempt-0f89b0b671654092-4be84b4161cadaa4a70818c2`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 8 | 701.97 | 8 | 0.01140 | 0.5926 | 0.2849 | 685.15 | 690.28 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 3.28/10.43 | 16.59/21.24 | 16.64/21.30 | 0.00 | 2/0/0 | 0 | 8.61 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 32.00 | 24.00/28.00 | 28.00 | 128.00 | 64.00 | 2.00/4.00 | 0.00 | 0.10 |

机制：admission=fixed，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：没有已确认的专项异常注释；不代表已证明不存在其他混杂。

配置：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c8-v3/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c8-v3/attempts/attempt-0f89b0b671654092/4be84b4161cadaa4a70818c2/events/4be84b4161cadaa4a70818c2.jsonl`

结果：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c8-v3/arms/4be84b4161cadaa4a70818c2.json`

## pilot-admission-c8-v3 / predicted-100

开始：2026-10-17T03:29:37.226733+08:00；run=succeeded，保存的 arm=succeeded；证据 ID `c8abd9be98-attempt-0f89b0b671654092-c76b08ba2aa4af90cb7407d1`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 8 | 692.13 | 8 | 0.01156 | 0.6010 | 0.2890 | 679.16 | 684.80 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 3.32/10.33 | 16.25/20.87 | 16.37/20.99 | 0.00 | 2/0/0 | 0 | 2.90 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 32.00 | 24.00/28.00 | 28.00 | 128.00 | 64.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：没有已确认的专项异常注释；不代表已证明不存在其他混杂。

配置：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c8-v3/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c8-v3/attempts/attempt-0f89b0b671654092/c76b08ba2aa4af90cb7407d1/events/c76b08ba2aa4af90cb7407d1.jsonl`

结果：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c8-v3/arms/c76b08ba2aa4af90cb7407d1.json`

## pilot-admission-c8-v3 / fixed-630

开始：2026-10-17T03:41:12.649336+08:00；run=succeeded，保存的 arm=succeeded；证据 ID `c8abd9be98-attempt-0f89b0b671654092-014a921a8bf6152f48d61343`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 8 | 713.52 | 8 | 0.01121 | 0.5830 | 0.2803 | 698.52 | 702.98 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 3.24/10.49 | 16.54/21.12 | 16.63/21.20 | 0.00 | 2/0/0 | 0 | 4.40 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 32.00 | 24.00/28.00 | 28.00 | 128.00 | 64.00 | 2.00/4.00 | 0.00 | 0.10 |

机制：admission=fixed，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：没有已确认的专项异常注释；不代表已证明不存在其他混杂。

配置：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c8-v3/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c8-v3/attempts/attempt-0f89b0b671654092/014a921a8bf6152f48d61343/events/014a921a8bf6152f48d61343.jsonl`

结果：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c8-v3/arms/014a921a8bf6152f48d61343.json`

## pilot-admission-c8-v3 / predicted-025

开始：2026-10-17T04:04:53.116116+08:00；run=succeeded，保存的 arm=succeeded；证据 ID `c8abd9be98-attempt-0f89b0b671654092-b9aa7d0e279affd91cd5507f`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 8 | 825.87 | 8 | 0.00969 | 0.5037 | 0.2422 | 814.06 | 821.13 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 2.82/10.35 | 16.25/20.56 | 16.41/20.75 | 0.00 | 1/0/0 | 0 | 16.38 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 32.00 | 24.00/28.00 | 28.00 | 128.00 | 64.00 | 2.00/4.00 | 0.00 | 0.25 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：没有已确认的专项异常注释；不代表已证明不存在其他混杂。

配置：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c8-v3/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c8-v3/attempts/attempt-0f89b0b671654092/b9aa7d0e279affd91cd5507f/events/b9aa7d0e279affd91cd5507f.jsonl`

结果：`/home/weitianc/clawbox-high-density-20261017/results/pilot-admission-c8-v3/arms/b9aa7d0e279affd91cd5507f.json`

## final-primary-c320 / full

开始：2026-10-17T04:22:51.383169+08:00；run=cancelled，保存的 arm=failed；证据 ID `85fbd46dad-attempt-342e58c2111d4174-a6c0a28a114880e64896422b`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 193.63 | 0 | 0.00000 | 0.0000 | 0.2272 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 15.90/23.68 | 33.64/70.63 | 33.72/70.64 | 0.00 | 2/0/0 | 1 | 21.01 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 768.00 | 576.00/690.00 | 690.00 | 256.00 | 128.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：没有已确认的专项异常注释；不代表已证明不存在其他混杂。

保存的失败原因：`[Errno 99] Cannot assign requested address`

配置：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320/attempts/attempt-342e58c2111d4174/a6c0a28a114880e64896422b/events/a6c0a28a114880e64896422b.jsonl`

结果：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320/arms/a6c0a28a114880e64896422b.json`

## final-mechanism-c128 / full

开始：2026-10-17T04:30:44.049778+08:00；run=cancelled，保存的 arm=failed；证据 ID `85de7609ef-attempt-cf5bcf3969924f04-76eebce2b30cad31ab7df303`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 128 | 86.47 | 0 | 0.00000 | 0.0000 | 0.1156 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 13.96/22.69 | 14.59/28.66 | 14.69/28.69 | 0.00 | 2/0/0 | 1 | 12.38 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 256.00 | 192.00/230.00 | 230.00 | 64.00 | 32.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：没有已确认的专项异常注释；不代表已证明不存在其他混杂。

保存的失败原因：`timed out or killed updating /sys/fs/cgroup/cube_sandbox/sandbox/clawbox-49258a764b2f434a8cc0a9e6851af662-vm-0079-runtime/cpuset.mems: kernel write limit is 5 seconds`

配置：`/home/weitianc/clawbox-high-density-20261017/results/final-mechanism-c128/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/final-mechanism-c128/attempts/attempt-cf5bcf3969924f04/76eebce2b30cad31ab7df303/events/76eebce2b30cad31ab7df303.jsonl`

结果：`/home/weitianc/clawbox-high-density-20261017/results/final-mechanism-c128/arms/76eebce2b30cad31ab7df303.json`

## final-primary-c320-r2 / full

开始：2026-10-17T04:33:54.579468+08:00；run=cancelled，保存的 arm=running；证据 ID `b401414544-attempt-6bfb00aacb9142ed-a6c0a28a114880e64896422b`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 454.58 | 0 | 0.00000 | 0.0088 | 0.2310 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 12.35/18.45 | 54.74/123.14 | 55.16/123.18 | 0.00 | 7/0/0 | 0 | 55.77 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 768.00 | 576.00/690.00 | 690.00 | 256.00 | 128.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-r2/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-r2/attempts/attempt-6bfb00aacb9142ed/a6c0a28a114880e64896422b/events/a6c0a28a114880e64896422b.jsonl`

结果：`缺失`

## final-primary-c320-r3 / full

开始：2026-10-17T04:48:18.705582+08:00；run=failed，保存的 arm=failed；证据 ID `b456d9f84d-attempt-827a35f2f57145fe-a6c0a28a114880e64896422b`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 114.65 | 0 | 0.00000 | 0.0000 | 0.2006 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 15.99/24.79 | 16.15/35.63 | 18.90/40.21 | 0.19 | 8/1/0 | 1 | 17.87 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 768.00 | 576.00/690.00 | 690.00 | 256.00 | 128.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：没有已确认的专项异常注释；不代表已证明不存在其他混杂。

保存的失败原因：`timed out updating /sys/fs/cgroup/cube_sandbox/sandbox/clawbox-dc31a309cff24a0f884da3ee47bf69b5-vm-0248-runtime/cpuset.cpus`

配置：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-r3/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-r3/attempts/attempt-827a35f2f57145fe/a6c0a28a114880e64896422b/events/a6c0a28a114880e64896422b.jsonl`

结果：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-r3/arms/a6c0a28a114880e64896422b.json`

## final-primary-c320-r4 / full

开始：2026-10-17T05:00:19.146550+08:00；run=cancelled，保存的 arm=running；证据 ID `0abd979351-attempt-4af7691b3776480c-a6c0a28a114880e64896422b`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 424.06 | 0 | 0.00000 | 0.0424 | 0.3655 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 18.39/27.12 | 83.58/164.60 | 84.93/164.64 | 0.00 | 13/0/0 | 0 | 27.01 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 768.00 | 576.00/690.00 | 690.00 | 256.00 | 128.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-r4/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-r4/attempts/attempt-4af7691b3776480c/a6c0a28a114880e64896422b/events/a6c0a28a114880e64896422b.jsonl`

结果：`缺失`

## final-primary-c320-r5 / full

开始：2026-10-17T05:16:11.335561+08:00；run=cancelled，保存的 arm=running；证据 ID `66f5b38776-attempt-c35c86d46e204d80-a6c0a28a114880e64896422b`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 725.57 | 0 | 0.00000 | 0.3776 | 0.5568 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 12.61/49.16 | 126.52/146.16 | 126.53/146.16 | 0.00 | 0/0/0 | 0 | 33.26 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 768.00 | 576.00/690.00 | 690.00 | 256.00 | 128.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-r5/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-r5/attempts/attempt-c35c86d46e204d80/a6c0a28a114880e64896422b/events/a6c0a28a114880e64896422b.jsonl`

结果：`缺失`

## final-primary-c320-r6 / full

开始：2026-10-17T05:36:50.206171+08:00；run=cancelled，保存的 arm=running；证据 ID `a00bb50f5f-attempt-706522fe51e645ba-a6c0a28a114880e64896422b`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 725.96 | 0 | 0.00000 | 0.3981 | 0.5551 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 12.10/49.12 | 121.95/139.81 | 121.96/139.81 | 0.00 | 0/0/0 | 0 | 19.13 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 768.00 | 576.00/690.00 | 690.00 | 256.00 | 128.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-r6/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-r6/attempts/attempt-706522fe51e645ba/a6c0a28a114880e64896422b/events/a6c0a28a114880e64896422b.jsonl`

结果：`缺失`

## final-primary-c320-r7 / full

开始：2026-10-17T05:55:52.809179+08:00；run=destroyed，保存的 arm=running；证据 ID `4a059e09a7-attempt-ba20fba058864d7f-a6c0a28a114880e64896422b`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 1783.80 | 0 | 0.00000 | 3.1792 | 1.3028 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 16.80/56.17 | 233.63/260.48 | 233.63/260.48 | 0.00 | 0/0/0 | 0 | 8.98 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 768.00 | 576.00/690.00 | 690.00 | 256.00 | 128.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-r7/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-r7/attempts/attempt-ba20fba058864d7f/a6c0a28a114880e64896422b/events/a6c0a28a114880e64896422b.jsonl`

结果：`缺失`

## final-primary-c320-short / full

开始：2026-10-17T06:34:31.618145+08:00；run=destroyed，保存的 arm=failed；证据 ID `e4f4d6cb4c-attempt-7686e8f3c7ae4b07-12aed1f5bc8199de0cfa530d`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 1500.00 | 0 | 0.00000 | 3.3880 | 1.6960 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 21.72/43.73 | 228.36/306.30 | 228.43/306.53 | 0.00 | 0/0/0 | 0 | 50.34 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 768.00 | 576.00/690.00 | 690.00 | 256.00 | 128.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

保存的失败原因：`cleanup timed out after 180 seconds`

配置：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-short/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-short/attempts/attempt-7686e8f3c7ae4b07/12aed1f5bc8199de0cfa530d/events/12aed1f5bc8199de0cfa530d.jsonl`

结果：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-short/arms/12aed1f5bc8199de0cfa530d.json`

## final-primary-c320-short-fixed-only / fixed

开始：2026-10-17T07:05:49.805370+08:00；run=failed，保存的 arm=timed_out；证据 ID `ef1309c971-attempt-049fb19583c04bbe-cef91f34a76f7525e8e400ee`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 1500.00 | 5 | 0.00333 | 3.7933 | 1.8247 | 1475.09 | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 24.82/33.92 | 220.62/299.40 | 220.65/299.47 | 0.00 | 0/0/0 | 0 | 63.84 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 768.00 | 576.00/690.00 | 690.00 | 256.00 | 128.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=fixed，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

保存的失败原因：`arm exceeded hard timeout of 1500 seconds`

配置：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-short-fixed-only/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-short-fixed-only/attempts/attempt-049fb19583c04bbe/cef91f34a76f7525e8e400ee/events/cef91f34a76f7525e8e400ee.jsonl`

结果：`/home/weitianc/clawbox-high-density-20261017/results/final-primary-c320-short-fixed-only/arms/cef91f34a76f7525e8e400ee.json`

## pilot-c32-01 / predicted-025

开始：2026-10-17T09:48:40.501973+08:00；run=destroyed，保存的 arm=running；证据 ID `8fd4087dda-attempt-daf662559b2c4c39-8467477cc347d7373f9ab4e2`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 32 | 1723.87 | 0 | 0.00000 | 0.7982 | 0.3794 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 83.71/99.49 | 47.63/53.91 | 55.79/68.70 | 0.00 | 15/0/0 | 0 | 15.11 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 64.00 | 48.00/56.00 | 56.00 | 128.00 | 64.00 | 2.00/4.00 | 0.00 | 0.25 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/pilot-c32-01/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/pilot-c32-01/attempts/attempt-daf662559b2c4c39/8467477cc347d7373f9ab4e2/events/8467477cc347d7373f9ab4e2.jsonl`

结果：`缺失`

## final-primary-c320-01 / fixed

开始：2026-10-17T11:06:16.083648+08:00；run=destroyed，保存的 arm=running；证据 ID `974e857d56-attempt-61de8d6472944ddd-5e65e09d367ad81c6324702e`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 80.33 | 0 | 0.00000 | 0.1369 | 0.1992 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 15.40/26.21 | 17.75/36.80 | 17.78/36.83 | 0.00 | 0/0/0 | 0 | 7.82 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 768.00 | 576.00/690.00 | 690.00 | 256.00 | 128.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=fixed，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/final-primary-c320-01/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/final-primary-c320-01/attempts/attempt-61de8d6472944ddd/5e65e09d367ad81c6324702e/events/5e65e09d367ad81c6324702e.jsonl`

结果：`缺失`

## final-primary-c320-01 / full

开始：2026-10-17T10:35:02.127374+08:00；run=destroyed，保存的 arm=timed_out；证据 ID `974e857d56-attempt-61de8d6472944ddd-a6c0a28a114880e64896422b`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 1800.00 | 98 | 0.05444 | 3.9950 | 1.9222 | 1606.57 | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 29.44/45.53 | 206.93/283.19 | 206.96/283.22 | 0.00 | 0/0/0 | 0 | 67.85 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 768.00 | 576.00/690.00 | 690.00 | 256.00 | 128.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

保存的失败原因：`arm exceeded hard timeout of 1800 seconds`

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/final-primary-c320-01/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/final-primary-c320-01/attempts/attempt-61de8d6472944ddd/a6c0a28a114880e64896422b/events/a6c0a28a114880e64896422b.jsonl`

结果：`/home/weitianc/clawbox-high-density-followup-20261017/results/final-primary-c320-01/arms/a6c0a28a114880e64896422b.json`

## final-primary-c320-03 / full

开始：2026-10-17T11:18:18.238045+08:00；run=failed，保存的 arm=failed；证据 ID `70713cb324-attempt-a9a69dc0efea4097-81e5e711f5fa228496d66e89`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 1800.00 | 83 | 0.04611 | 3.9917 | 1.8928 | 1677.75 | 1764.33 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 28.14/45.78 | 216.06/289.65 | 216.09/289.70 | 0.00 | 0/0/0 | 0 | 35.60 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 768.00 | 576.00/690.00 | 690.00 | 256.00 | 128.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

保存的失败原因：`237 (of 320) futures unfinished`

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/final-primary-c320-03/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/final-primary-c320-03/attempts/attempt-a9a69dc0efea4097/81e5e711f5fa228496d66e89/events/81e5e711f5fa228496d66e89.jsonl`

结果：`/home/weitianc/clawbox-high-density-followup-20261017/results/final-primary-c320-03/arms/81e5e711f5fa228496d66e89.json`

## contention-pilot-c1-01 / pilot-fixed-256

开始：2026-10-17T11:54:07.848382+08:00；run=succeeded，保存的 arm=succeeded；证据 ID `9e92971c35-attempt-9f416f1c13834103-ed2e32a97ad07f5817cbfe32`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 1 | 127.63 | 1 | 0.00784 | 0.0548 | 0.0627 | 123.47 | 124.38 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 1.05/3.52 | 5.01/7.69 | 5.02/7.69 | 0.00 | 0/0/0 | 0 | 0.97 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 48.00 | 36.00/42.00 | 42.00 | 128.00 | 64.00 | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=fixed，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：没有已确认的专项异常注释；不代表已证明不存在其他混杂。

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-pilot-c1-01/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-pilot-c1-01/attempts/attempt-9f416f1c13834103/ed2e32a97ad07f5817cbfe32/events/ed2e32a97ad07f5817cbfe32.jsonl`

结果：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-pilot-c1-01/arms/ed2e32a97ad07f5817cbfe32.json`

## contention-final-c32-01 / full-predicted

开始：2026-10-17T12:54:50.979632+08:00；run=failed，保存的 arm=running；证据 ID `a38a548a0f-attempt-0eac81a491fb48af-761d9b60e3628bf187b98179`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 32 | 241.26 | 0 | 0.00000 | 0.4601 | 0.6093 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 55.37/98.02 | 58.75/80.00 | 58.76/80.00 | 0.00 | 0/0/0 | 0 | 3.06 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 80.00 | 64.00/76.00 | 76.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=off，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

保存的失败原因：`cleanup timed out after 180 seconds`

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-final-c32-01/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-final-c32-01/attempts/attempt-0eac81a491fb48af/761d9b60e3628bf187b98179/events/761d9b60e3628bf187b98179.jsonl`

结果：`缺失`

## contention-command-table-c32-01 / full-predicted

开始：2026-10-17T13:19:21.930792+08:00；run=cancelled，保存的 arm=running；证据 ID `f3b2bf7b66-attempt-bedca3bf984f4385-82f0d8b4113b85a12a0dcd5e`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 32 | 315.85 | 0 | 0.00000 | 0.3324 | 0.4401 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 25.55/73.33 | 53.91/77.42 | 53.93/77.43 | 0.00 | 0/0/0 | 0 | 6.73 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 80.00 | 64.00/76.00 | 76.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=off，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c32-01/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c32-01/attempts/attempt-bedca3bf984f4385/82f0d8b4113b85a12a0dcd5e/events/82f0d8b4113b85a12a0dcd5e.jsonl`

结果：`缺失`

## contention-command-table-c28-01 / full-predicted

开始：2026-10-17T13:30:01.532439+08:00；run=cancelled，保存的 arm=running；证据 ID `98b8bcc4a4-attempt-45bf7df17adf4d52-52b8ec8cdffdca8aa6f43d88`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 28 | 307.88 | 0 | 0.00000 | 0.3378 | 0.4482 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 23.28/66.02 | 53.74/77.58 | 53.75/77.58 | 0.00 | 0/0/0 | 0 | 12.39 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 80.00 | 64.00/76.00 | 76.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=off，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c28-01/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c28-01/attempts/attempt-45bf7df17adf4d52/52b8ec8cdffdca8aa6f43d88/events/52b8ec8cdffdca8aa6f43d88.jsonl`

结果：`缺失`

## contention-command-table-c28-v2-01 / fixed-256

开始：2026-10-17T13:45:19.405868+08:00；run=succeeded，保存的 arm=succeeded；证据 ID `9aecae51c0-attempt-9f162a1540f24cd3-788c1ae72aa349475e55e5a4`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 28 | 238.84 | 28 | 0.11723 | 0.8206 | 0.9379 | 206.30 | 223.56 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 33.75/63.96 | 39.72/80.00 | 39.75/80.00 | 0.00 | 0/0/0 | 0 | 5.89 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 80.00 | 64.00/76.00 | 76.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=fixed，idle=release_cost/tool，borrow=off，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：没有已确认的专项异常注释；不代表已证明不存在其他混杂。

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c28-v2-01/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c28-v2-01/attempts/attempt-9f162a1540f24cd3/788c1ae72aa349475e55e5a4/events/788c1ae72aa349475e55e5a4.jsonl`

结果：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c28-v2-01/arms/788c1ae72aa349475e55e5a4.json`

## contention-command-table-c28-v2-01 / full-predicted

开始：2026-10-17T13:40:47.948992+08:00；run=succeeded，保存的 arm=succeeded；证据 ID `9aecae51c0-attempt-9f162a1540f24cd3-3891de1668e21fc96c1c1859`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 28 | 268.44 | 28 | 0.10431 | 0.7302 | 0.8345 | 221.75 | 251.61 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 32.85/63.85 | 37.48/70.58 | 37.51/70.58 | 0.00 | 0/0/0 | 0 | 8.38 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 80.00 | 64.00/76.00 | 76.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=off，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：没有已确认的专项异常注释；不代表已证明不存在其他混杂。

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c28-v2-01/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c28-v2-01/attempts/attempt-9f162a1540f24cd3/3891de1668e21fc96c1c1859/events/3891de1668e21fc96c1c1859.jsonl`

结果：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c28-v2-01/arms/3891de1668e21fc96c1c1859.json`

## contention-command-table-c28-v2-m64-01 / fixed-256

开始：2026-10-17T13:58:53.737836+08:00；run=succeeded，保存的 arm=succeeded；证据 ID `67bc99a88b-attempt-84a49c2c77af4f5b-7dc8b17435ec015258884c6c`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 28 | 186.08 | 28 | 0.15048 | 1.0533 | 1.2038 | 161.77 | 182.37 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 28.17/62.61 | 38.32/64.00 | 38.33/64.00 | 0.00 | 0/0/0 | 0 | 3.87 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 64.00 | 48.00/60.00 | 60.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=fixed，idle=release_cost/tool，borrow=off，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：没有已确认的专项异常注释；不代表已证明不存在其他混杂。

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c28-v2-m64-01/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c28-v2-m64-01/attempts/attempt-84a49c2c77af4f5b/7dc8b17435ec015258884c6c/events/7dc8b17435ec015258884c6c.jsonl`

结果：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c28-v2-m64-01/arms/7dc8b17435ec015258884c6c.json`

## contention-command-table-c28-v2-m64-01 / full-predicted

开始：2026-10-17T13:55:22.895289+08:00；run=succeeded，保存的 arm=succeeded；证据 ID `67bc99a88b-attempt-84a49c2c77af4f5b-aaf3218fdb0155d2b3c52337`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 28 | 207.74 | 28 | 0.13478 | 0.9435 | 1.0783 | 186.96 | 200.13 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 25.30/65.25 | 35.37/57.02 | 35.38/57.02 | 0.00 | 0/0/0 | 0 | 2.96 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 64.00 | 48.00/60.00 | 60.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=off，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：没有已确认的专项异常注释；不代表已证明不存在其他混杂。

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c28-v2-m64-01/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c28-v2-m64-01/attempts/attempt-84a49c2c77af4f5b/aaf3218fdb0155d2b3c52337/events/aaf3218fdb0155d2b3c52337.jsonl`

结果：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c28-v2-m64-01/arms/aaf3218fdb0155d2b3c52337.json`

## contention-command-table-c24-v2-m48-01 / fixed-256

开始：2026-10-17T14:10:56.646029+08:00；run=succeeded，保存的 arm=succeeded；证据 ID `3a55bbbe76-attempt-660b9f9719ff49e9-908d4d18296e51dcffa52c4b`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 24 | 485.14 | 24 | 0.04947 | 0.3463 | 0.3958 | 451.62 | 471.54 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 14.10/55.85 | 25.92/46.66 | 25.92/46.66 | 0.00 | 0/0/0 | 0 | 8.75 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 48.00 | 36.00/44.00 | 44.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=fixed，idle=release_cost/tool，borrow=off，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：没有已确认的专项异常注释；不代表已证明不存在其他混杂。

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c24-v2-m48-01/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c24-v2-m48-01/attempts/attempt-660b9f9719ff49e9/908d4d18296e51dcffa52c4b/events/908d4d18296e51dcffa52c4b.jsonl`

结果：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c24-v2-m48-01/arms/908d4d18296e51dcffa52c4b.json`

## contention-command-table-c24-v2-m48-01 / full-predicted

开始：2026-10-17T14:05:58.356336+08:00；run=succeeded，保存的 arm=succeeded；证据 ID `3a55bbbe76-attempt-660b9f9719ff49e9-40930dabffab2ff53fbcfce1`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 24 | 295.87 | 24 | 0.08112 | 0.5678 | 0.6489 | 253.99 | 274.24 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 18.98/56.41 | 28.66/42.07 | 28.67/42.07 | 0.00 | 0/0/0 | 0 | 3.78 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 48.00 | 36.00/44.00 | 44.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=off，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：没有已确认的专项异常注释；不代表已证明不存在其他混杂。

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c24-v2-m48-01/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c24-v2-m48-01/attempts/attempt-660b9f9719ff49e9/40930dabffab2ff53fbcfce1/events/40930dabffab2ff53fbcfce1.jsonl`

结果：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-command-table-c24-v2-m48-01/arms/40930dabffab2ff53fbcfce1.json`

## contention-no-admission-c24-v2-m48-01 / no-admission

开始：2026-10-17T14:42:38.850201+08:00；run=failed，保存的 arm=failed；证据 ID `14117c80f5-attempt-31b58881b82b4ccc-cf719a67e7ef4f78b8a0f8fb`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 24 | 509.70 | 0 | 0.00000 | 0.3021 | 0.3551 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 47.40/100.71 | 40.19/48.00 | 40.19/48.00 | 0.00 | 0/0/0 | 1 | 39.80 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 48.00 | 36.00/44.00 | 44.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=off，idle=release_cost/tool，borrow=off，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：旧 drain/cleanup 失败传播；不能据此声称胜过无准入。 CPU 采样峰值超过记录 CPU 容量，保留异常值，不能当作可信瞬时利用率。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

保存的失败原因：`session cleanup incomplete for cf719a67e7ef4f78b8a0f8fb-0016: policy session did not drain: cf719a67e7ef4f78b8a0f8fb-0016`

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-no-admission-c24-v2-m48-01/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-no-admission-c24-v2-m48-01/attempts/attempt-31b58881b82b4ccc/cf719a67e7ef4f78b8a0f8fb/events/cf719a67e7ef4f78b8a0f8fb.jsonl`

结果：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-no-admission-c24-v2-m48-01/arms/cf719a67e7ef4f78b8a0f8fb.json`

## contention-no-admission-c24-v2-m48-02 / no-admission

开始：2026-10-17T15:03:57.409117+08:00；run=failed，保存的 arm=failed；证据 ID `ad0fa5a082-attempt-488f9a0b85424390-cf719a67e7ef4f78b8a0f8fb`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 24 | 524.18 | 4 | 0.00763 | 0.2823 | 0.3491 | 203.69 | 211.31 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 45.32/93.24 | 36.93/48.00 | 36.93/48.00 | 0.00 | 0/0/0 | 20 | 83.43 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 48.00 | 36.00/44.00 | 44.00 | 128.00 | — | 2.00/4.00 | 0.00 | 1.00 |

机制：admission=off，idle=release_cost/tool，borrow=off，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：旧 drain/cleanup 失败传播；保留局部完成数，不能作无准入公平对照。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

保存的失败原因：`cleanup process exited with status 1`

配置：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-no-admission-c24-v2-m48-02/experiment.yaml`

事件：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-no-admission-c24-v2-m48-02/attempts/attempt-488f9a0b85424390/cf719a67e7ef4f78b8a0f8fb/events/cf719a67e7ef4f78b8a0f8fb.jsonl`

结果：`/home/weitianc/clawbox-high-density-followup-20261017/results/contention-no-admission-c24-v2-m48-02/arms/cf719a67e7ef4f78b8a0f8fb.json`

## c1280-screen-c320-01 / no-tool-admission-shared

开始：2026-10-17T16:04:01.635786+08:00；run=cancelled，保存的 arm=running；证据 ID `68f21ae06b-attempt-324bd2158fd44d2d-7e9b036d061a8d176ada7a4a`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 1200.00 | 174 | 0.14500 | 1.0642 | 1.2242 | 356.59 | 1000.48 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 18.51/40.42 | 49.78/115.74 | 49.82/115.78 | 0.00 | 0/0/0 | 0 | 245.39 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 800.00/864.00 | 864.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=off，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：旧 VM 创建路径瓶颈；局部完成数据可保留，排除正式策略比较。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

保存的失败原因：`146 (of 320) futures unfinished`

配置：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-01/attempts/attempt-324bd2158fd44d2d/7e9b036d061a8d176ada7a4a/events/7e9b036d061a8d176ada7a4a.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-01/attempts/attempt-324bd2158fd44d2d/7e9b036d061a8d176ada7a4a/arms/7e9b036d061a8d176ada7a4a.json`

## c1280-screen-c320-02 / no-tool-admission-shared

开始：2026-10-17T16:39:05.629983+08:00；run=cancelled，保存的 arm=running；证据 ID `bf83821add-attempt-9f5c2bec276849c7-53dfbbe7b8e8b7ae568f8175`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 695.54 | 166 | 0.23866 | 2.5548 | 2.8496 | 232.63 | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 22.49/48.54 | 142.82/286.76 | 142.84/286.81 | 0.00 | 0/0/0 | 0 | 78.61 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 800.00/864.00 | 864.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=off，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：旧 VM 创建路径瓶颈；部分成功可见，未完成完整对照。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-02/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-02/attempts/attempt-9f5c2bec276849c7/53dfbbe7b8e8b7ae568f8175/events/53dfbbe7b8e8b7ae568f8175.jsonl`

结果：`缺失`

## c1280-screen-c320-03 / no-tool-admission-shared

开始：2026-10-17T16:52:04.364429+08:00；run=destroyed，保存的 arm=running；证据 ID `644224b8a6-attempt-c38e70734a3940ef-53dfbbe7b8e8b7ae568f8175`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 560.15 | 6 | 0.01071 | 1.1068 | 1.4210 | 213.12 | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 22.65/53.94 | 32.84/230.65 | 32.89/230.69 | 0.00 | 0/0/0 | 56 | 167.75 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 800.00/864.00 | 864.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=off，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：旧 VM 创建路径/创建 gate 过高；排除正式策略比较。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-03/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-03/attempts/attempt-c38e70734a3940ef/53dfbbe7b8e8b7ae568f8175/events/53dfbbe7b8e8b7ae568f8175.jsonl`

结果：`缺失`

## c1280-priority-2h-01 / os-default-no-admission

开始：2026-10-17T17:24:11.911065+08:00；run=cancelled，保存的 arm=timed_out；证据 ID `0a1817d8b4-attempt-207578ffd9884936-37789dbe011e2c4dc63e178a`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 1280 | 1200.00 | 0 | 0.00000 | 1.4775 | 1.6458 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 22.46/55.22 | —/— | 419.36/637.62 | 0.00 | 0/0/0 | 1 | 14.44 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 1120.00 | —/— | 1120.00 | 384.00 | — | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=off，idle=off/tool，borrow=off，snapshot=full-copy/tiered，placement=os_default，runtime=off。

解释：供应 VM 阶段超时；c1280 未达到完整供给，不能代表稳态 c1280。 OS-default 的 local_used_bytes 实为整个 VM cgroup，不能解释为 NUMA 0/1 LOCAL；移入 VM_cgroup 列。

保存的失败原因：`worker exceeded arm execution plus finalization timeout of 1500 seconds`

配置：`/home/weitianc/clawbox-c1280-20261017/results/c1280-priority-2h-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/c1280-priority-2h-01/attempts/attempt-207578ffd9884936/37789dbe011e2c4dc63e178a/events/37789dbe011e2c4dc63e178a.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/c1280-priority-2h-01/arms/37789dbe011e2c4dc63e178a.json`

## c1280-priority-2h-01 / os-default-no-admission

开始：2026-10-17T17:49:50.181422+08:00；run=cancelled，保存的 arm=running；证据 ID `0a1817d8b4-attempt-207578ffd9884936-2c8689bc37968befcb2ff42a`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 176.53 | 0 | 0.00000 | 1.0707 | 1.3992 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 21.58/34.79 | —/— | 77.26/187.22 | 0.00 | 0/0/0 | 0 | 1.46 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 1120.00 | —/— | 1120.00 | 384.00 | — | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=off，idle=off/tool，borrow=off，snapshot=full-copy/tiered，placement=os_default，runtime=off。

解释：供应 VM 阶段超时；c1280 未达到完整供给，不能代表稳态 c1280。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec OS-default 的 local_used_bytes 实为整个 VM cgroup，不能解释为 NUMA 0/1 LOCAL；移入 VM_cgroup 列。

配置：`/home/weitianc/clawbox-c1280-20261017/results/c1280-priority-2h-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/c1280-priority-2h-01/attempts/attempt-207578ffd9884936/2c8689bc37968befcb2ff42a/events/2c8689bc37968befcb2ff42a.jsonl`

结果：`缺失`

## c1280-screen-c320-04 / os-default-no-admission

开始：2026-10-17T19:40:21.654446+08:00；run=failed，保存的 arm=failed；证据 ID `e4fee730f6-attempt-b6fad17ea35c47e3-9730b09263630536ad0946ae`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 2400.00 | 0 | 0.00000 | 0.4037 | 0.5479 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 38.83/98.25 | —/— | 482.92/615.99 | 0.00 | 0/0/0 | 4 | 3.55 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 1120.00 | —/— | 1120.00 | 384.00 | — | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=off，idle=off/tool，borrow=off，snapshot=full-copy/tiered，placement=os_default，runtime=off。

解释：文件描述符耗尽，内存观测可靠性受影响。 OS-default 的 local_used_bytes 实为整个 VM cgroup，不能解释为 NUMA 0/1 LOCAL；移入 VM_cgroup 列。

保存的失败原因：`worker exited with status 1 without a valid arm result`

配置：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-04/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-04/attempts/attempt-b6fad17ea35c47e3/9730b09263630536ad0946ae/events/9730b09263630536ad0946ae.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-04/arms/9730b09263630536ad0946ae.json`

## c1280-screen-c320-05 / predicted-shared

开始：2026-10-17T20:36:50.627900+08:00；run=failed，保存的 arm=failed；证据 ID `ce4aba7e4c-attempt-731d4fde769845bd-b58eea6a653a82a8d6cb0e75`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 2072.14 | 0 | 0.00000 | 0.9744 | 1.1437 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 21.93/72.77 | 606.65/828.98 | 608.61/830.73 | 0.00 | 2/0/0 | 2 | 186.32 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 800.00/864.00 | 864.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：shared-return 生命周期竞争导致策略控制器失败。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

保存的失败原因：`shared policy controller failed`

配置：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-05/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-05/attempts/attempt-731d4fde769845bd/b58eea6a653a82a8d6cb0e75/events/b58eea6a653a82a8d6cb0e75.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-05/arms/b58eea6a653a82a8d6cb0e75.json`

## c1280-screen-c320-os-06 / os-default-no-admission

开始：2026-10-17T21:28:48.671593+08:00；run=failed，保存的 arm=failed；证据 ID `3be9de9473-attempt-1b657dcea39749c7-c8f49c0efe72a8d87af3c2c7`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 3600.00 | 0 | 0.00000 | 0.4200 | 0.5122 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 26.28/98.41 | —/— | 791.45/1051.31 | 0.00 | 0/0/0 | 0 | 4.09 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 1120.00 | —/— | 1120.00 | 384.00 | — | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=off，idle=off/tool，borrow=off，snapshot=full-copy/tiered，placement=os_default，runtime=off。

解释：合成十阶段 trace；3600 s 超时边界，0 成功，无 OOM；不是无限时长的最终成功率。 OS-default 的 local_used_bytes 实为整个 VM cgroup，不能解释为 NUMA 0/1 LOCAL；移入 VM_cgroup 列。

保存的失败原因：`320 (of 320) futures unfinished`

配置：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-os-06/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-os-06/attempts/attempt-1b657dcea39749c7/c8f49c0efe72a8d87af3c2c7/events/c8f49c0efe72a8d87af3c2c7.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-os-06/arms/c8f49c0efe72a8d87af3c2c7.json`

## c1280-screen-c320-predicted-06 / predicted-shared

开始：2026-10-17T22:36:11.201602+08:00；run=succeeded，保存的 arm=succeeded；证据 ID `93c969798f-attempt-327c1b9f95e746d7-d9cd0c3e34cc61ed009de56b`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 993.18 | 320 | 0.32220 | 3.2220 | 3.5442 | 486.44 | 923.31 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 31.03/64.15 | 321.51/599.90 | 322.66/601.11 | 0.00 | 0/0/0 | 0 | 43.93 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 800.00/864.00 | 864.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：合成十阶段 trace；完整完成；borrow/checkpoint 未激活，不能归功于它们。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

配置：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-predicted-06/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-predicted-06/attempts/attempt-327c1b9f95e746d7/d9cd0c3e34cc61ed009de56b/events/d9cd0c3e34cc61ed009de56b.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/c1280-screen-c320-predicted-06/arms/d9cd0c3e34cc61ed009de56b.json`

## c1280-primary-c640-07 / os-default-no-admission

开始：2026-10-17T23:17:37.812287+08:00；run=destroyed，保存的 arm=cancelled；证据 ID `ed40ef0493-attempt-6d28da94bd9e4d36-ef060ba494250a8ae6c10142`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 1047.14 | 0 | 0.00000 | 0.0630 | 0.5138 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 63.15/100.77 | —/— | 533.91/705.30 | 0.00 | 0/0/0 | 164 | 6.67 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 1120.00 | —/— | 1120.00 | 384.00 | — | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=off，idle=off/tool，borrow=off，snapshot=full-copy/tiered，placement=os_default，runtime=off。

解释：v5 trace 缺少验证要求的 completion marker；性能对照无效。 CPU 采样峰值超过记录 CPU 容量，保留异常值，不能当作可信瞬时利用率。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec OS-default 的 local_used_bytes 实为整个 VM cgroup，不能解释为 NUMA 0/1 LOCAL；移入 VM_cgroup 列。

配置：`/home/weitianc/clawbox-c1280-20261017/results/c1280-primary-c640-07/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/c1280-primary-c640-07/attempts/attempt-6d28da94bd9e4d36/ef060ba494250a8ae6c10142/events/ef060ba494250a8ae6c10142.jsonl`

结果：`缺失`

## c1280-primary-c640-07 / predicted-shared

开始：2026-10-17T22:57:47.158945+08:00；run=destroyed，保存的 arm=failed；证据 ID `ed40ef0493-attempt-6d28da94bd9e4d36-622a0233149c108896f5c849`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 1178.95 | 0 | 0.00000 | 2.1341 | 2.6676 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 44.98/85.86 | 220.60/442.67 | 221.83/443.94 | 0.00 | 0/0/0 | 640 | 47.87 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 800.00/864.00 | 864.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：v5 trace 缺少验证要求的 completion marker；性能对照无效。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

保存的失败原因：`validation failed for 622a0233149c108896f5c849-0024`

配置：`/home/weitianc/clawbox-c1280-20261017/results/c1280-primary-c640-07/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/c1280-primary-c640-07/attempts/attempt-6d28da94bd9e4d36/622a0233149c108896f5c849/events/622a0233149c108896f5c849.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/c1280-primary-c640-07/arms/622a0233149c108896f5c849.json`

## c1280-primary-c640-08-v6 / os-default-no-admission

开始：2026-10-18T00:25:13.439658+08:00；run=failed，保存的 arm=failed；证据 ID `076ba12788-attempt-98951d41f2634374-8b887fd887e74832fd1f9af2`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 3600.00 | 0 | 0.00000 | 0.1381 | 0.2775 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 37.24/112.04 | —/— | 683.71/813.55 | 0.00 | 0/0/0 | 164 | 7.33 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 1120.00 | —/— | 1120.00 | 384.00 | — | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=off，idle=off/tool，borrow=off，snapshot=full-copy/tiered，placement=os_default，runtime=off。

解释：合成四阶段 v6，和 c320 v3 不同，不能直接当作同一工作负载的扩展曲线。 CPU 采样峰值超过记录 CPU 容量，保留异常值，不能当作可信瞬时利用率。 OS-default 的 local_used_bytes 实为整个 VM cgroup，不能解释为 NUMA 0/1 LOCAL；移入 VM_cgroup 列。

保存的失败原因：`476 (of 640) futures unfinished`

配置：`/home/weitianc/clawbox-c1280-20261017/results/c1280-primary-c640-08-v6/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/c1280-primary-c640-08-v6/attempts/attempt-98951d41f2634374/8b887fd887e74832fd1f9af2/events/8b887fd887e74832fd1f9af2.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/c1280-primary-c640-08-v6/arms/8b887fd887e74832fd1f9af2.json`

## c1280-primary-c640-08-v6 / predicted-shared

开始：2026-10-17T23:54:33.287939+08:00；run=failed，保存的 arm=succeeded；证据 ID `076ba12788-attempt-98951d41f2634374-0ff3ff3c96475702d9497076`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 1811.07 | 640 | 0.35338 | 1.4135 | 1.7669 | 524.84 | 1737.01 |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 32.86/81.04 | 275.07/453.63 | 276.20/455.14 | 0.00 | 0/0/0 | 0 | 45.61 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 800.00/864.00 | 864.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/tiered，placement=load_aware，runtime=explicit。

解释：合成四阶段 v6，和 c320 v3 不同，不能直接当作同一工作负载的扩展曲线。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

配置：`/home/weitianc/clawbox-c1280-20261017/results/c1280-primary-c640-08-v6/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/c1280-primary-c640-08-v6/attempts/attempt-98951d41f2634374/0ff3ff3c96475702d9497076/events/0ff3ff3c96475702d9497076.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/c1280-primary-c640-08-v6/arms/0ff3ff3c96475702d9497076.json`

## c320-admission-ablation-01 / admission-predicted

开始：2026-10-18T02:11:55.939249+08:00；run=destroyed，保存的 arm=running；证据 ID `63f457d6b2-attempt-be19d43ac4d54b08-5cd254f5225db7db3bc881a3`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 875.45 | 308 | 0.35182 | 3.6518 | 4.0162 | 354.42 | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 32.36/56.46 | 289.79/489.86 | 290.82/484.87 | 0.00 | 0/0/0 | 0 | 48.19 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 800.00/864.00 | 864.00 | 384.00 | — | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=predicted，idle=off/tool，borrow=off，snapshot=full-copy/tiered，placement=round_robin，runtime=off。

解释：用户终止的合成负载实验；308 个已验证完成仍保留，非完整消融。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/c320-admission-ablation-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/c320-admission-ablation-01/attempts/attempt-be19d43ac4d54b08/5cd254f5225db7db3bc881a3/events/5cd254f5225db7db3bc881a3.jsonl`

结果：`缺失`

## real-trace-p0-c320-01 / f-s384

开始：2026-10-18T03:15:33.694477+08:00；run=cancelled，保存的 arm=running；证据 ID `27bf90e0c1-attempt-7c984ef610524a4a-bd627ce0a2030145035021f9`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 336.23 | 0 | 0.00000 | 0.0000 | 2.7184 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 45.11/72.91 | 126.51/225.35 | 127.88/226.83 | 0.00 | 0/0/0 | 1 | 43.57 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 448.00/512.00 | 512.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：原始日志不支持旧 112 完成说法；窗口仅 0 成功。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-01/attempts/attempt-7c984ef610524a4a/bd627ce0a2030145035021f9/events/bd627ce0a2030145035021f9.jsonl`

结果：`缺失`

## real-trace-p0-c320-l96-01 / f-s384

开始：2026-10-18T03:29:54.530326+08:00；run=cancelled，保存的 arm=running；证据 ID `7ea015249d-attempt-f3dd931e53dd4075-7d1086f91fc94a267fc68405`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 982.59 | 0 | 0.00000 | 3.1193 | 1.6212 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 13.98/48.07 | 71.29/87.83 | 103.35/136.03 | 35.54 | 98/133/126 | 5 | 45.55 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 80.00/96.00 | 96.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=predicted，idle=release_cost/tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：极紧水位造成 pause/restore churn；诊断边界。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-l96-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-l96-01/attempts/attempt-f3dd931e53dd4075/7d1086f91fc94a267fc68405/events/7d1086f91fc94a267fc68405.jsonl`

结果：`缺失`

## real-trace-p0-c320-pair-l160-01 / f-s384

开始：2026-10-18T04:27:27.852403+08:00；run=cancelled，保存的 arm=running；证据 ID `bb12044d5a-attempt-bdee47fd040e485e-e9a110853abcaa8023a27f46`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 319.87 | 0 | 0.00000 | 1.0348 | 1.4068 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 33.22/56.87 | 61.57/114.83 | 62.02/117.67 | 10.20 | 11/40/35 | 1 | 57.98 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 128.00/160.00 | 160.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：NUMA migration 隐含 30 s 限制导致 controller 失败。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 有 policy/controller failure，机制归因受影响。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-01/attempts/attempt-bdee47fd040e485e/e9a110853abcaa8023a27f46/events/e9a110853abcaa8023a27f46.jsonl`

结果：`缺失`

## real-trace-p0-c320-pair-l160-02 / f-s384

开始：2026-10-18T04:42:28.341367+08:00；run=cancelled，保存的 arm=running；证据 ID `021a21bbd8-attempt-4345a971eccd48ea-e9a110853abcaa8023a27f46`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 845.09 | 0 | 0.00000 | 2.7098 | 1.6957 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 16.43/49.96 | 103.34/128.92 | 108.09/139.36 | 64.64 | 41/120/116 | 13 | 159.13 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 128.00/160.00 | 160.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：并发创建 16 与 trace/checkpoint 叠加出现创建失败；随后改为 8。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 有 policy/controller failure，机制归因受影响。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-02/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-02/attempts/attempt-4345a971eccd48ea/e9a110853abcaa8023a27f46/events/e9a110853abcaa8023a27f46.jsonl`

结果：`缺失`

## real-trace-p0-c320-pair-l160-03 / f-s384

开始：2026-10-18T05:01:20.212331+08:00；run=cancelled，保存的 arm=running；证据 ID `b61c11b0ca-attempt-b32b0f124b3843ce-e9a110853abcaa8023a27f46`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 582.55 | 0 | 0.00000 | 2.0908 | 1.7029 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 21.96/55.52 | 93.13/116.28 | 96.13/119.03 | 48.90 | 30/126/125 | 0 | 125.16 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 128.00/160.00 | 160.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：Cube tombstone 残留与 borrow 预留记账问题。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 有 policy/controller failure，机制归因受影响。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-03/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-03/attempts/attempt-b32b0f124b3843ce/e9a110853abcaa8023a27f46/events/e9a110853abcaa8023a27f46.jsonl`

结果：`缺失`

## real-trace-p0-c320-pair-l160-04 / f-s384

开始：2026-10-18T05:20:19.623701+08:00；run=cancelled，保存的 arm=running；证据 ID `33dfd7b4ea-attempt-83e109e2e75d4a99-e9a110853abcaa8023a27f46`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 344.81 | 0 | 0.00000 | 1.0788 | 1.3341 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 27.21/61.73 | 84.95/118.87 | 86.24/122.49 | 24.29 | 13/58/54 | 1 | 91.06 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 128.00/160.00 | 160.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：暂停后清理 populated cgroup 破坏 tombstone 恢复；后续已撤销。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-04/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-04/attempts/attempt-83e109e2e75d4a99/e9a110853abcaa8023a27f46/events/e9a110853abcaa8023a27f46.jsonl`

结果：`缺失`

## real-trace-p0-c320-pair-l160-05 / f-s384

开始：2026-10-18T05:35:58.563395+08:00；run=cancelled，保存的 arm=running；证据 ID `163129f0b7-attempt-55b595ceef7b401a-e9a110853abcaa8023a27f46`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 1671.92 | 0 | 0.00000 | 2.7346 | 1.4367 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 15.13/61.40 | 102.33/114.05 | 110.59/121.64 | 157.70 | 71/420/396 | 22 | 201.78 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 128.00/160.00 | 160.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：失败 Agent 的 model-gateway drain 异常终止整臂；大量动作仅证明激活。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

保存的失败原因：`session cleanup incomplete for e9a110853abcaa8023a27f46-0003: model gateway session did not drain`

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-05/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-05/attempts/attempt-55b595ceef7b401a/e9a110853abcaa8023a27f46/events/e9a110853abcaa8023a27f46.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-05/attempts/attempt-55b595ceef7b401a/e9a110853abcaa8023a27f46/arms/e9a110853abcaa8023a27f46.json`

## real-trace-p0-c320-pair-l160-06 / a-off

开始：2026-10-18T07:06:24.108855+08:00；run=cancelled，保存的 arm=running；证据 ID `86bfdc9bb9-attempt-af9e5cfa740c4252-4a743172cfe6516ae97611e8`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 429.35 | 0 | 0.00000 | 1.8889 | 2.1730 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 24.39/63.83 | 101.01/135.30 | 103.65/146.63 | 36.83 | 18/74/62 | 1 | 64.56 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 128.00/160.00 | 160.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=off，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：Full 的远端 policy.py 过旧，两个 controller failure；A-off 提前终止。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 有 policy/controller failure，机制归因受影响。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-06/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-06/attempts/attempt-af9e5cfa740c4252/4a743172cfe6516ae97611e8/events/4a743172cfe6516ae97611e8.jsonl`

结果：`缺失`

## real-trace-p0-c320-pair-l160-06 / f-s384

开始：2026-10-18T06:16:23.216044+08:00；run=cancelled，保存的 arm=timed_out；证据 ID `86bfdc9bb9-attempt-af9e5cfa740c4252-e9a110853abcaa8023a27f46`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 2400.00 | 0 | 0.00000 | 1.7542 | 0.8321 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 13.60/62.49 | 138.34/157.05 | 154.91/181.00 | 51.10 | 31/104/98 | 6 | 82.18 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 128.00/160.00 | 160.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：Full 的远端 policy.py 过旧，两个 controller failure；A-off 提前终止。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 有 policy/controller failure，机制归因受影响。

保存的失败原因：`worker exceeded arm execution plus finalization timeout of 3000 seconds`

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-06/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-06/attempts/attempt-af9e5cfa740c4252/e9a110853abcaa8023a27f46/events/e9a110853abcaa8023a27f46.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-pair-l160-06/arms/e9a110853abcaa8023a27f46.json`

## real-trace-p0-c320-remaining-l160-01 / a-off

开始：2026-10-18T07:24:26.660473+08:00；run=cancelled，保存的 arm=running；证据 ID `f1c2a09b58-attempt-577b6cbfdfaf4b21-d1ce0442647ecbed3ca4574d`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 320 | 2400.00 | 0 | 0.00000 | 3.8021 | 1.8396 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 34.32/67.91 | 161.36/249.72 | 194.15/302.53 | 111.62 | 72/236/225 | 19 | 109.31 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 128.00/160.00 | 160.00 | 384.00 | 192.00 | 2.00/4.00 | 0.05 | 1.00 |

机制：admission=off，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：A-off 到达 2400 s；无成功，存在独立失败；高 checkpoint 活跃度，非收益证据。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-remaining-l160-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c320-remaining-l160-01/attempts/attempt-577b6cbfdfaf4b21/d1ce0442647ecbed3ca4574d/events/d1ce0442647ecbed3ca4574d.jsonl`

结果：`缺失`

## real-trace-p0-c640-mild-l640-01 / f-s384

开始：2026-10-18T08:10:45.187902+08:00；run=cancelled，保存的 arm=running；证据 ID `15a1a55feb-attempt-7f464bff31b149f1-f6c54435035ca0bce08fbddc`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 336.14 | 0 | 0.00000 | 0.0089 | 3.0463 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 39.40/68.97 | 133.01/229.16 | 133.58/229.88 | 0.00 | 0/0/0 | 0 | 23.05 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 512.00/640.00 | 640.00 | 384.00 | 192.00 | 2.00/4.00 | 0.50 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：绕过 wrapper 继承 FD=1024，EMFILE，基础设施无效点。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-mild-l640-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-mild-l640-01/attempts/attempt-7f464bff31b149f1/f6c54435035ca0bce08fbddc/events/f6c54435035ca0bce08fbddc.jsonl`

结果：`缺失`

## real-trace-p0-c640-mild-l640-02 / f-s384

开始：2026-10-18T08:20:10.152624+08:00；run=failed，保存的 arm=failed；证据 ID `b7ae6acd58-attempt-1160bd4f40cb467b-f6c54435035ca0bce08fbddc`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 1.92 | — | — | — | — | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| —/— | —/— | —/— | — | None/None/None | 0 | — |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 512.00/640.00 | 640.00 | 384.00 | 192.00 | 2.00/4.00 | 0.50 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：残留 qualification VM 导致 preflight 失败，未开始负载。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 缺少原始 event 文件，缺失指标未补零。

保存的失败原因：`worker exited with status 1 without a valid arm result`

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-mild-l640-02/experiment.yaml`

事件：`缺失`

结果：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-mild-l640-02/arms/f6c54435035ca0bce08fbddc.json`

## real-trace-p0-c640-mild-l640-03 / f-s384

开始：2026-10-18T08:22:53.445611+08:00；run=failed，保存的 arm=timed_out；证据 ID `e0fdb47ee7-attempt-a7b04ee9ce5e4f9d-f6c54435035ca0bce08fbddc`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 2400.00 | 0 | 0.00000 | 2.7075 | 2.4558 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 23.79/66.07 | 407.86/512.69 | 410.54/515.81 | 2.53 | 17/4/3 | 265 | 265.95 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 512.00/640.00 | 640.00 | 384.00 | 192.00 | 2.00/4.00 | 0.50 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：0.5 s 到达，Cube 生命周期/I/O/创建排队过载；统计截到配置执行期限，最终化另计。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

保存的失败原因：`worker exceeded arm execution plus finalization timeout of 3000 seconds`

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-mild-l640-03/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-mild-l640-03/attempts/attempt-a7b04ee9ce5e4f9d/f6c54435035ca0bce08fbddc/events/f6c54435035ca0bce08fbddc.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-mild-l640-03/arms/f6c54435035ca0bce08fbddc.json`

## real-trace-p0-c640-balanced-full-l512-01 / f-s384

开始：2026-10-18T09:17:51.144495+08:00；run=cancelled，保存的 arm=running；证据 ID `79afcfcc47-attempt-83297c17e2764c9c-ef3657e9ff61e922adefc78a`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 1100.00 | 0 | 0.00000 | 4.7618 | 2.6764 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 26.96/57.28 | 287.42/389.08 | 287.94/389.68 | 0.00 | 6/0/0 | 59 | 172.61 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 448.00/512.00 | 512.00 | 384.00 | 192.00 | 2.00/4.00 | 1.00 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：1.0 s 到达仍有 HTTP 408/502/envd 错误；未完成 Agent。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-balanced-full-l512-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-balanced-full-l512-01/attempts/attempt-83297c17e2764c9c/ef3657e9ff61e922adefc78a/events/ef3657e9ff61e922adefc78a.jsonl`

结果：`缺失`

## real-trace-p0-c640-paced-full-l352-01 / f-s384

开始：2026-10-18T10:08:24.614308+08:00；run=cancelled，保存的 arm=running；证据 ID `17d0405645-attempt-5a8872e49e2649cf-2d7f2f4888da043b689beb91`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 721.27 | 0 | 0.00000 | 2.9406 | 2.0533 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 25.04/41.26 | 194.15/283.51 | 195.10/286.61 | 3.51 | 4/6/6 | 16 | 65.86 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 320.00/352.00 | 352.00 | 384.00 | 192.00 | 2.00/4.00 | 2.00 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：首次主机重启；恢复后 TAP 池 500 不足；两个 attempt 分开保留。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-paced-full-l352-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-paced-full-l352-01/attempts/attempt-5a8872e49e2649cf/2d7f2f4888da043b689beb91/events/2d7f2f4888da043b689beb91.jsonl`

结果：`缺失`

## real-trace-p0-c640-paced-full-l352-01 / f-s384

开始：2026-10-18T09:46:10.405209+08:00；run=cancelled，保存的 arm=None；证据 ID `17d0405645-attempt-87111c818a3d441e-2d7f2f4888da043b689beb91`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 229.60 | 0 | 0.00000 | 2.9573 | 1.4634 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 25.93/41.62 | 71.02/148.27 | 71.18/148.44 | 0.00 | 0/0/0 | 0 | 3.23 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 320.00/352.00 | 352.00 | 384.00 | 192.00 | 2.00/4.00 | 2.00 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：首次主机重启；恢复后 TAP 池 500 不足；两个 attempt 分开保留。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-paced-full-l352-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-paced-full-l352-01/attempts/attempt-87111c818a3d441e/2d7f2f4888da043b689beb91/events/2d7f2f4888da043b689beb91.jsonl`

结果：`缺失`

## real-trace-p0-c640-paced-full-l352-02 / f-s384

开始：2026-10-18T10:34:49.300673+08:00；run=cancelled，保存的 arm=running；证据 ID `f1300a48eb-attempt-63b01b3701324682-2d7f2f4888da043b689beb91`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 1179.40 | 0 | 0.00000 | 2.9558 | 1.9052 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 23.24/42.11 | 225.59/291.00 | 226.47/291.01 | 28.39 | 10/50/43 | 16 | 179.46 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 320.00/352.00 | 352.00 | 384.00 | 192.00 | 2.00/4.00 | 2.00 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：过于保守的准入边界；50 次 VM checkpoint，可用于机制激活与排队诊断。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-paced-full-l352-02/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-paced-full-l352-02/attempts/attempt-63b01b3701324682/2d7f2f4888da043b689beb91/events/2d7f2f4888da043b689beb91.jsonl`

结果：`缺失`

## real-trace-p0-c640-relaxed-full-l640-01 / f-s384

开始：2026-10-18T11:03:16.512830+08:00；run=cancelled，保存的 arm=running；证据 ID `79d7a961fa-attempt-2683c13570654a7f-5d25bb001985108b77669e0a`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 1115.30 | 0 | 0.00000 | 2.8835 | 1.9206 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 29.39/55.35 | 295.82/419.37 | 295.85/419.39 | 0.00 | 0/0/0 | 0 | 37.30 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 512.00/640.00 | 640.00 | 384.00 | 192.00 | 2.00/4.00 | 2.00 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：S3lvol missing chunks/flush 失败；不能作策略对照。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-relaxed-full-l640-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-relaxed-full-l640-01/attempts/attempt-2683c13570654a7f/5d25bb001985108b77669e0a/events/5d25bb001985108b77669e0a.jsonl`

结果：`缺失`

## real-trace-p0-c640-relaxed-full-l640-02 / f-s384

开始：2026-10-18T12:07:43.522625+08:00；run=failed，保存的 arm=failed；证据 ID `e7d1df38e1-attempt-b91fe025ae1247cc-92df2534aaeb1f6958d7bfdd`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 2400.00 | 0 | 0.00000 | 1.3500 | 1.3550 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 23.29/50.83 | 405.16/485.31 | 405.21/485.31 | 0.00 | 0/0/0 | 2 | 51.94 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 512.00/640.00 | 640.00 | 384.00 | 192.00 | 2.00/4.00 | 2.00 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：执行期间 0 成功；最终化 model-gateway 未 drain；保留窗口数据，不认定完整结果。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

保存的失败原因：`638 (of 640) futures unfinished`

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-relaxed-full-l640-02/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-relaxed-full-l640-02/attempts/attempt-b91fe025ae1247cc/92df2534aaeb1f6958d7bfdd/events/92df2534aaeb1f6958d7bfdd.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-relaxed-full-l640-02/arms/92df2534aaeb1f6958d7bfdd.json`

## real-trace-p0-c640-main-full-l896-01 / f-s384

开始：2026-10-18T13:04:30.878588+08:00；run=cancelled，保存的 arm=running；证据 ID `f32aafa22a-attempt-0a78c934f47c4fd4-d6c9621e6cd9c440193b1389`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 697.26 | 0 | 0.00000 | 5.2592 | 3.3173 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 38.65/52.50 | 216.69/413.30 | 216.74/413.71 | 0.00 | 0/0/0 | 5 | 51.28 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 768.00/896.00 | 896.00 | 384.00 | 192.00 | 2.00/4.00 | 2.00 | 1.00 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：到达爬坡期提前终止；不能推断全部 640 到达后的争用。 arm 状态 running 是遗留元数据；run 已终止，不表示仍在运行。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。 配置恢复方法：policy identity SHA256 matches arm_id using saved historical policy; nonpolicy fields from frozen spec

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-main-full-l896-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-main-full-l896-01/attempts/attempt-0a78c934f47c4fd4/d6c9621e6cd9c440193b1389/events/d6c9621e6cd9c440193b1389.jsonl`

结果：`缺失`

## real-trace-p0-c640-balanced-full-s075-01 / f-s384

开始：2026-10-18T13:23:38.692542+08:00；run=failed，保存的 arm=timed_out；证据 ID `c9eb19f987-attempt-1421d77e952d413e-24aee1a5beea83932f271092`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 1200.00 | 0 | 0.00000 | 3.9008 | 2.7500 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 36.32/54.93 | 338.48/510.07 | 338.92/510.68 | 0.00 | 2/0/0 | 33 | 104.98 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 512.00/640.00 | 640.00 | 384.00 | 192.00 | 2.00/4.00 | 2.00 | 0.75 |

机制：admission=predicted，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：1200 s 窗口内按计划约 600/640 到达；0 成功；旧 forecast gate，borrow 实测成本远高于估计。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

保存的失败原因：`worker exceeded arm execution plus finalization timeout of 1380 seconds`

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-balanced-full-s075-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-balanced-full-s075-01/attempts/attempt-1421d77e952d413e/24aee1a5beea83932f271092/events/24aee1a5beea83932f271092.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-balanced-full-s075-01/arms/24aee1a5beea83932f271092.json`

## real-trace-p0-c640-balanced-aoff-s075-01 / a-off

开始：2026-10-18T13:49:30.945780+08:00；run=failed，保存的 arm=timed_out；证据 ID `2fd7dff074-attempt-22bfa16c787340ea-92b300785f814b194923dc62`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 1200.00 | 0 | 0.00000 | 4.0608 | 3.2917 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 35.25/53.36 | 337.86/535.18 | 338.74/537.90 | 0.00 | 6/0/0 | 83 | 74.42 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 928.00 | 512.00/640.00 | 640.00 | 384.00 | 192.00 | 2.00/4.00 | 2.00 | 0.75 |

机制：admission=off，idle=wait/agent_and_tool，borrow=pressure，snapshot=incremental-cow/warm-only，placement=load_aware，runtime=explicit。

解释：仅关闭工具准入，其他机制保留；不等于 OS-default；0 成功。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 采样存在 >30 s 空洞；内存均值为插值估计，峰值仅为已采到的峰值。

保存的失败原因：`worker exceeded arm execution plus finalization timeout of 1380 seconds`

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-balanced-aoff-s075-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-balanced-aoff-s075-01/attempts/attempt-22bfa16c787340ea/92b300785f814b194923dc62/events/92b300785f814b194923dc62.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-balanced-aoff-s075-01/arms/92b300785f814b194923dc62.json`

## real-trace-p0-c640-balanced-os-s075-01 / d-os-default

开始：2026-10-18T14:14:52.954283+08:00；run=cancelled，保存的 arm=failed；证据 ID `392c2cde34-attempt-7fe51f593fd243e4-d4638885ac7474bdca350fdc`。

| 并发 | 窗口 s | 成功 | Agents/s | Tool Steps/s | Model响应/s | P90执行 s | P90到达 s |
|---|---|---|---|---|---|---|---|
| 640 | 1200.00 | 0 | 0.00000 | 5.4008 | 3.3417 | — | — |

| CPU avg/peak % | LOCAL avg/peak GiB | VM cgroup avg/peak GiB | WARM peak GiB | borrow/checkpoint/restore | 独立失败 | 采样最大间隔 s |
|---|---|---|---|---|---|---|
| 47.28/82.87 | —/— | 345.00/580.23 | 0.00 | 0/0/0 | 183 | 4.40 |

| LOCAL容量GiB | LOW/HIGH GiB | pool GiB | WARM GiB | borrow cap GiB | Runtime/Tool GiB | 到达间隔 s | scale |
|---|---|---|---|---|---|---|---|
| 1120.00 | —/— | 1120.00 | 384.00 | — | 2.00/4.00 | 2.00 | 0.75 |

机制：admission=off，idle=off/agent_and_tool，borrow=off，snapshot=full-copy/tiered，placement=os_default，runtime=off。

解释：全部 ClawBox 策略关闭；0 成功，失败含创建/envd 初始化超时；最终化异常。 历史 wait 策略包含 LLM forecast 到期门控；不能代表修复后的实际空闲策略。 OS-default 的 local_used_bytes 实为整个 VM cgroup，不能解释为 NUMA 0/1 LOCAL；移入 VM_cgroup 列。

保存的失败原因：`cleanup timed out after 180 seconds`

配置：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-balanced-os-s075-01/experiment.yaml`

事件：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-balanced-os-s075-01/attempts/attempt-7fe51f593fd243e4/d4638885ac7474bdca350fdc/events/d4638885ac7474bdca350fdc.jsonl`

结果：`/home/weitianc/clawbox-c1280-20261017/results/real-trace-p0-c640-balanced-os-s075-01/arms/d4638885ac7474bdca350fdc.json`

