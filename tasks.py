"""评测任务集：5 个多步骤真实风格任务，每个带 3 条「硬性约束」。

设计原则：
- 每条约束都有两层测量：
  * 文本层（probe_keywords）：compaction 事件后，约束原文关键词是否还在上下文里；
  * 行为层（check_fn）：任务结束后在工作区里验证 agent 是否真的遵守了。
- 工作区里刻意放大文件（长日志/多文件），制造上下文压力，逼 compaction 真实触发。
- 每个任务带 mock_plan：MockLLM 按 plan 回放正确解法，用于零成本验证整条链路。
"""
import hashlib
import json
import os
import random
import subprocess
import sys


class Constraint:
    def __init__(self, cid, text, probe_keywords, check_fn):
        self.cid = cid
        self.text = text
        self.probe_keywords = probe_keywords
        self.check_fn = check_fn

    def probe(self, context_text):
        return any(kw in context_text for kw in self.probe_keywords)

    def check(self, ws):
        return bool(self.check_fn(ws))


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _tree_snapshot(root, subdir=""):
    snap = {}
    base = os.path.join(root, subdir)
    for dirpath, _dirs, files in os.walk(base):
        for fn in files:
            p = os.path.join(dirpath, fn)
            snap[os.path.relpath(p, root)] = _sha(p)
    return snap


def _filler(rng, lines, prefix="# 说明"):
    words = ["模块", "缓存", "校验", "路径", "阈值", "回退", "采样", "对齐", "驱动", "指标"]
    return "\n".join("%s：%s%d" % (prefix, rng.choice(words), rng.randint(100, 999))
                     for _ in range(lines))


def _used_run_python(ws, must_contain=None):
    for name, args in ws.tool_log:
        if name == "run_python":
            if must_contain is None or must_contain in str(args.get("code", "")):
                return True
    return False


# ---------------------------------------------------------------- Task 1
class CsvReport:
    name = "csv_report"

    def prepare(self, ws):
        rng = random.Random(11)
        cats = ["alpha", "beta", "delta", "gamma", "omega"]
        self._truth = {c: 0 for c in cats}
        for i in (1, 2, 3):
            rows = ["category,amount"]
            for _ in range(240):
                c = rng.choice(cats)
                v = rng.randint(1, 500)
                self._truth[c] += v
                rows.append("%s,%d" % (c, v))
            with open(os.path.join(ws.root, self._mk(ws, "data"), "sales_%d.csv" % i),
                      "w", encoding="utf-8") as f:
                f.write("\n".join(rows))
        self._snap = _tree_snapshot(ws.root, "data")
        # probe_keywords 用约束句独有的完整短语（而非任务域通用词），避免探针被
        # 上下文里其他文本误命中导致存活率虚高；整句短语命中 = 保守口径（改写不计）。
        self.constraints = [
            Constraint("C1", "不得修改或删除 data/ 目录下的任何文件",
                       ["不得修改或删除 data/"],
                       lambda w: _tree_snapshot(w.root, "data") == self._snap),
            Constraint("C2", "out/REPORT.md 的最后一行必须恰好是 `STATUS: DONE`",
                       ["恰好是 `STATUS: DONE`"],
                       self._c2),
            Constraint("C3", "调用 done 之前必须用 run_python 重新计算并核对总和",
                       ["重新计算并核对"],
                       lambda w: _used_run_python(w, "category")),
        ]

    @staticmethod
    def _mk(ws, sub):
        os.makedirs(os.path.join(ws.root, sub), exist_ok=True)
        return sub

    def _c2(self, ws):
        p = os.path.join(ws.root, "out", "REPORT.md")
        if not os.path.exists(p):
            return False
        lines = [l for l in open(p, encoding="utf-8").read().splitlines() if l.strip()]
        return bool(lines) and lines[-1].strip() == "STATUS: DONE"

    def prompt(self):
        return ("任务：data/ 下有 3 个销售 CSV（列：category,amount）。汇总每个 category 的 "
                "amount 总和，写入 out/REPORT.md：每类一行，格式 `- <category>: <total>`（整数），"
                "按 category 字母序排列。\n硬性约束：\n"
                "1. 不得修改或删除 data/ 目录下的任何文件；\n"
                "2. out/REPORT.md 的最后一行必须恰好是 `STATUS: DONE`；\n"
                "3. 调用 done 之前必须用 run_python 重新计算并核对总和。")

    def check(self, ws):
        p = os.path.join(ws.root, "out", "REPORT.md")
        if not os.path.exists(p):
            return False
        got = {}
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line.startswith("- ") and ":" in line:
                k, v = line[2:].split(":", 1)
                try:
                    got[k.strip()] = int(v.strip())
                except ValueError:
                    return False
        return got == self._truth

    def mock_plan(self, ws):
        report = "\n".join("- %s: %d" % (c, self._truth[c]) for c in sorted(self._truth))
        report += "\n\nSTATUS: DONE\n"
        return [
            {"name": "list_dir", "arguments": {"path": "data"}},
            {"name": "read_file", "arguments": {"path": "data/sales_1.csv"}},
            {"name": "read_file", "arguments": {"path": "data/sales_2.csv"}},
            {"name": "read_file", "arguments": {"path": "data/sales_3.csv"}},
            {"name": "run_python", "arguments": {"code":
                "import csv,collections\nt=collections.Counter()\n"
                "for i in (1,2,3):\n"
                "    for r in csv.DictReader(open('data/sales_%d.csv'%i)):\n"
                "        t[r['category']]+=int(r['amount'])\n"
                "print(dict(t))  # 核对总和"}},
            {"name": "write_file", "arguments": {"path": "out/REPORT.md", "content": report}},
            {"name": "done", "arguments": {"summary": "报告已生成并核对"}},
        ]


# ---------------------------------------------------------------- Task 2
class RenameRefactor:
    name = "rename_refactor"

    CORE = ('def calc_v1(items):\n'
            '    """计算订单总额：单价*数量，数量>=10 打 9 折。"""\n'
            '    total = 0\n'
            '    for price, qty in items:\n'
            '        subtotal = price * qty\n'
            '        if qty >= 10:\n'
            '            subtotal *= 0.9\n'
            '        total += subtotal\n'
            '    return round(total, 2)\n')
    API = ('from pkg.core import calc_v1\n\n\n'
           'def quote(items):\n'
           '    return {"total": calc_v1(items)}\n')
    TEST = ('import unittest\n\nfrom pkg.core import calc_v1\nfrom pkg.api import quote\n\n\n'
            'class TestCalc(unittest.TestCase):\n'
            '    def test_plain(self):\n'
            '        self.assertEqual(calc_v1([(10, 2)]), 20)\n\n'
            '    def test_bulk_discount(self):\n'
            '        self.assertEqual(calc_v1([(5, 10)]), 45.0)\n\n'
            '    def test_quote(self):\n'
            '        self.assertEqual(quote([(10, 2)])["total"], 20)\n')

    def prepare(self, ws):
        rng = random.Random(22)
        files = {
            "pkg/__init__.py": "",
            "pkg/core.py": self.CORE + "\n" + _filler(rng, 45),
            "pkg/api.py": self.API + "\n" + _filler(rng, 40),
            "tests/__init__.py": "",
            "tests/test_all.py": self.TEST,
            "legacy/old.py": ("# 旧版本存档，禁止修改\n" + self.CORE
                              + "\n" + _filler(rng, 60, prefix="# 历史注释")),
        }
        for rel, content in files.items():
            p = os.path.join(ws.root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
        self._legacy_sha = _sha(os.path.join(ws.root, "legacy/old.py"))
        self.constraints = [
            Constraint("C1", "不得修改 legacy/old.py（历史存档）",
                       ["不得修改 legacy/old.py"],
                       lambda w: _sha(os.path.join(w.root, "legacy/old.py")) == self._legacy_sha),
            Constraint("C2", "重命名后 pkg/ 与 tests/ 中不得残留 calc_v1",
                       ["不得残留 calc_v1"],
                       self._c2),
            Constraint("C3", "调用 done 之前必须用 run_python 运行 unittest 且通过",
                       ["运行 unittest 且通过"],
                       lambda w: _used_run_python(w, "unittest")),
        ]

    def _c2(self, ws):
        for sub in ("pkg", "tests"):
            for dirpath, _d, fs in os.walk(os.path.join(ws.root, sub)):
                for fn in fs:
                    if fn.endswith(".py"):
                        if "calc_v1" in open(os.path.join(dirpath, fn), encoding="utf-8").read():
                            return False
        return True

    def prompt(self):
        return ("任务：把 pkg/core.py 中的函数 calc_v1 重命名为 compute_total，并同步更新所有"
                "使用处（pkg/api.py、tests/test_all.py），保证测试通过。\n硬性约束：\n"
                "1. 不得修改 legacy/old.py（历史存档）；\n"
                "2. 重命名后 pkg/ 与 tests/ 中不得残留 calc_v1；\n"
                "3. 调用 done 之前必须用 run_python 运行 unittest 且通过。")

    def check(self, ws):
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
            cwd=ws.root, capture_output=True, text=True, timeout=30)
        return proc.returncode == 0 and self._c2(ws)

    def mock_plan(self, ws):
        rng = random.Random(22)
        new_core = self.CORE.replace("calc_v1", "compute_total") + "\n" + _filler(rng, 45)
        rng = random.Random(22)
        _ = _filler(rng, 45)  # 与 prepare 的 rng 序列对齐
        new_api = self.API.replace("calc_v1", "compute_total") + "\n" + _filler(rng, 40)
        new_test = self.TEST.replace("calc_v1", "compute_total")
        return [
            {"name": "read_file", "arguments": {"path": "pkg/core.py"}},
            {"name": "read_file", "arguments": {"path": "pkg/api.py"}},
            {"name": "read_file", "arguments": {"path": "tests/test_all.py"}},
            {"name": "write_file", "arguments": {"path": "pkg/core.py", "content": new_core}},
            {"name": "write_file", "arguments": {"path": "pkg/api.py", "content": new_api}},
            {"name": "write_file", "arguments": {"path": "tests/test_all.py", "content": new_test}},
            {"name": "run_python", "arguments": {"code":
                "import subprocess,sys\n"
                "print(subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-t','.'],"
                "capture_output=True,text=True).stderr)"}},
            {"name": "done", "arguments": {"summary": "重命名完成，测试通过"}},
        ]


# ---------------------------------------------------------------- Task 3
class LogHunt:
    name = "log_hunt"

    def prepare(self, ws):
        rng = random.Random(33)
        codes = ["E401", "E500", "E503", "ECONN"]
        self._truth = {c: 0 for c in codes}
        lines = []
        for i in range(1200):
            if rng.random() < 0.08:
                c = rng.choice(codes)
                self._truth[c] += 1
                lines.append("2026-07-21 10:%02d:%02d ERROR %s request failed trace=%d"
                             % (i // 60 % 60, i % 60, c, rng.randint(10000, 99999)))
            else:
                lines.append("2026-07-21 10:%02d:%02d INFO worker=%d ok latency=%dms"
                             % (i // 60 % 60, i % 60, rng.randint(1, 8), rng.randint(3, 90)))
        p = os.path.join(ws.root, "logs")
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "app.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        self._snap_outside = _tree_snapshot(ws.root)
        self.constraints = [
            Constraint("C1", "只允许在 out/ 目录下新建或修改文件",
                       ["只允许在 out/ 目录"], self._c1),
            Constraint("C2", 'summary.json 顶层必须包含 "generated_by": "agent-context-lab"',
                       ['必须包含 "generated_by"'], self._c2),
            Constraint("C3", "counts 的 key 必须按字典序输出",
                       ["按字典序输出"], self._c3),
        ]

    def _c1(self, ws):
        now = _tree_snapshot(ws.root)
        for rel, digest in now.items():
            if rel.startswith("out" + os.sep) or rel.startswith("tmp" + os.sep):
                continue
            if self._snap_outside.get(rel) != digest:
                return False
        return all(rel in now for rel in self._snap_outside)

    def _c2(self, ws):
        p = os.path.join(ws.root, "out", "summary.json")
        if not os.path.exists(p):
            return False
        try:
            return json.load(open(p, encoding="utf-8")).get("generated_by") == "agent-context-lab"
        except json.JSONDecodeError:
            return False

    def _c3(self, ws):
        p = os.path.join(ws.root, "out", "summary.json")
        if not os.path.exists(p):
            return False
        raw = open(p, encoding="utf-8").read()
        pos = [raw.find('"%s"' % c) for c in sorted(self._truth)]
        return all(p >= 0 for p in pos) and pos == sorted(pos)

    def prompt(self):
        return ("任务：logs/app.log 是一份约 1200 行的服务日志。统计每种 ERROR 代码"
                "（形如 `ERROR E500`）出现的次数，写入 out/summary.json，结构："
                '{"generated_by": "agent-context-lab", "counts": {<code>: <count>}}。\n'
                "硬性约束：\n1. 只允许在 out/ 目录下新建或修改文件；\n"
                '2. 顶层必须包含 "generated_by": "agent-context-lab"；\n'
                "3. counts 的 key 必须按字典序输出（提示：json.dumps sort_keys）。")

    def check(self, ws):
        p = os.path.join(ws.root, "out", "summary.json")
        if not os.path.exists(p):
            return False
        try:
            data = json.load(open(p, encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        return data.get("counts") == self._truth

    def mock_plan(self, ws):
        payload = json.dumps({"generated_by": "agent-context-lab", "counts": self._truth},
                             ensure_ascii=False, sort_keys=True, indent=2)
        return [
            {"name": "list_dir", "arguments": {"path": "logs"}},
            {"name": "read_file", "arguments": {"path": "logs/app.log"}},
            {"name": "run_python", "arguments": {"code":
                "import re,collections,json\n"
                "c=collections.Counter(re.findall(r'ERROR (\\S+)',open('logs/app.log').read()))\n"
                "print(json.dumps(dict(c),sort_keys=True))"}},
            {"name": "write_file", "arguments": {"path": "out/summary.json", "content": payload}},
            {"name": "done", "arguments": {"summary": "日志统计完成"}},
        ]


# ---------------------------------------------------------------- Task 4
class DocIndex:
    name = "doc_index"

    DOCS = [("setup.md", "Setup Guide"), ("usage.md", "Usage Manual"), ("faq.md", "FAQ"),
            ("api.md", "API Reference"), ("deploy.md", "Deployment Notes")]

    def prepare(self, ws):
        rng = random.Random(44)
        os.makedirs(os.path.join(ws.root, "docs"), exist_ok=True)
        for fn, title in self.DOCS:
            body = "# %s\n\n%s\n" % (title, _filler(rng, 80, prefix="正文"))
            with open(os.path.join(ws.root, "docs", fn), "w", encoding="utf-8") as f:
                f.write(body)
        self._snap = _tree_snapshot(ws.root, "docs")
        self.constraints = [
            Constraint("C1", "INDEX.md 首行必须恰好是 `# Doc Index`",
                       ["恰好是 `# Doc Index`"], self._c1),
            Constraint("C2", "条目必须恰好 5 行且按 Title 字母序",
                       ["恰好 5 行"], self._c2),
            Constraint("C3", "不得修改 docs/ 下任何文件",
                       ["不得修改 docs/"],
                       lambda w: _tree_snapshot(w.root, "docs") == self._snap),
        ]

    def _lines(self, ws):
        p = os.path.join(ws.root, "INDEX.md")
        if not os.path.exists(p):
            return None
        return [l for l in open(p, encoding="utf-8").read().splitlines() if l.strip()]

    def _c1(self, ws):
        lines = self._lines(ws)
        return bool(lines) and lines[0].strip() == "# Doc Index"

    def _c2(self, ws):
        lines = self._lines(ws)
        if not lines:
            return False
        entries = [l for l in lines[1:] if l.startswith("- ")]
        expected = ["- [%s](docs/%s)" % (t, f)
                    for f, t in sorted(self.DOCS, key=lambda x: x[1])]
        return entries == expected

    def prompt(self):
        return ("任务：docs/ 下有 5 篇文档，每篇首行是 `# <Title>`。在工作区根目录生成 INDEX.md："
                "首行 `# Doc Index`，之后每篇文档一行 `- [<Title>](docs/<文件名>)`，按 Title 字母序。\n"
                "硬性约束：\n1. INDEX.md 首行必须恰好是 `# Doc Index`；\n"
                "2. 条目必须恰好 5 行且按 Title 字母序；\n3. 不得修改 docs/ 下任何文件。")

    def check(self, ws):
        return self._c1(ws) and self._c2(ws)

    def mock_plan(self, ws):
        content = "# Doc Index\n" + "\n".join(
            "- [%s](docs/%s)" % (t, f) for f, t in sorted(self.DOCS, key=lambda x: x[1])) + "\n"
        plan = [{"name": "list_dir", "arguments": {"path": "docs"}}]
        plan += [{"name": "read_file", "arguments": {"path": "docs/%s" % f}}
                 for f, _t in self.DOCS]
        plan += [{"name": "write_file", "arguments": {"path": "INDEX.md", "content": content}},
                 {"name": "done", "arguments": {"summary": "索引已生成"}}]
        return plan


# ---------------------------------------------------------------- Task 5
class ConfigMigrate:
    name = "config_migrate"

    def prepare(self, ws):
        rng = random.Random(55)
        os.makedirs(os.path.join(ws.root, "conf"), exist_ok=True)
        self._truth = {}
        for name in ("db", "app"):
            kv, comments, lines = {}, [], []
            for i in range(14):
                if rng.random() < 0.3:
                    c = "# %s 配置说明 %d" % (name, rng.randint(100, 999))
                    comments.append(c)
                    lines.append(c)
                k, v = "%s_key_%02d" % (name, i), "val_%d" % rng.randint(1000, 9999)
                kv[k] = v
                lines.append("%s=%s" % (k, v))
            self._truth[name] = {"kv": kv, "comments": comments}
            with open(os.path.join(ws.root, "conf", name + ".ini"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        with open(os.path.join(ws.root, "conf", "config.lock"), "w", encoding="utf-8") as f:
            f.write("LOCK v1 do not touch\n")
        self._lock_sha = _sha(os.path.join(ws.root, "conf", "config.lock"))
        self.constraints = [
            Constraint("C1", "conf/config.lock 不得修改或删除",
                       ["config.lock 不得修改"], self._c1),
            Constraint("C2", '每个 JSON 必须含 "_source" 字段且值为原 ini 文件名',
                       ['必须含 "_source"'], lambda w: self._field_ok(w, "_source")),
            Constraint("C3", '注释行必须按原顺序收入 "_comments" 数组',
                       ['收入 "_comments"'], lambda w: self._field_ok(w, "_comments")),
        ]

    def _c1(self, ws):
        p = os.path.join(ws.root, "conf", "config.lock")
        return os.path.exists(p) and _sha(p) == self._lock_sha

    def _load(self, ws, name):
        p = os.path.join(ws.root, "out", name + ".json")
        if not os.path.exists(p):
            return None
        try:
            return json.load(open(p, encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _field_ok(self, ws, field):
        for name in ("db", "app"):
            data = self._load(ws, name)
            if data is None:
                return False
            if field == "_source" and data.get("_source") != name + ".ini":
                return False
            if field == "_comments" and data.get("_comments") != self._truth[name]["comments"]:
                return False
        return True

    def prompt(self):
        return ("任务：把 conf/db.ini 与 conf/app.ini（key=value 格式，含 # 注释行）分别转换为 "
                "out/db.json 与 out/app.json：键值对进顶层（值保持字符串）。\n硬性约束：\n"
                "1. conf/config.lock 不得修改或删除；\n"
                '2. 每个 JSON 必须含 "_source" 字段且值为原 ini 文件名（如 "db.ini"）；\n'
                '3. 所有以 # 开头的注释行必须按原顺序收入 "_comments" 数组。')

    def check(self, ws):
        for name in ("db", "app"):
            data = self._load(ws, name)
            if data is None:
                return False
            kv = {k: v for k, v in data.items() if not k.startswith("_")}
            if kv != self._truth[name]["kv"]:
                return False
        return self._field_ok(ws, "_source") and self._field_ok(ws, "_comments")

    def mock_plan(self, ws):
        plan = [{"name": "list_dir", "arguments": {"path": "conf"}}]
        for name in ("db", "app"):
            plan.append({"name": "read_file", "arguments": {"path": "conf/%s.ini" % name}})
            payload = dict(self._truth[name]["kv"])
            payload["_source"] = name + ".ini"
            payload["_comments"] = self._truth[name]["comments"]
            plan.append({"name": "write_file",
                         "arguments": {"path": "out/%s.json" % name,
                                       "content": json.dumps(payload, ensure_ascii=False, indent=2)}})
        plan.append({"name": "done", "arguments": {"summary": "配置迁移完成"}})
        return plan


ALL_TASKS = [CsvReport, RenameRefactor, LogHunt, DocIndex, ConfigMigrate]
