"""事件模型：扁平 append-only 日志的最小语义单元。

本模块固化三条设计约束，后续所有模块都必须遵守：

1. 存储层是扁平的 ``(branch_id, seq)`` 序列。``parent_id`` 只是对话 lineage 的
   可选标注，恢复与重放不得依赖递归查询（OpenHands/codex 均为扁平存储 +
   派生 lineage；带 parent_id 的树在主模型上已被验证会引入活跃叶维护与
   控制事件混入两类事故）。
2. 事件按 ``kind`` 显式分为 ``TREE_NODE`` 与 ``ARTIFACT``。只有 TREE_NODE 进入
   对话树与上下文视图；预算、租约、压缩、错误、审批等控制事实一律为
   ARTIFACT，禁止混入树。
3. ``seq`` 在分支内连续递增、不可变。任何历史改写都被存储层的触发器拒绝
   （见 store/schema.py）。

payload 的规范形状见 docs/semantics.md「事件目录」一节。
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ids import new_id


class EventKind(StrEnum):
    TREE_NODE = "tree_node"
    ARTIFACT = "artifact"


class Source(StrEnum):
    USER = "user"
    AGENT = "agent"
    TOOL = "tool"
    SYSTEM = "system"


class TreeEventType(StrEnum):
    USER_MESSAGE = "user_message"
    AGENT_MESSAGE = "agent_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    INTERRUPT = "interrupt"
    RESUME = "resume"


class ArtifactEventType(StrEnum):
    ERROR = "error"
    BUDGET_UPDATE = "budget_update"
    LEASE = "lease"
    COMPACTION = "compaction"
    APPROVAL = "approval"
    STATE_UPDATE = "state_update"


_KINDS: dict[EventKind, frozenset[str]] = {
    EventKind.TREE_NODE: frozenset(t.value for t in TreeEventType),
    EventKind.ARTIFACT: frozenset(t.value for t in ArtifactEventType),
}


class NewEvent(BaseModel):
    """待落盘事件：seq 与 created_at 由存储层分配。"""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=lambda: new_id("evt"))
    run_id: str
    branch_id: str
    kind: EventKind
    type: str
    source: Source
    payload: dict[str, Any] = Field(default_factory=dict)
    parent_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None

    @model_validator(mode="after")
    def _type_matches_kind(self) -> NewEvent:
        allowed = _KINDS[self.kind]
        if self.type not in allowed:
            raise ValueError(
                f"event type {self.type!r} is not a valid {self.kind.value}; "
                f"allowed: {sorted(allowed)}"
            )
        return self

    @classmethod
    def tree(
        cls,
        *,
        run_id: str,
        branch_id: str,
        type: TreeEventType,
        source: Source,
        payload: dict[str, Any] | None = None,
        **rest: Any,
    ) -> NewEvent:
        return cls(
            run_id=run_id,
            branch_id=branch_id,
            kind=EventKind.TREE_NODE,
            type=type.value,
            source=source,
            payload=payload or {},
            **rest,
        )

    @classmethod
    def artifact(
        cls,
        *,
        run_id: str,
        branch_id: str,
        type: ArtifactEventType,
        source: Source = Source.SYSTEM,
        payload: dict[str, Any] | None = None,
        **rest: Any,
    ) -> NewEvent:
        return cls(
            run_id=run_id,
            branch_id=branch_id,
            kind=EventKind.ARTIFACT,
            type=type.value,
            source=source,
            payload=payload or {},
            **rest,
        )


class Event(NewEvent):
    """已落盘事件：多出存储层分配的 seq 与 created_at。"""

    seq: int = Field(ge=0)
    created_at: float = Field(default_factory=time.time)

    @property
    def is_tree_node(self) -> bool:
        return self.kind is EventKind.TREE_NODE
