# agent-context-lab

一个**最小 coding-agent harness + 上下文管理消融实验**：量化回答一个问题——

> Agent 的上下文管理策略（compaction / tool-result clearing）在多步任务上到底换来了多少 token 节省、又付出了多少「初始指令丢失」的代价？

灵感来自真实踩坑：长程 agent 任务中，compaction 之后 agent 会「忘掉」任务开头立下的硬性约束（不许改某文件、输出必须带某标记……），表面上任务照常推进，实际上约束已经悄悄被违反。本实验把这个现象做成可复现、可量化的对照实验。

## 设计

**最小 harness**（`harness/`，纯标准库、零第三方依赖）：

- `agent.py` — agent loop：system + 任务指令 → (上下文准备 → 模型 → 工具执行) 循环 → done
- `tools.py` — 受限工作区 + 5 个工具（list_dir / read_file / write_file / run_python / done）；超长工具结果截断并提示改用 run_python——把 tool-call offloading 内置进工具设计
- `context.py` — 三种上下文策略（**本实验的自变量**）：
  - `full`：不压缩，整段历史重发（基线：最贵、信息最全）
  - `compact`：超过 token 预算后，把早期历史 LLM 摘要成一条替换（与真实 harness 的 auto-compact 同构，有损）
  - `compact_clear`：先把老的大块工具结果原地替换成占位符（结构保留、内容清除），不够再摘要
- `llm.py` — OpenAI 兼容后端（DeepSeek / OpenAI / Kimi / Qwen…）+ MockLLM（端到端自测）

**任务集**（`tasks.py`）：5 个多步骤真实风格任务（CSV 汇总 / 跨文件重命名重构 / 大日志统计 / 文档索引 / 配置迁移），每个任务带 **3 条硬性约束**，且工作区刻意放大文件制造上下文压力，逼 compaction 真实触发。

**双层测量**是本实验的关键设计——每条约束同时有：

1. **文本层探针**：每次 compaction/clear 事件后，检测约束关键词是否还存在于上下文中（存活率）；
2. **行为层检查器**：任务结束后在工作区里验证 agent 是否真的遵守了（文件 hash 对比、输出格式校验、工具调用审计——例如「done 前必须跑过测试」直接查 tool_log）。

文本消失是因、行为违规是果，两层数据放在一起就能回答「约束是在哪次压缩中丢的、丢了之后有没有真的违反」。

## 快速开始

```bash
# 1. 端到端自测（MockLLM 确定性回放正确解法，零成本，只验证整条链路、不产生科学数据）
python3 run_experiment.py --backend mock --budget 3000

# 2. 真实实验（DeepSeek API，5 任务 × 3 策略 × 3 重复 ≈ 数元人民币）
export DEEPSEEK_API_KEY=sk-...
python3 run_experiment.py --backend deepseek --budget 12000 --runs 3 --plot

# 3. 对照开关：compaction 摘要 prompt 显式要求逐条保留硬性约束——测「prompt 修复」能挽回多少存活率
python3 run_experiment.py --backend deepseek --constraint-aware-summary --tag ca

# 其他 OpenAI 兼容端点
python3 run_experiment.py --backend openai-compat --base-url https://api.example.com/v1 --model xxx
```

输出：`results/results-*.jsonl`（逐 run 明细，含每次 compaction 事件的存活快照）+ `results/summary-*.md`（按策略汇总：任务成功率 / 约束行为达标率 / 约束文本存活率 / token 成本 / compaction 次数）。

## 首轮实测结果（deepseek-chat · budget 1500 · 5 任务 × 3 约束 × 3 重复 × 2 组 = 90 run · 零协议错误）

| 策略 | 任务成功率 | 约束行为达标率 | 约束文本存活率 | 平均 prompt tok (±σ) | 平均 compaction 次数 |
|---|---|---|---|---|---|
| full | 100% | 100% | —（不压缩） | 14872 ±6296 | 0 |
| compact | 100% | 100% | **8%** | 13583 ±3974 | 1.4 |
| compact_clear | 100% | 100% | **15%** | 12482 ±3263 | 0.9 |
| compact + 保约束摘要 | 100% | 100% | **83%** | 12988 ±4013 | 1.2 |
| compact_clear + 保约束摘要 | 100% | 100% | **90%** | 12012 ±2595 | 0.7 |

**结论：**

1. **默认摘要式 compaction 是约束粉碎机**：压缩后约束文本存活率仅 8%——「压缩丢指令」被强证实；
2. **一行 prompt 修复挽回约束保真**：摘要 prompt 显式要求逐条保留硬性约束 → 8%→83%、15%→90%，token 成本持平；
3. **compact_clear 全面占优**：存活更高、token 最省、压缩次数最少——清工具结果换回上下文余量、推迟了有损摘要；
4. **诚实边界**：行为违规率 0%——任务仅 4-7 步、约束动作多在压缩前已完成，文本丢失尚未传导为行为违规；观察传导需要更长任务（见 Roadmap）；
5. **实验插曲（测量口径也是实验的一部分）**：token 估算器首版 `len//3` 对中文低估约 3 倍，导致 budget 3000 时压缩触发率近零、实验现象整体失灵——已修为 CJK 感知估算（中文 ~1 字/token）。

原始数据：`results/results-deepseek-{main2,ca}.jsonl` · 汇总：`results/summary-deepseek-{main2,ca}.md`

## 局限（诚实声明）

- 探针只在**非 tool 消息**里找约束句的**独有完整短语**（工具返回天然含任务域词，计入会造成探针污染、系统性高估存活率）；整句命中是保守口径——摘要若改写了约束（语义还在、字面变了）不计存活，结论以行为层检查器为准；
- token 触发阈值用字符数粗估（真实成本统计用 API 返回的 usage）；
- `run_python` 仅做环境裁剪级隔离（不继承 API key 等环境变量、realpath 路径校验），**无网络/资源隔离**——接真实模型建议整个实验在容器里跑；
- 卡死熔断：连续 3 次不调用工具即终止该 run（防止真实 API 下空耗预算）；
- 任务规模刻意做小（预算约束下让 compaction 高频触发），结论的外推需谨慎；
- MockLLM 仅验证链路，其「摘要」是截断假摘要（约束恰好在被截留的开头，机制性偏向保留），不可用于任何对比结论。

## Roadmap

- [x] deepseek-chat 真实数据 + 方差（两组矩阵共 90 run）→ 见上方「首轮实测结果」
- [ ] **长任务版**：验证「文本丢失 → 行为违规」的传导（首轮违规率 0%，任务过短未传导——需 20+ 步、约束动作分布在压缩前后的任务设计）
- [ ] 逐约束归因：每条约束丢在第几次 compaction（context_events 里已有原始数据，待分析脚本）
- [ ] prefix cache 命中率维度（DeepSeek API 返回 cache hit 字段，比较三策略对缓存友好度）
- [ ] tool_choice=required 强制工具调用（替代熔断 nudge）
- [ ] 更多策略：滑动窗口丢弃 / 分层摘要 / 约束下沉到 system prompt（CLAUDE.md 模式）对照
