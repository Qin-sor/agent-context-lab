"""LLM 后端适配层。

- OpenAICompatLLM：任何 OpenAI 兼容端点（DeepSeek: base_url=https://api.deepseek.com,
  model=deepseek-chat；也兼容 OpenAI / Kimi / Qwen 等）。零第三方依赖（urllib）。
- MockLLM：确定性脚本模型，按任务给定的 plan 依次发工具调用。
  只用于零成本验证整条链路（agent loop / 工具 / 上下文策略 / 指标统计），
  不产生有科学意义的对比数据——真实数据必须用真实 API 跑。
"""
import json
import os
import time
import urllib.error
import urllib.request


class LLMReply:
    def __init__(self, content=None, tool_calls=None, usage=None, raw=None,
                 finish_reason=None):
        self.content = content
        # tool_calls: [{"id": str, "name": str, "arguments": dict}]
        self.tool_calls = tool_calls or []
        self.usage = usage or {"prompt_tokens": 0, "completion_tokens": 0}
        # raw: 直接回填到对话历史里的 assistant message（保留 tool_calls 原始格式）
        self.raw = raw or {"role": "assistant", "content": content or ""}
        self.finish_reason = finish_reason  # "length" = 输出被截断，需在指标里标记


def estimate_tokens(text):
    """CJK 感知的 token 粗估：中文 ~1 字/token，ASCII ~4 字符/token。
    （首版用 len//3 对中文低估约 3 倍，导致真实 API 上压缩长期无法触发——
    估算口径错误会让实验现象整体失灵，此处是修复后的口径。）
    触发阈值用估算，成本统计用 API 返回的真实 usage。"""
    t = str(text)
    cjk = sum(1 for ch in t if "一" <= ch <= "鿿")
    return max(1, cjk + (len(t) - cjk) // 4)


def message_tokens(message):
    total = estimate_tokens(str(message.get("content") or ""))
    for call in message.get("tool_calls") or []:
        total += estimate_tokens(json.dumps(call, ensure_ascii=False))
    return total + 8  # 消息结构开销


def history_tokens(history):
    return sum(message_tokens(m) for m in history)


class OpenAICompatLLM:
    def __init__(self, base_url, model, api_key=None, temperature=0.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.api_key = (api_key or os.environ.get("DEEPSEEK_API_KEY")
                        or os.environ.get("OPENAI_API_KEY"))
        if not self.api_key:
            raise RuntimeError("未找到 API key：请先 export DEEPSEEK_API_KEY=... （或 OPENAI_API_KEY）")

    def chat(self, messages, tools=None):
        payload = {"model": self.model, "messages": messages,
                   "temperature": self.temperature, "max_tokens": 4096}
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.api_key})
        data = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                # 读出响应体（真正的报错原因在 body 里）；4xx（除 429）不重试
                try:
                    body = e.read().decode("utf-8", "replace")[:500]
                except Exception:  # noqa: BLE001
                    body = str(e.reason)
                err = RuntimeError("HTTP %d: %s" % (e.code, body))
                if (e.code == 429 or e.code >= 500) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise err from None
            except Exception:  # 网络类错误（超时/连接重置）才盲重试
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("API 返回无 choices: "
                               + json.dumps(data, ensure_ascii=False)[:300])
        msg = choices[0]["message"]
        finish_reason = choices[0].get("finish_reason")
        calls = []
        for c in msg.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_malformed": c["function"].get("arguments")}
            calls.append({"id": c["id"], "name": c["function"]["name"], "arguments": args})
        usage = data.get("usage") or {}
        raw = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            raw["tool_calls"] = msg["tool_calls"]
        return LLMReply(msg.get("content"), calls,
                        {"prompt_tokens": usage.get("prompt_tokens", 0),
                         "completion_tokens": usage.get("completion_tokens", 0),
                         # DeepSeek 前缀缓存命中统计（其他 OpenAI 兼容端点若无此字段则为 0）
                         "prompt_cache_hit_tokens": usage.get("prompt_cache_hit_tokens", 0),
                         "prompt_cache_miss_tokens": usage.get("prompt_cache_miss_tokens", 0)},
                        raw, finish_reason=finish_reason)


class MockLLM:
    """按 plan 回放工具调用；被当作 summarizer 调用（无 tools 参数）时返回截断式假摘要。"""

    def __init__(self):
        self.plan = []
        self.i = 0

    def bind(self, plan):
        self.plan = list(plan)
        self.i = 0

    def chat(self, messages, tools=None):
        fake_in = history_tokens(messages)
        if not tools:  # summarizer 调用
            # 注意：截断式假摘要会机制性地偏向保留约束——任务约束通常写在 prompt 开头、
            # 恰好落在前 400 字符里。因此 mock 的「存活率」数字不可作任何参考（README 已声明）。
            src = str(messages[-1].get("content") or "")
            content = "[MOCK-SUMMARY] " + src[:400]
            return LLMReply(content=content,
                            usage={"prompt_tokens": fake_in,
                                   "completion_tokens": estimate_tokens(content)})
        if self.i >= len(self.plan):
            call = {"name": "done", "arguments": {"summary": "mock finished"}}
        else:
            call = self.plan[self.i]
            self.i += 1
        call_id = "call_%d" % self.i
        raw = {"role": "assistant", "content": "",
               "tool_calls": [{"id": call_id, "type": "function",
                               "function": {"name": call["name"],
                                            "arguments": json.dumps(call["arguments"],
                                                                    ensure_ascii=False)}}]}
        reply_calls = [{"id": call_id, "name": call["name"], "arguments": call["arguments"]}]
        return LLMReply(None, reply_calls,
                        {"prompt_tokens": fake_in, "completion_tokens": 24}, raw)


def make_llm(backend, base_url=None, model=None):
    if backend == "mock":
        return MockLLM()
    if backend == "deepseek":
        return OpenAICompatLLM(base_url or "https://api.deepseek.com",
                               model or "deepseek-chat")
    if backend == "openai-compat":
        if not base_url or not model:
            raise RuntimeError("openai-compat 后端需要 --base-url 与 --model")
        return OpenAICompatLLM(base_url, model)
    raise RuntimeError("未知 backend: " + backend)
