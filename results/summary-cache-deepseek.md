# KV cache 前缀实验汇总

运行配置：`{"experiment": "cache_prefix", "backend": "deepseek", "model": "deepseek-chat"}`

| 前缀设计 | run 数 | 任务成功率 | 总命中/总miss (tokens) | 加权命中率 | 平均每调用延迟 |
|---|---|---|---|---|---|
| volatile_system | 6 | 100% | 19840 / 72943 | 21% | 1.79s |
| stable_prefix | 6 | 100% | 79488 / 8971 | 90% | 1.64s |

口径：两组注入完全相同的心跳信息，仅位置不同（system prompt 尾部 vs 临时尾部 user 消息，后者不写入持久历史）；命中数据来自 DeepSeek API 返回的 prompt_cache_hit_tokens / prompt_cache_miss_tokens。上下文策略固定 full。