"""派生状态：运行时从不持久化"状态对象"，只持久化事件日志。

DerivedState 是事件日志的折叠结果，可随时重建，因此不存在"状态与日志不一致"
这一类问题。所有不变量以 Violation 形式显式返回（而不是靠隐式断言），
调用方可以决定告警、修复还是中止。

不变量清单（docs/semantics.md 同步维护）：

* INV-001 同一 ``tool_call_id`` 至多一个 tool_call
* INV-002 tool_result 必须匹配一个未闭合的 tool_call
* INV-003 同一 tool_call 至多一个 tool_result
* INV-004 interrupt 不得在未 resume 时重复出现
* INV-005 resume 必须对应一个未闭合的 interrupt
* INV-006 终态（completed/failed）之后不得再出现树节点
* INV-007 resume 携带的 interrupt_index 必须与未闭合 interrupt 的 index 一致
  （index 与 id 双重核验：只对 id 不打分的实现会把 resume 值接到错误的 interrupt 上）
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel

from .events import ArtifactEventType, Event, EventKind, TreeEventType


class RunStatus(StrEnum):
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"


class Violation(BaseModel):
    code: str
    detail: str
    event_id: str | None = None


@dataclass(frozen=True)
class DerivedState:
    status: RunStatus
    step: int
    open_tool_calls: Mapping[str, str] = field(default_factory=dict)
    closed_tool_calls: tuple[str, ...] = ()
    pending_interrupt_id: str | None = None
    last_event_id: str | None = None
    last_seq: int = -1

    def fingerprint(self) -> str:
        """用于重放确定性断言：同一事件日志必须得到同一指纹。"""
        payload = {
            "status": self.status.value,
            "step": self.step,
            "open": sorted(self.open_tool_calls.items()),
            "closed": list(self.closed_tool_calls),
            "interrupt": self.pending_interrupt_id,
            "last_event_id": self.last_event_id,
            "last_seq": self.last_seq,
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def reduce_events(events: Sequence[Event]) -> tuple[DerivedState, list[Violation]]:
    """把有效日志折叠成派生状态，同时收集不变量违反。"""
    status = RunStatus.RUNNING
    step = 0
    open_calls: dict[str, str] = {}
    closed_calls: list[str] = []
    pending_interrupt: str | None = None
    pending_interrupt_index: int | None = None
    last_event_id: str | None = None
    last_seq = -1
    violations: list[Violation] = []
    terminal = False

    for event in events:
        last_event_id, last_seq = event.event_id, event.seq
        if event.kind is EventKind.ARTIFACT:
            if event.type == ArtifactEventType.ERROR.value and event.payload.get("fatal"):
                status = RunStatus.FAILED
                terminal = True
            continue
        if terminal:
            violations.append(
                Violation(
                    code="INV-006",
                    detail=f"tree node {event.type!r} appended after terminal state",
                    event_id=event.event_id,
                )
            )
        if event.type == TreeEventType.AGENT_MESSAGE.value:
            step += 1
            if event.payload.get("final"):
                status = RunStatus.COMPLETED
                terminal = True
        elif event.type == TreeEventType.TOOL_CALL.value:
            call_id = event.payload.get("tool_call_id")
            if not isinstance(call_id, str):
                violations.append(
                    Violation(
                        code="INV-001",
                        detail="tool_call without tool_call_id",
                        event_id=event.event_id,
                    )
                )
            elif call_id in open_calls or call_id in closed_calls:
                violations.append(
                    Violation(
                        code="INV-001",
                        detail=f"duplicate tool_call_id {call_id!r}",
                        event_id=event.event_id,
                    )
                )
            else:
                open_calls[call_id] = event.event_id
        elif event.type == TreeEventType.TOOL_RESULT.value:
            call_id = event.payload.get("tool_call_id")
            if call_id in closed_calls:
                violations.append(
                    Violation(
                        code="INV-003",
                        detail=f"tool_call {call_id!r} already has a result",
                        event_id=event.event_id,
                    )
                )
            elif call_id in open_calls:
                open_calls.pop(call_id)
                closed_calls.append(call_id)
            else:
                violations.append(
                    Violation(
                        code="INV-002",
                        detail=f"orphan tool_result for {call_id!r}",
                        event_id=event.event_id,
                    )
                )
        elif event.type == TreeEventType.INTERRUPT.value:
            interrupt_id = event.payload.get("interrupt_id")
            if pending_interrupt is not None:
                violations.append(
                    Violation(
                        code="INV-004",
                        detail=f"interrupt {interrupt_id!r} while {pending_interrupt!r} is open",
                        event_id=event.event_id,
                    )
                )
            else:
                pending_interrupt = (
                    interrupt_id if isinstance(interrupt_id, str) else event.event_id
                )
                raw_index = event.payload.get("interrupt_index")
                pending_interrupt_index = raw_index if isinstance(raw_index, int) else None
                status = RunStatus.WAITING_HUMAN
        elif event.type == TreeEventType.RESUME.value:
            if pending_interrupt is None:
                violations.append(
                    Violation(
                        code="INV-005",
                        detail="resume without open interrupt",
                        event_id=event.event_id,
                    )
                )
            else:
                resume_index = event.payload.get("interrupt_index")
                if (
                    pending_interrupt_index is not None
                    and isinstance(resume_index, int)
                    and resume_index != pending_interrupt_index
                ):
                    violations.append(
                        Violation(
                            code="INV-007",
                            detail=(
                                f"resume index {resume_index} does not match open "
                                f"interrupt index {pending_interrupt_index}"
                            ),
                            event_id=event.event_id,
                        )
                    )
                pending_interrupt = None
                pending_interrupt_index = None
                if status is RunStatus.WAITING_HUMAN:
                    status = RunStatus.RUNNING

    if status is RunStatus.COMPLETED and open_calls:
        violations.append(
            Violation(
                code="INV-006",
                detail=f"agent declared final with open tool calls: {sorted(open_calls)}",
            )
        )

    return (
        DerivedState(
            status=status,
            step=step,
            open_tool_calls=dict(open_calls),
            closed_tool_calls=tuple(closed_calls),
            pending_interrupt_id=pending_interrupt,
            last_event_id=last_event_id,
            last_seq=last_seq,
        ),
        violations,
    )
