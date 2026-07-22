# 实验汇总

运行配置：`{"backend": "deepseek", "model": "deepseek-chat", "budget": 3000, "keep_recent": 6, "constraint_aware_summary": false}`

| 策略 | 任务成功率 | 约束行为达标率 | 约束文本存活率(有compaction的run) | 平均prompt tok (±σ) | 平均completion tok | 平均compaction次数 |
|---|---|---|---|---|---|---|
| full | 100% | 100% | n/a | 12885 ±3854 | 994 | 0.0 |
| compact | 100% | 100% | 33% | 12799 ±4410 | 1054 | 0.4 |
| compact_clear | 100% | 100% | 33% | 12482 ±2960 | 1024 | 0.2 |

注：约束文本存活率 = 最后一次 compaction 后，各硬性约束的独有短语仍在非 tool 上下文中的比例（整句短语命中 = 保守口径，改写不计；行为层由约束检查器兜底）。