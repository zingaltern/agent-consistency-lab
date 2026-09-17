"""真实模型的 transport：OpenAI 兼容的 ``/chat/completions``（R-B1）。

三条纪律：

1. **只用标准库**：``urllib`` 发 HTTP，不引入新依赖（内核依赖仍然只有 pydantic）。
   要接别的供应商就换 ``base_url`` 和字段映射（映射统一在 `harness/cassette.py`
   的 ``normalize_usage`` 里，不允许在这里"顺手转"）。
2. **key 只从环境变量读**：绝不写进仓库文件、报告或事件 payload；缺失时给**可读失败**
   （说清设哪个环境变量），而不是抛一个裸 KeyError。
3. **永远不是默认路径**：只有显式 ``--model record --transport live`` 才会走到这里；
   CI 不执行任何在线调用（``tests/test_live_model.py`` 里的在线用例带 ``-m live``）。

已知限制（写在 docstring 而不是报告里，避免它被误当成能力）：
本 transport 把视图块拼成**一条 user 消息**，工具清单以 ``tools`` 参数下发，
参数 schema 由 ``Tool.parameters`` 声明（未声明的工具下发空参数表——实测空表下
真实模型倾向于用文字描述"我打算调用哪个工具"而**不真正调用**，因此声明 schema
是 live 路径可用性的前提）。即便如此，"真实模型自主选择工具"仍取决于供应商对
工具调用的支持程度；接真实模型的用途是**接口回归、录制与语义验证**，不是模型质量评测。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .llm import RawCompletion
from .tools import ToolCallRequest, tool_param_schema


class LiveTransportError(RuntimeError):
    """在线调用的失败：一律带**可读**上下文（哪个 URL、哪个环境变量、供应商说了什么）。"""


class MissingAPIKeyError(LiveTransportError):
    pass


def api_key_from_env(var_name: str) -> str:
    """从环境变量读 key；缺失即给出可读失败（不要用裸 KeyError）。"""
    value = os.environ.get(var_name, "").strip()
    if not value:
        raise MissingAPIKeyError(
            f"环境变量 {var_name} 为空：live 模式需要真实 API key。"
            f"用法示例：export {var_name}=sk-... && python -m experiments.worker "
            "--model record --transport live --record-dir /tmp/cassette --budget-usd 0.05"
        )
    return value


class LiveChatTransport:
    """OpenAI 兼容的 chat completions。"""

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        key_env: str = "OPENAI_API_KEY",
        temperature: float = 0.0,
        timeout: float = 60.0,
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.key_env = key_env
        self.temperature = temperature
        self.timeout = timeout
        self.tools = tools or []
        self.system_prompt = system_prompt
        self.calls = 0

    # ------------------------------------------------------------------ 内部

    def _payload(self, blocks: tuple[str, ...]) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": "\n\n".join(blocks)})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if self.tools:
            payload["tools"] = self.tools
        return payload

    def _post(self, payload: dict[str, Any], key: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:400]
            raise LiveTransportError(
                f"供应商返回 HTTP {exc.code}（{self.base_url}/chat/completions）：{body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LiveTransportError(
                f"无法连接 {self.base_url}/chat/completions：{exc.reason}"
            ) from exc

    # ------------------------------------------------------------------ public

    def complete(self, *, blocks: tuple[str, ...], step: int) -> RawCompletion:
        key = api_key_from_env(self.key_env)
        self.calls += 1
        data = self._post(self._payload(blocks), key)
        choices = data.get("choices") or []
        if not choices:
            raise LiveTransportError(f"供应商返回里没有 choices：{json.dumps(data)[:300]}")
        message = choices[0].get("message") or {}
        tool_calls: list[ToolCallRequest] = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            raw_args = function.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError as exc:
                raise LiveTransportError(
                    f"工具参数不是合法 JSON（tool_call_id={call.get('id')!r}）：{raw_args[:200]}"
                ) from exc
            tool_calls.append(
                ToolCallRequest(
                    tool_call_id=str(call.get("id") or f"live-{step}-{len(tool_calls)}"),
                    tool=str(function.get("name", "")),
                    args=args,
                )
            )
        return RawCompletion(
            text=str(message.get("content") or ""),
            tool_calls=tool_calls,
            usage_raw=dict(data.get("usage") or {}),
        )


def tools_schema_from_registry(registry: Any) -> list[dict[str, Any]]:
    """把工具注册表转成 OpenAI 的 ``tools`` 形态（名字 + 描述 + 参数 schema）。

    参数 schema 取 ``Tool.parameters``；未声明时回退到空参数表（向后兼容：
    既有工具不声明 schema 时，下发的形状与从前逐字相同）。schema 是"工具自述"的
    一部分——它必须与 ``fn`` 实际接受的字段一致，否则真实模型的合法调用会被判成参数错误。
    回退形状只有一处定义（``harness/tools.py::tool_param_schema``），MCP 侧同源。
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or tool.name,
                "parameters": tool_param_schema(tool),
            },
        }
        for tool in registry
    ]
