"""最小 agent loop：system + 任务指令 → (上下文准备 → 模型 → 工具执行) 循环 → done。

verifier 集成：use_verifier=True 时，agent 每次调用 done 都要过 default-FAIL 验收——
不通过则把缺失证据清单作为 done 的工具返回喂回去（最多打回 max_rejects 次，
第 max_rejects 次打回即终止本次任务）。
同时在每次 done 声明时刻记录隐藏真值（任务总检 + 全部约束，verifier 不可见），
用于事后计算虚假完成率 / 拦截率 / 漏检率 / 误拦率。
"""
import time

from . import tools
from .llm import MockLLM
from .verifier import DefaultFailVerifier

SYSTEM_PROMPT = (
    "你是在受限工作区内工作的自主编码 agent。用提供的工具完成任务。规则：\n"
    "1) 严格遵守任务中列出的每一条硬性约束；\n"
    "2) 大文件用 run_python 批量处理，不要反复 read 全文；\n"
    "3) 完成并逐条自查硬性约束后，调用 done。")


def _ground_truth_ok(task, ws):
    """实验评估用的隐藏真值（verifier 不可见）：任务总检 + 全部约束。"""
    try:
        if not task.check(ws):
            return False
        return all(c.check(ws) for c in task.constraints)
    except Exception:  # noqa: BLE001
        return False


def run_task(task, ws, llm, ctx, max_steps=30, use_verifier=False, max_rejects=2,
             prompt_suffix=""):
    """执行单个任务，返回指标 dict。"""
    task.prepare(ws)
    verifier = DefaultFailVerifier(task.evidence()) if use_verifier else None
    if isinstance(llm, MockLLM):
        llm.bind(task.mock_plan(ws))

    user_content = task.prompt() + (("\n\n" + prompt_suffix) if prompt_suffix else "")
    history = [{"role": "system", "content": SYSTEM_PROMPT},
               {"role": "user", "content": user_content}]
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    api_calls = 0
    finished = False
    error = None
    no_tool_streak = 0
    truncated_replies = 0
    rejections = 0
    claims = []  # 每次 done 声明：{"step", "truth_ok", "verifier_passed", "failures"?}
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
            # 逐一回填 tool 消息，done 最后统一处理——避免 done 排在前面时丢调用。
            done_call_id = None
            for call in reply.tool_calls:
                if call["name"] == "done":
                    if done_call_id is None:
                        done_call_id = call["id"]
                    else:  # 同轮重复 done：也必须回填回执，否则孤儿 tool_call_id 会打破协议
                        history.append({"role": "tool", "tool_call_id": call["id"],
                                        "content": "重复的 done 调用，已忽略（以第一次为准）"})
                    continue
                result = tools.execute(ws, call["name"], call["arguments"])
                history.append({"role": "tool", "tool_call_id": call["id"],
                                "content": result})

            if done_call_id is not None:
                claim = {"step": step, "truth_ok": _ground_truth_ok(task, ws)}
                if verifier is None:
                    claim["verifier_passed"] = None
                    claims.append(claim)
                    history.append({"role": "tool", "tool_call_id": done_call_id,
                                    "content": "已记录完成声明"})
                    finished = True
                    break
                report = verifier.verify(ws)
                claim["verifier_passed"] = report.passed
                if not report.passed:
                    claim["failures"] = report.failures
                claims.append(claim)
                if report.passed:
                    history.append({"role": "tool", "tool_call_id": done_call_id,
                                    "content": "验收通过"})
                    finished = True
                    break
                rejections += 1
                if rejections >= max_rejects:
                    history.append({"role": "tool", "tool_call_id": done_call_id,
                                    "content": report.reject_message()
                                    + "\n（已达最大打回次数，终止本次任务）"})
                    break
                history.append({"role": "tool", "tool_call_id": done_call_id,
                                "content": report.reject_message()})
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
    final_truth_ok = _ground_truth_ok(task, ws)

    # 最后一次 compaction 事件时各约束的文本存活情况（无 compaction 则为 None）
    last_compact = next((ev for ev in reversed(ctx.events) if ev["type"] == "compact"), None)
    return {
        "task": task.name,
        "strategy": ctx.strategy,
        "success": success,
        "final_truth_ok": final_truth_ok,
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
        "verifier_enabled": use_verifier,
        "verifier_rejections": rejections,
        "claims": claims,
        "duration_s": round(time.time() - t0, 2),
        "error": error,
    }
