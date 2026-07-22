"""受限工作区与工具集。

设计要点：
- 所有路径解析强制落在工作区内（防逃逸）。
- 工具结果超过 MAX_RESULT_CHARS 截断并提示「用 run_python 处理全量数据」——
  把 tool-call offloading（大数据下沉文件系统/代码执行，上下文只留摘要）内置进工具设计。
- 每次工具调用记录到 Workspace.tool_log，供约束检查器审计（如「done 前必须跑过测试」）。
"""
import os
import subprocess
import sys

MAX_RESULT_CHARS = 6000


class Workspace:
    def __init__(self, root):
        os.makedirs(os.path.abspath(root), exist_ok=True)
        self.root = os.path.realpath(root)  # realpath：符号链接也不允许逃逸
        self.tool_log = []  # [(tool_name, args_dict)]

    def resolve(self, path):
        p = os.path.realpath(os.path.join(self.root, path or "."))
        if p != self.root and not p.startswith(self.root + os.sep):
            raise ValueError("路径越出工作区: " + str(path))
        return p


TOOL_SCHEMAS = [
    {"name": "list_dir",
     "description": "列出工作区中某个目录的内容",
     "parameters": {"type": "object",
                    "properties": {"path": {"type": "string", "description": "相对路径，默认 ."}},
                    "required": []}},
    {"name": "read_file",
     "description": "读取工作区内的文本文件（超长会被截断，处理大文件请用 run_python）",
     "parameters": {"type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"]}},
    {"name": "write_file",
     "description": "写入（覆盖）工作区内的文本文件，自动创建父目录",
     "parameters": {"type": "object",
                    "properties": {"path": {"type": "string"},
                                   "content": {"type": "string"}},
                    "required": ["path", "content"]}},
    {"name": "run_python",
     "description": "在工作区根目录下执行一段 Python 代码（subprocess，20s 超时），返回 stdout+stderr。适合批量处理大文件、运行测试。",
     "parameters": {"type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"]}},
    {"name": "done",
     "description": "宣布任务完成（完成所有硬性约束后才允许调用）",
     "parameters": {"type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"]}},
]


def _truncate(text):
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return (text[:MAX_RESULT_CHARS]
            + "\n...[输出已截断，共 %d 字符。请改用 run_python 处理全量数据]" % len(text))


def execute(ws, name, args):
    """执行工具，永远返回字符串（异常转为错误信息喂回模型）。"""
    ws.tool_log.append((name, dict(args)))
    try:
        if name == "list_dir":
            p = ws.resolve(args.get("path", "."))
            entries = []
            for e in sorted(os.listdir(p)):
                full = os.path.join(p, e)
                entries.append(e + ("/" if os.path.isdir(full) else " (%dB)" % os.path.getsize(full)))
            return "\n".join(entries) or "(空目录)"
        if name == "read_file":
            with open(ws.resolve(args["path"]), "r", encoding="utf-8") as f:
                return _truncate(f.read())
        if name == "write_file":
            p = ws.resolve(args["path"])
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(args["content"])
            return "已写入 %s（%d 字符）" % (args["path"], len(args["content"]))
        if name == "run_python":
            # 环境裁剪：不继承父进程环境（尤其 API key 等敏感变量）。
            # 注意这只是最小隔离——无网络/资源隔离，跑真实模型建议在容器里做（见 README 局限）。
            clean_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                         "HOME": ws.root,
                         "LANG": os.environ.get("LANG", "C.UTF-8"),
                         "PYTHONDONTWRITEBYTECODE": "1"}
            proc = subprocess.run([sys.executable, "-c", args["code"]],
                                  cwd=ws.root, capture_output=True, text=True,
                                  timeout=20, env=clean_env)
            out = (proc.stdout or "") + (proc.stderr or "")
            return _truncate(out if out.strip() else "(无输出，exit=%d)" % proc.returncode)
        return "错误：未知工具 " + name
    except subprocess.TimeoutExpired:
        return "错误：run_python 超时（20s）"
    except Exception as e:  # noqa: BLE001 —— 错误也要喂回模型让它自愈
        return "错误：%s: %s" % (type(e).__name__, e)
