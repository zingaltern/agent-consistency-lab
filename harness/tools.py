"""工具层：效果声明、幂等键、参数规范化。

三条约束（对应评审对原设计的修正）：

1. **幂等键 = hash(run_id, tool_call_id)**，不含 step、不含参数。理由：
   step 在分叉/重试时会漂移；参数一旦被审批改写，语义上就是一次新调用
   （Stripe 口径：改参即换键），把参数放进键会让"改参"变成"重放"。
2. **参数 hash 只作旁路校验**（``args_sha256``）：用于检测"同键不同参"，
   即下游去重会掩盖参数变更的那类事故。
3. **效果类别由工具声明**，不从错误文本推断：
   ``read`` / ``write_idempotent`` / ``write_nonidempotent``。

``write_nonidempotent`` 的工具在崩溃窗口 2（效果已发生、记录未落盘）下必然产生
重复副作用——这正是 W2 要测出来的结论，也是 W3 outbox 的动机。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Effect(StrEnum):
    READ = "read"
    WRITE_IDEMPOTENT = "write_idempotent"
    WRITE_NONIDEMPOTENT = "write_nonidempotent"


class ToolCallRequest(BaseModel):
    """模型提出的一次工具调用请求。"""

    tool_call_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    tool_call_id: str
    status: str  # executed | failed | unknown
    result: dict[str, Any] | None = None
    error_class: str | None = None


ToolFn = Callable[[dict[str, Any], str], dict[str, Any]]
"""工具实现签名：(args, idempotency_key) -> result。

幂等实现应当用 ``idempotency_key`` 做 upsert；非幂等实现忽略它并直接追加效果。
"""


@dataclass(frozen=True)
class Tool:
    name: str
    effect: Effect
    fn: ToolFn
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


class ToolRegistry:
    def __init__(self, tools: Mapping[str, Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = dict(tools or {})

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)


def idempotency_key(run_id: str, tool_call_id: str) -> str:
    """确定性的幂等键；同一 run 内同一 tool_call_id 必须得到同一键。"""
    digest = hashlib.sha256(f"{run_id}\x1f{tool_call_id}".encode())
    return f"idem_{digest.hexdigest()[:32]}"


def canonical_json(payload: Any) -> str:
    """规范化 JSON：键排序、无多余空白、保留 Unicode、拒绝 NaN。

    规范化规则一旦改动，历史事件的 hash 全部失效，因此这里配 golden hash 测试。
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def canonical_args_sha256(args: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(args)).encode("utf-8")).hexdigest()
