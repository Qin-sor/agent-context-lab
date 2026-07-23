#!/usr/bin/env python3
"""KV cache 前缀设计实验：易变信息放哪，决定前缀缓存的死活。

对照的两种 harness 前缀设计（每次调用注入同一条"心跳"易变信息，仅位置不同）：
- volatile_system：心跳追加进 system prompt —— 每次调用前缀从第 0 个 token 起就变，
  预期缓存全 miss（这是真实 harness 里常见的反模式：把当前时间/剩余预算塞 system）；
- stable_prefix ：system 与历史保持逐字节稳定，心跳作为一条临时尾部 user 消息注入
  （不写入持久历史）—— 预期历史前缀大量命中。

测量：DeepSeek API 每次返回 prompt_cache_hit_tokens / prompt_cache_miss_tokens，
直接计算命中率；同时记录每次调用延迟。上下文策略固定为 full（不压缩）——
compaction 会重写历史、本身就是另一种缓存破坏源，留作后续实验单独对照。

用法：
  python3 run_cache_experiment.py --backend mock              # 端到端自测（mock 无缓存字段，仅验链路）
  python3 run_cache_experiment.py --backend deepseek --runs 2
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
from harness.llm import make_llm              # noqa: E402
from harness.tools import Workspace           # noqa: E402
from tasks import CsvReport, DocIndex, LogHunt  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
TASKS = [CsvReport, DocIndex, LogHunt]
VARIANTS = ("volatile_system", "stable_prefix")


class PrefixVariantLLM:
    """按变体改写每次请求的消息（不改持久历史），并记录缓存命中与延迟。"""

    def __init__(self, inner, variant):
        assert variant in VARIANTS
        self.inner = inner
        self.variant = variant
        self.tick = 0
        self.calls = []  # 每次调用：{hit, miss, prompt, latency_s}

    def chat(self, messages, tools=None):
        self.tick += 1
        heartbeat = "[会话心跳 tick=%d，时间片 %d]" % (self.tick, 1700000000 + self.tick)
        msgs = [dict(m) for m in messages]
        if self.variant == "volatile_system":
            msgs[0]["content"] = str(msgs[0]["content"]) + "\n" + heartbeat
        else:
            msgs = msgs + [{"role": "user", "content": heartbeat}]
        t0 = time.time()
        reply = self.inner.chat(msgs, tools)
        u = reply.usage
        self.calls.append({"hit": u.get("prompt_cache_hit_tokens", 0),
                           "miss": u.get("prompt_cache_miss_tokens", 0),
                           "prompt": u.get("prompt_tokens", 0),
                           "latency_s": round(time.time() - t0, 3)})
        return reply


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="mock", choices=["mock", "deepseek", "openai-compat"])
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    results_dir = os.path.join(ROOT, "results")
    os.makedirs(results_dir, exist_ok=True)
    suffix = "cache-%s%s" % (args.backend, ("-" + args.tag) if args.tag else "")
    out_path = os.path.join(results_dir, "results-%s.jsonl" % suffix)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        suffix += "-" + time.strftime("%Y%m%d-%H%M%S")
        out_path = os.path.join(results_dir, "results-%s.jsonl" % suffix)
        print("已有同名结果，另存为: " + out_path)

    model_name = args.model or ("deepseek-chat" if args.backend == "deepseek" else args.backend)
    meta = {"experiment": "cache_prefix", "backend": args.backend, "model": model_name}

    records = []
    with open(out_path, "w", encoding="utf-8") as out:
        for run_i in range(args.runs):
            for task_cls in TASKS:
                for variant in VARIANTS:
                    run_id = "%s-%s-r%d" % (task_cls.name, variant, run_i)
                    ws_root = os.path.join(ROOT, "tmp", "cache-" + run_id)
                    shutil.rmtree(ws_root, ignore_errors=True)
                    ws = Workspace(ws_root)
                    llm = PrefixVariantLLM(make_llm(args.backend, args.base_url, args.model),
                                           variant)
                    ctx = ContextManager("full", budget_tokens=10**9)
                    rec = run_task(task_cls(), ws, llm, ctx, max_steps=30)
                    hit = sum(c["hit"] for c in llm.calls)
                    miss = sum(c["miss"] for c in llm.calls)
                    rec.update(meta)
                    rec.update({"variant": variant, "run": run_i,
                                "cache_hit_tokens": hit, "cache_miss_tokens": miss,
                                "cache_hit_ratio": round(hit / (hit + miss), 4)
                                                   if (hit + miss) else None,
                                "avg_call_latency_s": round(
                                    sum(c["latency_s"] for c in llm.calls)
                                    / max(1, len(llm.calls)), 3),
                                "calls": llm.calls})
                    records.append(rec)
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    out.flush()
                    print("[%-34s] success=%-5s 命中率=%s hit=%-6d miss=%-6d 平均延迟=%.2fs"
                          % (run_id, rec["success"],
                             ("%.0f%%" % (100 * rec["cache_hit_ratio"]))
                             if rec["cache_hit_ratio"] is not None else "n/a",
                             hit, miss, rec["avg_call_latency_s"]))

    summary = summarize(records, meta)
    summary_path = os.path.join(results_dir, "summary-%s.md" % suffix)
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary)
    print("\n" + summary)
    print("明细: %s\n汇总: %s" % (out_path, summary_path))


def summarize(records, meta):
    lines = ["# KV cache 前缀实验汇总", "",
             "运行配置：`%s`" % json.dumps(meta, ensure_ascii=False), "",
             "| 前缀设计 | run 数 | 任务成功率 | 总命中/总miss (tokens) | 加权命中率 | 平均每调用延迟 |",
             "|---|---|---|---|---|---|"]
    for variant in VARIANTS:
        rs = [r for r in records if r["variant"] == variant]
        if not rs:
            continue
        n = len(rs)
        succ = sum(1 for r in rs if r["success"]) / n
        hit = sum(r["cache_hit_tokens"] for r in rs)
        miss = sum(r["cache_miss_tokens"] for r in rs)
        ratio = ("%.0f%%" % (100 * hit / (hit + miss))) if (hit + miss) else "n/a"
        lat = sum(r["avg_call_latency_s"] for r in rs) / n
        lines.append("| %s | %d | %.0f%% | %d / %d | %s | %.2fs |"
                     % (variant, n, succ * 100, hit, miss, ratio, lat))
    lines += ["",
              "口径：两组注入完全相同的心跳信息，仅位置不同（system prompt 尾部 vs 临时尾部 "
              "user 消息，后者不写入持久历史）；命中数据来自 DeepSeek API 返回的 "
              "prompt_cache_hit_tokens / prompt_cache_miss_tokens。上下文策略固定 full。"]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
