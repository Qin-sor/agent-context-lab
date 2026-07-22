# 实验汇总

运行配置：`{"backend": "deepseek", "model": "deepseek-chat", "budget": 1500, "keep_recent": 6, "constraint_aware_summary": true}`

| 策略 | 任务成功率 | 约束行为达标率 | 约束文本存活率(有compaction的run) | 平均prompt tok (±σ) | 平均completion tok | 平均compaction次数 |
|---|---|---|---|---|---|---|
| full | 100% | 100% | n/a | 12762 ±3460 | 958 | 0.0 |
| compact | 100% | 100% | 83% | 12988 ±4013 | 1407 | 1.2 |
| compact_clear | 100% | 100% | 90% | 12012 ±2595 | 1182 | 0.7 |

注：约束文本存活率 = 最后一次 compaction 后，各硬性约束的独有短语仍在非 tool 上下文中的比例（整句短语命中 = 保守口径，改写不计；行为层由约束检查器兜底）。