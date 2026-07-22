"""上下文管理策略（本实验的核心变量）。

三种策略：
- full          ：不做任何压缩，整段历史原样重发（基线：最贵、信息最全）。
- compact       ：估算 token 超过 budget 后，把「system 之后、最近 keep_recent 条之前」
                  的历史交给 LLM 摘要成一条消息替换（有损，与真实 harness 的 auto-compact 同构）。
- compact_clear ：在 compact 之前先做 tool-result clearing——把老的大块工具结果
                  原地替换成占位符（信息删除但结构保留），不够再触发摘要。

指令存活探针：每次 clear / compact 事件后，对任务的每条硬性约束做关键词命中检测，
记录该约束的文本是否还存在于上下文中——这是「初始指令在 compaction 后的存活率」的数据来源。
（关键词命中是保守近似：命中≠模型一定遵守，未命中≈大概率丢失；行为层由任务结束后的
约束检查器兜底，两层互为印证。）
"""
from .llm import estimate_tokens, history_tokens

STRATEGIES = ("full", "compact", "compact_clear")

SUMMARY_PROMPT = (
    "请把下面这段 agent 工作会话历史压缩成简明摘要，保留：任务目标、已完成的步骤、"
    "关键文件路径与关键发现、尚未完成的事项。丢弃冗长的文件内容原文。直接输出摘要正文。\n"
    "＝＝＝会话历史＝＝＝\n")

# 实验旋钮：摘要 prompt 是否显式要求逐条保留硬性约束原文（默认关——这正是要测的失效模式）
SUMMARY_PROMPT_CONSTRAINT_AWARE = (
    "请把下面这段 agent 工作会话历史压缩成简明摘要，保留：任务目标、任务中列出的每一条硬性约束"
    "（必须逐条原样保留，不得省略）、已完成的步骤、关键文件路径与关键发现、尚未完成的事项。"
    "丢弃冗长的文件内容原文。直接输出摘要正文。\n＝＝＝会话历史＝＝＝\n")


def _render(messages):
    parts = []
    for m in messages:
        role = m.get("role")
        content = str(m.get("content") or "")
        if m.get("tool_calls"):
            calls = ", ".join(c["function"]["name"] for c in m["tool_calls"])
            content = (content + " [调用工具: %s]" % calls).strip()
        parts.append("%s: %s" % (role, content))
    return "\n".join(parts)


def _context_text(history):
    """探针只看非 tool 消息：工具返回里天然含有 data/、docs/ 等任务域词，
    若计入会系统性高估约束存活率（探针污染）。约束文本只可能活在
    system / user（含摘要消息）/ assistant 的 content 里。"""
    return "\n".join(str(m.get("content") or "") for m in history
                     if m.get("role") != "tool")


class ContextManager:
    def __init__(self, strategy, budget_tokens=12000, keep_recent=6,
                 clear_min_tokens=150, constraint_aware_summary=False):
        assert strategy in STRATEGIES, strategy
        self.strategy = strategy
        self.budget = budget_tokens
        self.keep_recent = keep_recent
        self.clear_min_tokens = clear_min_tokens
        self.summary_prompt = (SUMMARY_PROMPT_CONSTRAINT_AWARE if constraint_aware_summary
                               else SUMMARY_PROMPT)
        self.events = []  # [{type, step, tokens_before, tokens_after, survival}]
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}  # summarizer 自身的开销也计入总账

    def _survival(self, history, constraints):
        text = _context_text(history)
        return {c.cid: c.probe(text) for c in constraints}

    def _safe_cut(self, history):
        """tail 起点不能落在 tool 消息上（不能拆散 assistant tool_calls / tool result 对）。"""
        start = max(1, len(history) - max(1, self.keep_recent))
        start = min(start, len(history) - 1)  # keep_recent<=0 时防越界
        while start > 1 and history[start].get("role") == "tool":
            start -= 1
        return start

    def prepare(self, history, llm, constraints, step):
        """就地重写 history（有损——与真实 harness 一致）。返回 history 本身。"""
        before = history_tokens(history)
        if before <= self.budget or self.strategy == "full":
            return history

        if self.strategy == "compact_clear":
            cut = self._safe_cut(history)
            cleared = 0
            for m in history[1:cut]:
                if (m.get("role") == "tool" and not m.get("_cleared")
                        and estimate_tokens(str(m.get("content") or "")) > self.clear_min_tokens):
                    m["content"] = ("[已清除的工具结果，原约 %d tokens。如需内容请重新调用工具]"
                                    % estimate_tokens(str(m["content"])))
                    m["_cleared"] = True
                    cleared += 1
            if cleared:
                self.events.append({"type": "clear", "step": step, "cleared": cleared,
                                    "tokens_before": before,
                                    "tokens_after": history_tokens(history),
                                    "survival": self._survival(history, constraints)})
            if history_tokens(history) <= self.budget:
                return history

        # compaction（compact 与 compact_clear 共用）
        cut = self._safe_cut(history)
        mid = history[1:cut]
        if len(mid) < 2:
            return history  # 没有可折叠的历史
        reply = llm.chat([{"role": "user",
                           "content": self.summary_prompt + _render(mid)}])
        self.usage["prompt_tokens"] += reply.usage.get("prompt_tokens", 0)
        self.usage["completion_tokens"] += reply.usage.get("completion_tokens", 0)
        summary_msg = {"role": "user",
                       "content": "[历史对话摘要（compaction 产物，此前细节已折叠）]\n"
                                  + (reply.content or "")}
        new_history = [history[0], summary_msg] + history[cut:]
        history[:] = new_history
        self.events.append({"type": "compact", "step": step,
                            "tokens_before": before,
                            "tokens_after": history_tokens(history),
                            "survival": self._survival(history, constraints)})
        return history
