#!/usr/bin/env python3
"""实验矩阵入口：tasks × strategies × runs → results/results.jsonl + summary.md（可选出图）。

用法：
  python3 run_experiment.py --backend mock --budget 3000          # 管线自测（零成本、确定性）
  export DEEPSEEK_API_KEY=sk-...
  python3 run_experiment.py --backend deepseek --budget 12000 --runs 3
  python3 run_experiment.py --backend deepseek --constraint-aware-summary   # 摘要保约束对照组
"""
import argparse
import json
import os
import shutil
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness.agent import run_task            # noqa: E402
from harness.context import STRATEGIES, ContextManager  # noqa: E402
from harness.llm import make_llm              # noqa: E402
from harness.tools import Workspace           # noqa: E402
from tasks import ALL_TASKS                   # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="mock", choices=["mock", "deepseek", "openai-compat"])
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--strategies", default=",".join(STRATEGIES))
    ap.add_argument("--tasks", default="all", help="逗号分隔任务名，或 all")
    ap.add_argument("--budget", type=int, default=12000, help="触发上下文治理的 token 预算")
    ap.add_argument("--keep-recent", type=int, default=6)
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--runs", type=int, default=1, help="重复次数（控制非确定性，看方差）")
    ap.add_argument("--constraint-aware-summary", action="store_true",
                    help="compaction 摘要 prompt 显式要求逐条保留硬性约束（对照旋钮）")
    ap.add_argument("--tag", default="", help="结果文件名后缀")
    ap.add_argument("--plot", action="store_true", help="需要 matplotlib")
    args = ap.parse_args()

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    for s in strategies:
        assert s in STRATEGIES, "未知策略: " + s
    task_classes = (ALL_TASKS if args.tasks == "all"
                    else [t for t in ALL_TASKS if t.name in args.tasks.split(",")])
    assert task_classes, "没有匹配的任务"

    results_dir = os.path.join(ROOT, "results")
    tmp_dir = os.path.join(ROOT, "tmp")
    os.makedirs(results_dir, exist_ok=True)
    suffix = args.backend + (("-" + args.tag) if args.tag else "")
    out_path = os.path.join(results_dir, "results-%s.jsonl" % suffix)
    # 防覆盖：已有非空结果（可能是花真钱跑出来的）→ 自动加时间戳另存
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        suffix += "-" + time.strftime("%Y%m%d-%H%M%S")
        out_path = os.path.join(results_dir, "results-%s.jsonl" % suffix)
        print("已有同名结果，另存为: " + out_path)

    model_name = args.model or ("deepseek-chat" if args.backend == "deepseek" else args.backend)
    meta = {"backend": args.backend, "model": model_name, "budget": args.budget,
            "keep_recent": args.keep_recent,
            "constraint_aware_summary": args.constraint_aware_summary}

    records = []
    with open(out_path, "w", encoding="utf-8") as out:
        for run_i in range(args.runs):
            for task_cls in task_classes:
                for strategy in strategies:
                    run_id = "%s-%s-r%d" % (task_cls.name, strategy, run_i)
                    ws_root = os.path.join(tmp_dir, run_id)
                    shutil.rmtree(ws_root, ignore_errors=True)
                    ws = Workspace(ws_root)
                    llm = make_llm(args.backend, args.base_url, args.model)
                    ctx = ContextManager(strategy, budget_tokens=args.budget,
                                         keep_recent=args.keep_recent,
                                         constraint_aware_summary=args.constraint_aware_summary)
                    rec = run_task(task_cls(), ws, llm, ctx, max_steps=args.max_steps)
                    rec["run"] = run_i
                    rec.update(meta)  # 结果自带运行配置，mock/真实数据可从数据本身区分
                    records.append(rec)
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    out.flush()
                    print("[%-28s] success=%-5s steps=%-3d compactions=%d "
                          "prompt_tok=%-7d completion_tok=%-6d %s"
                          % (run_id, rec["success"], rec["steps"], rec["n_compactions"],
                             rec["prompt_tokens"], rec["completion_tokens"],
                             ("ERR: " + rec["error"]) if rec["error"] else ""))

    summary = summarize(records, strategies, meta)
    summary_path = os.path.join(results_dir, "summary-%s.md" % suffix)
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary)
    print("\n" + summary)
    print("明细: %s\n汇总: %s" % (out_path, summary_path))

    if args.plot:
        plot(records, strategies,
             os.path.join(results_dir, "overview-%s.png" % suffix))


def summarize(records, strategies, meta):
    lines = ["# 实验汇总", "",
             "运行配置：`%s`" % json.dumps(meta, ensure_ascii=False), "",
             "| 策略 | 任务成功率 | 约束行为达标率 | 约束文本存活率(有compaction的run) | "
             "平均prompt tok (±σ) | 平均completion tok | 平均compaction次数 |",
             "|---|---|---|---|---|---|---|"]
    for s in strategies:
        rs = [r for r in records if r["strategy"] == s]
        if not rs:
            continue
        n = len(rs)
        succ = sum(1 for r in rs if r["success"]) / n
        cons = [c["passed"] for r in rs for c in r["constraints"].values()]
        cons_rate = (sum(cons) / len(cons)) if cons else 0.0
        surv_flags = [v for r in rs if r["survival_after_last_compaction"]
                      for v in r["survival_after_last_compaction"].values()]
        surv = ("%.0f%%" % (100 * sum(surv_flags) / len(surv_flags))) if surv_flags else "n/a"
        pts = [r["prompt_tokens"] for r in rs]
        pt_sd = ("±%.0f" % statistics.pstdev(pts)) if len(pts) > 1 else ""
        avg_ct = sum(r["completion_tokens"] for r in rs) / n
        avg_cp = sum(r["n_compactions"] for r in rs) / n
        lines.append("| %s | %.0f%% | %.0f%% | %s | %.0f %s | %.0f | %.1f |"
                     % (s, succ * 100, cons_rate * 100, surv,
                        sum(pts) / n, pt_sd, avg_ct, avg_cp))
    if meta.get("backend") == "mock":
        lines += ["", "**⚠️ mock 数据仅验证管线，不可用于任何对比结论**（假摘要机制性偏向保留约束）。"]
    lines += ["", "注：约束文本存活率 = 最后一次 compaction 后，各硬性约束的独有短语仍在"
                  "非 tool 上下文中的比例（整句短语命中 = 保守口径，改写不计；行为层由约束检查器兜底）。"]
    return "\n".join(lines)


def plot(records, strategies, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:  # noqa: BLE001 —— numpy ABI 不兼容等环境问题也不应阻塞实验
        print("matplotlib 不可用，跳过出图（%s）" % e)
        return
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    succ, toks = [], []
    for s in strategies:
        rs = [r for r in records if r["strategy"] == s]
        succ.append(100 * sum(1 for r in rs if r["success"]) / max(1, len(rs)))
        toks.append(sum(r["prompt_tokens"] for r in rs) / max(1, len(rs)))
    axes[0].bar(strategies, succ, color="#1a5276")
    axes[0].set_title("Task success rate (%)")
    axes[1].bar(strategies, toks, color="#5a8bb0")
    axes[1].set_title("Avg prompt tokens / task")
    for ax in axes:
        ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    print("图已保存: " + path)


if __name__ == "__main__":
    main()
