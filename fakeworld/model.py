"""脚本化模型：确定性、无网络、按 step 索引。

真实模型换成任何 provider 都不影响 loop 与崩溃语义——被测对象是 runtime，
不是模型。脚本按 step 取 turn，因此"崩溃后从哪个 step 继续"完全可复现。
"""

from __future__ import annotations

from typing import Any

from harness.model import ModelTurn
from harness.tools import ToolCallRequest

from .tools import scripted_turns


class ScriptedModel:
    def __init__(self, scenario: str, *, steps: int = 8, lines: int = 60) -> None:
        self._script: list[dict[str, Any]] = scripted_turns(scenario, steps=steps, lines=lines)
        self.calls: int = 0

    def next_turn(self, *, step: int, view: Any = None) -> ModelTurn:
        self.calls += 1
        index = min(step, len(self._script) - 1)
        entry = self._script[index]
        return ModelTurn(
            text=str(entry.get("text", "")),
            tool_calls=[
                ToolCallRequest(
                    tool_call_id=call["tool_call_id"], tool=call["tool"], args=call.get("args", {})
                )
                for call in entry.get("tool_calls", [])
            ],
            usage={"prompt_tokens": 512 + 64 * index, "completion_tokens": 48},
        )
