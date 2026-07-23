#!/usr/bin/env python3
"""verifier 自测：正确解法应被验收通过；构造破坏场景应被 default-FAIL 拒绝且缺口分级正确。

零成本（MockLLM 回放正确解法），验证的是 verifier 判定逻辑本身，不是模型能力。
用法：python3 test_verifier.py
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness.agent import run_task            # noqa: E402
from harness.context import ContextManager    # noqa: E402
from harness.llm import MockLLM               # noqa: E402
from harness.tools import Workspace           # noqa: E402
from harness.verifier import DefaultFailVerifier  # noqa: E402
from tasks import BugfixSuite, CsvReport, StrictTotal  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
PASSED, FAILED = [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, detail))


def fresh_run(task_cls, run_name):
    """用 MockLLM 跑一遍正确解法，返回 (task, ws, rec)。"""
    ws_root = os.path.join(ROOT, "tmp", "selftest-" + run_name)
    shutil.rmtree(ws_root, ignore_errors=True)
    ws = Workspace(ws_root)
    task = task_cls()
    rec = run_task(task, ws, MockLLM(), ContextManager("full", budget_tokens=10**9),
                   use_verifier=True)
    return task, ws, rec


def gap_classes(report):
    return sorted({f["gap_class"] for f in report.failures})


def main():
    print("== 1. 正确解法应验收通过 ==")
    for task_cls in (CsvReport, BugfixSuite, StrictTotal):
        task, ws, rec = fresh_run(task_cls, task_cls.name)
        check("%s: 首次声明即通过验收" % task_cls.name,
              rec["finished_called_done"] and rec["claims"]
              and rec["claims"][0]["verifier_passed"] is True)
        check("%s: 最终真值成立" % task_cls.name, rec["final_truth_ok"])

    print("== 2. 破坏场景应被拒绝且分级正确 ==")
    # A 类：删除交付物
    task, ws, _ = fresh_run(CsvReport, "corrupt-a")
    os.remove(os.path.join(ws.root, "out", "REPORT.md"))
    report = DefaultFailVerifier(task.evidence()).verify(ws)
    check("删除 REPORT.md → 拒绝，含 A 类", not report.passed and "A" in gap_classes(report),
          str(gap_classes(report)))

    # B 类：篡改数值（确定性：把首个条目的数值 +1）
    import re
    task, ws, _ = fresh_run(CsvReport, "corrupt-b")
    p = os.path.join(ws.root, "out", "REPORT.md")
    content = re.sub(r"(- \w+: )(\d+)",
                     lambda m: m.group(1) + str(int(m.group(2)) + 1),
                     open(p, encoding="utf-8").read(), count=1)
    open(p, "w", encoding="utf-8").write(content)
    report = DefaultFailVerifier(task.evidence()).verify(ws)
    check("篡改报告数值 → 拒绝，含 B 类", not report.passed and "B" in gap_classes(report),
          str(gap_classes(report)))

    # D 类：动了禁改文件
    task, ws, _ = fresh_run(CsvReport, "corrupt-d")
    with open(os.path.join(ws.root, "data", "sales_1.csv"), "a", encoding="utf-8") as f:
        f.write("\nalpha,1")
    report = DefaultFailVerifier(task.evidence()).verify(ws)
    check("篡改 data/ → 拒绝，含 D 类", not report.passed and "D" in gap_classes(report),
          str(gap_classes(report)))

    # B 类：把修好的代码换回带 bug 版本（模拟「没修完就喊 done」）
    task, ws, _ = fresh_run(BugfixSuite, "corrupt-bugfix")
    with open(os.path.join(ws.root, "pkg2", "stats.py"), "w", encoding="utf-8") as f:
        f.write(BugfixSuite.STATS_BUGGY)
    report = DefaultFailVerifier(task.evidence()).verify(ws)
    check("换回带 bug 实现 → 拒绝，含 B 类", not report.passed and "B" in gap_classes(report),
          str(gap_classes(report)))

    # B 类：违反业务口径（不排除 void，模拟约束被压缩丢失后的错误产物）
    task, ws, _ = fresh_run(StrictTotal, "corrupt-void")
    import json
    total_all = 0
    rows = 0
    with open(os.path.join(ws.root, "data", "orders.csv"), encoding="utf-8") as f:
        next(f)
        for row in f:
            if row.strip():
                total_all += int(row.strip().split(",")[1])
                rows += 1
    with open(os.path.join(ws.root, "out", "total.json"), "w", encoding="utf-8") as f:
        json.dump({"total": total_all, "excluded_rows": 0, "counted_rows": rows}, f)
    report = DefaultFailVerifier(task.evidence()).verify(ws)
    check("未排除 void 行 → 拒绝，含 B 类", not report.passed and "B" in gap_classes(report),
          str(gap_classes(report)))

    print("\n结果：%d 通过 / %d 失败" % (len(PASSED), len(FAILED)))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
