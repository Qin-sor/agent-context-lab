#!/usr/bin/env python3
"""compaction × KV cache 实验：压缩重写历史之后，前缀缓存还剩多少。

KV cache 前缀实验（run_cache_experiment.py）把上下文策略固定为 full，留下一个问题：
compaction 把早期历史换成一条摘要，压缩点之后的前缀全部改变，按前缀匹配的缓存在压缩后的
那次调用上应当大面积未命中；compact_clear 把老的工具结果原地换成占位符，同样改变前缀。
本实验沿用主实验的 5 个任务与 token 预算，对比 full / compact / compact_clear 三种策略：

- 每次调用记录 API 返回的 prompt_cache_hit_tokens / prompt_cache_miss_tokens；
- agent 调用分为四类：每个 run 的第一次调用、压缩之后的第一次调用、清除之后的第一次调用、
  其余平常调用；ContextManager 内部的摘要请求（不带 tools）单列；
- 输入成本按「命中价 = 未命中价的 1/10」（DeepSeek 定价口径）折算成未命中 token 当量，
  与原始 prompt token 一起对比：compaction 省下的 token，有多少被缓存失效吃回去。

用法：
  python3 run_compaction_cache_experiment.py --backend mock                    # 链路自测（mock 无缓存字段）
  python3 run_compaction_cache_experiment.py --backend deepseek --model deepseek-flash --runs 2
"""
import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness.agent import run_task            # noqa: E402
from harness.context import ContextManager    # noqa: E402
from harness.llm import MockLLM, make_llm     # noqa: E402
from harness.tools import Workspace           # noqa: E402
from tasks import ConfigMigrate, CsvReport, DocIndex, LogHunt, RenameRefactor  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
TASKS = [CsvReport, RenameRefactor, LogHunt, DocIndex, ConfigMigrate]
STRATEGIES = ("full", "compact", "compact_clear")
KINDS = ("first", "steady", "after_compact", "after_clear", "summary")
HIT_PRICE = 0.1   # 命中价 / 未命中价


class CallRecorder:
    """记录每次调用的缓存命中；ctx.events 在两次 agent 调用之间变长，说明刚压缩或清除过。"""

    def __init__(self, inner, ctx, before_first_call=None):
        self.inner = inner
        self.ctx = ctx
        self._before_first_call = before_first_call
        self.calls = []
        self._seen_events = 0
        self._agent_calls = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _kind(self, tools):
        if not tools:
            return "summary"
        new = {e["type"] for e in self.ctx.events[self._seen_events:]}
        self._seen_events = len(self.ctx.events)
        self._agent_calls += 1
        if "compact" in new:
            return "after_compact"
        if "clear" in new:
            return "after_clear"
        return "first" if self._agent_calls == 1 else "steady"

    def chat(self, messages, tools=None, **kw):
        if self._before_first_call:
            self._before_first_call()
            self._before_first_call = None
        kind = self._kind(tools)
        t0 = time.time()
        reply = self.inner.chat(messages, tools, **kw)
        u = reply.usage
        self.calls.append({"kind": kind,
                           "hit": u.get("prompt_cache_hit_tokens", 0),
                           "miss": u.get("prompt_cache_miss_tokens", 0),
                           "prompt": u.get("prompt_tokens", 0),
                           "latency_s": round(time.time() - t0, 3)})
        return reply


def _ratio(hit, miss):
    return hit / (hit + miss) if (hit + miss) else None


def _pct(x):
    return "n/a" if x is None else "%.0f%%" % (100 * x)


def summarize(records, meta):
    lines = ["# compaction × KV cache 实验汇总", "",
             "运行配置：`%s`" % json.dumps(meta, ensure_ascii=False), "",
             "| 策略 | run | 成功率 | 平均压缩/清除次数 | 平均 prompt tok | 总命中率 | 平常调用命中率 | 压缩后首次调用命中率（次数） | 清除后首次调用命中率（次数） | 摘要请求命中率 | 平均输入成本（未命中当量） | 相对 full |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    base_cost = None
    rows = []
    for strat in STRATEGIES:
        rs = [r for r in records if r["strategy"] == strat]
        if not rs:
            continue
        calls = [c for r in rs for c in r["calls"]]
        agg = {k: [0, 0, 0] for k in KINDS}
        for c in calls:
            a = agg[c["kind"]]
            a[0] += c["hit"]; a[1] += c["miss"]; a[2] += 1
        hit = sum(c["hit"] for c in calls)
        miss = sum(c["miss"] for c in calls)
        cost = (miss + HIT_PRICE * hit) / len(rs)
        if strat == "full":
            base_cost = cost
        rows.append((strat, rs, agg, hit, miss, cost))
    for strat, rs, agg, hit, miss, cost in rows:
        n = len(rs)
        lines.append("| %s | %d | %.0f%% | %.1f / %.1f | %.0f | %s | %s | %s（%d） | %s（%d） | %s | %.0f | %s |" % (
            strat, n, 100 * sum(1 for r in rs if r["success"]) / n,
            sum(r["n_compactions"] for r in rs) / n, sum(r["n_clears"] for r in rs) / n,
            sum(r["prompt_tokens"] for r in rs) / n,
            _pct(_ratio(hit, miss)), _pct(_ratio(*agg["steady"][:2])),
            _pct(_ratio(*agg["after_compact"][:2])), agg["after_compact"][2],
            _pct(_ratio(*agg["after_clear"][:2])), agg["after_clear"][2],
            _pct(_ratio(*agg["summary"][:2])), cost,
            ("%.2f×" % (cost / base_cost)) if base_cost else "-"))
    lines += ["", "命中率 = hit / (hit + miss)，按 token 加权；输入成本 = miss + %.1f × hit（未命中 token 当量），"
              "摘要请求计入成本。" % HIT_PRICE]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backend", default="mock", choices=["mock", "deepseek", "openai-compat"])
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--budget", type=int, default=1500, help="触发上下文管理的 token 预算（与主实验相同）")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--thinking", action="store_true", help="不关闭思考（默认对 deepseek 后端关闭）")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    extra = None if (args.thinking or args.backend != "deepseek") else {"thinking": {"type": "disabled"}}
    results_dir = os.path.join(ROOT, "results")
    os.makedirs(results_dir, exist_ok=True)
    suffix = "compaction-cache-%s%s" % (args.backend, ("-" + args.tag) if args.tag else "")
    out_path = os.path.join(results_dir, "results-%s.jsonl" % suffix)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        suffix += "-" + time.strftime("%Y%m%d-%H%M%S")
        out_path = os.path.join(results_dir, "results-%s.jsonl" % suffix)
        print("已有同名结果，另存为: " + out_path)
    model_name = args.model or ("deepseek-chat" if args.backend == "deepseek" else args.backend)
    meta = {"experiment": "compaction_cache", "backend": args.backend, "model": model_name,
            "budget": args.budget, "runs": args.runs, "extra_body": extra}

    records = []
    with open(out_path, "w", encoding="utf-8") as out:
        for run_i in range(args.runs):
            for task_cls in TASKS:
                for strat in STRATEGIES:
                    run_id = "%s-%s-r%d" % (task_cls.name, strat, run_i)
                    ws_root = os.path.join(ROOT, "tmp", "ccache-" + run_id)
                    shutil.rmtree(ws_root, ignore_errors=True)
                    ws = Workspace(ws_root)
                    ctx = ContextManager(strat, budget_tokens=args.budget)
                    task = task_cls()
                    inner = make_llm(args.backend, args.base_url, args.model, extra)
                    # run_task 只对 MockLLM 实例装载回放计划；包了一层记录器后改为第一次调用前装载
                    # （回放计划依赖 task.setup 之后才有的真值）
                    bind = ((lambda t=task, w=ws, m=inner: m.bind(t.mock_plan(w)))
                            if isinstance(inner, MockLLM) else None)
                    llm = CallRecorder(inner, ctx, before_first_call=bind)
                    rec = run_task(task, ws, llm, ctx, max_steps=30)
                    rec.update(meta)
                    rec.update({"run": run_i, "calls": llm.calls})
                    records.append(rec)
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    out.flush()
                    hit = sum(c["hit"] for c in llm.calls)
                    miss = sum(c["miss"] for c in llm.calls)
                    print("[%-30s] success=%-5s 压缩=%d 清除=%d 命中率=%s calls=%d"
                          % (run_id, rec["success"], rec["n_compactions"], rec["n_clears"],
                             _pct(_ratio(hit, miss)), len(llm.calls)))

    summary = summarize(records, meta)
    summary_path = os.path.join(results_dir, "summary-%s.md" % suffix)
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary)
    print("\n" + summary)
    print("明细: %s\n汇总: %s" % (out_path, summary_path))


if __name__ == "__main__":
    main()
