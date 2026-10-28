# compaction × KV cache 实验汇总

运行配置：`{"experiment": "compaction_cache", "backend": "deepseek", "model": "deepseek-flash", "budget": 1500, "runs": 2, "extra_body": {"thinking": {"type": "disabled"}}}`

| 策略 | run | 成功率 | 平均压缩/清除次数 | 平均 prompt tok | 总命中率 | 平常调用命中率 | 压缩后首次调用命中率（次数） | 清除后首次调用命中率（次数） | 摘要请求命中率 | 平均输入成本（未命中当量） | 相对 full |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| full | 10 | 80% | 0.0 / 0.0 | 7157 | 79% | 81% | n/a（0） | n/a（0） | n/a | 2077 | 1.00× |
| compact | 10 | 80% | 0.4 / 0.0 | 7012 | 69% | 82% | 20%（4） | n/a（0） | 0% | 2658 | 1.28× |
| compact_clear | 10 | 90% | 0.5 / 0.5 | 7648 | 68% | 83% | 16%（5） | 36%（2） | 11% | 2994 | 1.44× |

命中率 = hit / (hit + miss)，按 token 加权；输入成本 = miss + 0.1 × hit（未命中 token 当量），摘要请求计入成本。
