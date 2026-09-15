"""模型接口与响应类型：loop 与 LLM client 共享，避免循环依赖。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel, Field

from .tools import ToolCallRequest


class Message(BaseModel):
    role: str
    content: str


class ModelTurn(BaseModel):
    text: str = ""
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)


class Model(Protocol):
    """模型接口；本项目的实现是确定性的脚本模型（fakeworld/model.py）。

    只依赖 ``step`` 与视图：真实模型的实现完全可以是 HTTP 调用，
    loop 与崩溃语义不受影响。
    """

    def next_turn(self, *, step: int, view: Any) -> ModelTurn: ...


def message_texts(messages: Sequence[Message]) -> tuple[str, ...]:
    return tuple(message.content for message in messages)
