"""最小 agent loop：system + 任务指令 → (上下文策略整备 → 模型 → 工具执行) 循环 → done。"""
import time

from . import tools
from .llm import MockLLM

SYSTEM_PROMPT = (
    "你是在受限工作区内工作的自主编码 agent。用提供的工具完成任务。规则：\n"
    "1) 严格遵守任务中列出的每一条硬性约束；\n"
    "2) 大文件用 run_python 批量处理，不要反复 read 全文；\n"
    "3) 完成并逐条自查硬性约束后，调用 done。")


def run_task(task, ws, llm, ctx, max_steps=30):
    """执行单个任务，返回指标 dict。"""
    task.prepare(ws)
    if isinstance(llm, MockLLM):
        llm.bind(task.mock_plan(ws))

    history = [{"role": "system", "content": SYSTEM_PROMPT},
               {"role": "user", "content": task.prompt()}]
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    api_calls = 0
    finished = False
    error = None
    no_tool_streak = 0
    truncated_replies = 0
    t0 = time.time()
    step = 0

    try:
        for step in range(1, max_steps + 1):
            history = ctx.prepare(history, llm, task.constraints, step)
            reply = llm.chat(history, tools=tools.TOOL_SCHEMAS)
            api_calls += 1
            usage["prompt_tokens"] += reply.usage.get("prompt_tokens", 0)
            usage["completion_tokens"] += reply.usage.get("completion_tokens", 0)
            if getattr(reply, "finish_reason", None) == "length":
                truncated_replies += 1
            history.append(reply.raw)

            if not reply.tool_calls:
                # 熔断：连续不调工具说明卡死，提前终止，别拿真钱空耗 max_steps
                no_tool_streak += 1
                if no_tool_streak >= 3:
                    error = "stuck: 连续 %d 次未调用工具，熔断终止" % no_tool_streak
                    break
                history.append({"role": "user",
                                "content": "请继续用工具推进任务；全部完成并自查约束后调用 done。"})
                continue
            no_tool_streak = 0

            # 同批可能有多个 tool_calls 且顺序不保证：先执行完所有非 done 调用并
            # 逐一回填 tool 消息（done 也回占位，保证每个 tool_call_id 都有配对回复），
            # 最后才统一判断是否结束——避免 done 排在前面时丢掉同批的其他调用。
            done_seen = False
            for call in reply.tool_calls:
                if call["name"] == "done":
                    done_seen = True
                    history.append({"role": "tool", "tool_call_id": call["id"],
                                    "content": "已记录完成声明"})
                    continue
                result = tools.execute(ws, call["name"], call["arguments"])
                history.append({"role": "tool", "tool_call_id": call["id"],
                                "content": result})
            if done_seen:
                finished = True
                break
    except Exception as e:  # noqa: BLE001 —— 记录失败而非中断整个实验矩阵
        error = "%s: %s" % (type(e).__name__, e)

    constraint_results = {}
    for c in task.constraints:
        try:
            constraint_results[c.cid] = {"text": c.text, "passed": bool(c.check(ws))}
        except Exception as e:  # noqa: BLE001
            constraint_results[c.cid] = {"text": c.text, "passed": False,
                                         "check_error": str(e)}
    try:
        success = bool(task.check(ws)) and error is None
    except Exception as e:  # noqa: BLE001
        success = False
        error = error or ("check: %s" % e)

    # 最后一次 compaction 事件时各约束的文本存活情况（无 compaction 则为 None）
    last_compact = next((ev for ev in reversed(ctx.events) if ev["type"] == "compact"), None)
    return {
        "task": task.name,
        "strategy": ctx.strategy,
        "success": success,
        "finished_called_done": finished,
        "steps": step,
        "api_calls": api_calls + len([e for e in ctx.events if e["type"] == "compact"]),
        "prompt_tokens": usage["prompt_tokens"] + ctx.usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"] + ctx.usage["completion_tokens"],
        "n_compactions": len([e for e in ctx.events if e["type"] == "compact"]),
        "n_clears": len([e for e in ctx.events if e["type"] == "clear"]),
        "truncated_replies": truncated_replies,
        "constraints": constraint_results,
        "survival_after_last_compaction": (last_compact["survival"] if last_compact else None),
        "context_events": ctx.events,
        "duration_s": round(time.time() - t0, 2),
        "error": error,
    }
