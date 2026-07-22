# 实验汇总

运行配置：`{"backend": "mock", "model": "mock", "budget": 1200, "keep_recent": 6, "constraint_aware_summary": false}`

| 策略 | 任务成功率 | 约束行为达标率 | 约束文本存活率(有compaction的run) | 平均prompt tok (±σ) | 平均completion tok | 平均compaction次数 |
|---|---|---|---|---|---|---|
| full | 100% | 100% | n/a | 8129 ±2952 | 163 | 0.0 |
| compact | 100% | 100% | 25% | 8270 ±3006 | 776 | 2.4 |
| compact_clear | 100% | 100% | 25% | 7599 ±2509 | 719 | 2.2 |

**⚠️ mock 数据仅验证链路，不可用于任何对比结论**（假摘要机制性偏向保留约束）。

注：约束文本存活率 = 最后一次 compaction 后，各硬性约束的独有短语仍在非 tool 上下文中的比例（整句短语命中 = 保守口径，改写不计；行为层由约束检查器兜底）。