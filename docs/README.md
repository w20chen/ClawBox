# docs/ 索引

> 按用途分类。**先读这几个**：`CAPABILITIES.md`（已支持）· `GAPS.md`（未完成）· `AGENT_HANDOFF_2026-08-18-managed-sandbox-roadmap.md`（总路线图）。

## 权威现状（新 session 先读）
| 文档 | 内容 |
|---|---|
| `CAPABILITIES.md` | 已支持功能（代码核实，含 ✅🟢🟡 状态） |
| `GAPS.md` | 未完成工作（代码核实，含 🔴🟠🟡⚪ 区分） |
| `AGENT_HANDOFF_2026-08-18-managed-sandbox-roadmap.md` | **权威总路线图**（M0-M7、验收口径、不允许的回退） |
| `IMPLEMENTATION_MAPPING.md` | 目标 ↔ 实现 ↔ 验证 索引 |
| `ADRs/` | 架构决策记录 ADR-001..010 |

## 交接记录（按日期，历史证据）
| 文档 | 内容 |
|---|---|
| `AGENT_HANDOFF_2026-08-16.md` | 早期基线 |
| `AGENT_HANDOFF_2026-08-17.md` / `-e2e.md` / `-e2e-round2.md` / `-real-task.md` / `-runtime.md` | 单任务 MVP + 真机验证记录（FC-0 18/18、host gate 19/19、live smoke） |
| `AGENT_HANDOFF_2026-08-19-kb-tenant-repo-concurrency.md` | KB × 租户 × repo + 并发 |
| `AGENT_HANDOFF_2026-08-19-p0-accepted.md` | P0 真实任务验收（join_rate=1.0、DeepSeek、patch present） |
| `AGENT_HANDOFF_2026-08-19-p0-real-task-blocked.md` | P0 被 devmapper 卡住记录（现已可解：免密 sudo） |
| `AGENT_HANDOFF_2026-08-19-research-next.md` | 科研向下一步（监控+预测+KB 闭环 / 并发 / Execution ID join） |

## 专项/发现（FINDING）
| 文档 | 内容 |
|---|---|
| `FINDING_2026-08-18-m1-live-validation.md` | M1 实况验证发现 |
| `FINDING_2026-08-18-scale32-fd-exhaustion.md` | kata shim FD 上限（~19 cells 并发墙） |
| `CONCURRENT_KATA_SWE.md` | 并发 Kata SWE 记录 |
| `OPENEUER_ARM64.md` | openEuler ARM64 部署细节/回滚边界 |
| `PHASE3.md` | 阶段 3 备注 |

## 证据（真机产物）
| 文件 | 内容 |
|---|---|
| `m1-concurrent-3x-evidence.md` | 3 并发真实任务全 Cleaned/Succeeded |
| `m1-real-task-evidence.txt` / `m1-real-task-patch-c1ad059.txt` | 单真实任务正确 patch（c1ad059，与 gold PR#13 一致） |
