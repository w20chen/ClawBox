# M1 并发验证：3 个真实 agent 同时通过 M1 API 运行（2026-08-18）

## 结果

3 个并发 run 通过 M1 Managed API 提交（deadline=180s，真实 problem），全部 Cleaned/Succeeded，
全部产出真实 patch，0 泄漏。

| Cell | phase/outcome | agent turns | result | patch len |
|---|---|---|---|---|
| run-01m0af6vza59mxt42ag4nr8p3e-a1 | Cleaned/Succeeded | 22 | succeeded, exit 0, patch_status=present | **1335** |
| run-01m0af6w13asy5edjhz8eqwczz-a1 | Cleaned/Succeeded | 20 | succeeded, exit 0, patch_status=present | **1175** (= gold) |
| run-01m0af6w2vd2c0q0nqnx91kakp-a1 | Cleaned/Succeeded | 26 | succeeded, exit 0, patch_status=present | **1175** (= gold) |

- 3 个 run: `01M0AF6VZA59MXT42AG4NR8P3E` / `01M0AF6W13ASY5EDJHZ8EQWCZZ` / `01M0AF6W2VD2C0Q0NQNX91KAKP`
- 并发：3 CR 同时 RuntimeRunning，6 个 Firecracker 微 VM（3 tool + 3 runtime）真并发
- 每个 agent 真实 read/edit/exec/pytest，~20-26 turns
- 限时 180s 生效（agent 按时 stop，runtime 收割；finalize 因 SSH 瞬态稍慢，job grace 内完成）
- **patch_status=present**（限时下 agent 不 commit，工作区 diff 被正确收集；对比单 run 的 committed-patch 提取缺口）
- 泄漏检查：0 firecracker
