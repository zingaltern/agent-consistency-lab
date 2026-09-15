"""上下文视图：把事件日志渲染成模型请求的纯函数。

三条不变式（对应评审从 OpenHands/供应商约束里挑出的真实坑）：

* **配对**：每个 tool_call 必须有对应的 tool_result；孤儿的任一侧都会让供应商 400。
* **批次原子**：同一次模型响应里提出的多个 tool_call 是**一个批次**，要么全留要么全删。
  只删一半会让工具调用序列在语义上不可解释。批次只在"下一个 turn 边界"
  （新的 agent_message / user_message / 日志结束）收束——``interrupt`` / ``resume``
  不打断批次，否则审批路径下"提议调用 → 人审批 → 执行 → 结果"会被撕成两半。
* **观测唯一**：同一个 tool_call 至多一个 tool_result。

违反不变式时**不抛出**，而是丢弃违规部分、把违反记进 ``View.violations``——
因为视图是恢复路径上重建出来的，宁可少给模型一点信息，也不能给出一个会被 400 拒绝的请求。

缓存纪律（本文件是它的落点）：视图分四段——

``prefix``（工具 + 系统提示，永不变化）→ ``history``（append-only 的对话与工具结果）
→ ``summary``（压缩摘要，追加在尾部，不修改历史）→ ``tail``（每步变化的动态信息）。

动态信息一律进 ``tail``；``dynamic_at_head=True`` 只是为了做"把动态信息塞进前缀"的
反面对照实验（每次请求都会击穿缓存）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from .artifacts import ArtifactStore
from .events import ArtifactEventType, Event, TreeEventType
from .state import Violation
from .tokens import estimate_message_tokens
from .tools import ToolRegistry, canonical_json

DEFAULT_MAX_INLINE_TOKENS = 800


class Block(BaseModel):
    role: str  # system | user | assistant | tool | summary | dynamic
    content: str
    section: str  # prefix | history | summary | tail
    event_ids: tuple[str, ...] = ()
    tokens: int = 0


class View(BaseModel):
    blocks: tuple[Block, ...] = ()
    sections: dict[str, int] = Field(default_factory=dict)
    dropped_event_ids: tuple[str, ...] = ()
    violations: tuple[Violation, ...] = ()
    offloaded: tuple[str, ...] = ()
    total_tokens: int = 0

    def block_texts(self) -> tuple[str, ...]:
        return tuple(f"{block.role}\x1f{block.content}" for block in self.blocks)

    def fingerprint(self) -> str:
        blob = "\x1e".join(self.block_texts())
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def render(self) -> str:
        return "\n\n".join(f"[{block.role}] {block.content}" for block in self.blocks)


class ViewBuilder:
    def __init__(
        self,
        *,
        system_prompt: str,
        registry: ToolRegistry,
        artifacts: ArtifactStore | None = None,
        max_inline_tokens: int = DEFAULT_MAX_INLINE_TOKENS,
        dynamic_at_head: bool = False,
    ) -> None:
        self._system_prompt = system_prompt
        self._registry = registry
        self._artifacts = artifacts
        self._max_inline = max_inline_tokens
        self._dynamic_at_head = dynamic_at_head

    # ------------------------------------------------------------------ build

    def build(self, *, events: Sequence[Event], dynamic: dict[str, Any] | None = None) -> View:
        blocks: list[Block] = []
        violations: list[Violation] = []
        dropped: list[str] = []
        offloaded: list[str] = []

        blocks.append(self._prefix_block(dynamic if self._dynamic_at_head else None))

        compactions = [
            event for event in events if event.type == ArtifactEventType.COMPACTION.value
        ]
        replaced: set[str] = set()
        for event in compactions:
            replaced.update(event.payload.get("replaces_event_ids", []))

        kept = [
            event
            for event in events
            if event.is_tree_node
            and event.event_id not in replaced
            and event.type
            in (
                TreeEventType.USER_MESSAGE.value,
                TreeEventType.AGENT_MESSAGE.value,
                TreeEventType.TOOL_CALL.value,
                TreeEventType.TOOL_RESULT.value,
                TreeEventType.INTERRUPT.value,
                TreeEventType.RESUME.value,
            )
        ]

        history, history_violations, history_dropped = self._render_history(
            kept, offloaded=offloaded
        )
        blocks.extend(history)
        violations.extend(history_violations)
        dropped.extend(history_dropped)

        for event in compactions:
            summary = str(event.payload.get("summary", ""))
            ref = event.payload.get("artifact_ref") or {}
            blocks.append(
                Block(
                    role="summary",
                    section="summary",
                    content=(
                        f"（上下文压缩摘要 {event.payload.get('compaction_id', '')}）\n{summary}"
                        + (f"\n完整内容见 artifact {ref.get('digest')}" if ref else "")
                    ),
                    event_ids=(event.event_id,),
                    tokens=estimate_message_tokens(summary),
                )
            )

        if not self._dynamic_at_head and dynamic:
            blocks.append(self._dynamic_block(dynamic))

        for block in blocks:
            if block.tokens == 0:
                block.tokens = estimate_message_tokens(block.content)

        sections: dict[str, int] = {}
        for block in blocks:
            sections[block.section] = sections.get(block.section, 0) + block.tokens

        return View(
            blocks=tuple(blocks),
            sections=sections,
            dropped_event_ids=tuple(dropped),
            violations=tuple(violations),
            offloaded=tuple(offloaded),
            total_tokens=sum(sections.values()),
        )

    # ----------------------------------------------------------------- blocks

    def _prefix_block(self, dynamic: dict[str, Any] | None) -> Block:
        tool_lines = [
            f"- {tool.name}({', '.join(sorted(tool.tags)) or 'no-tags'}): {tool.description}"
            for tool in self._registry
        ]
        content = f"{self._system_prompt}\n\n可用工具：\n" + "\n".join(tool_lines)
        if dynamic:
            # 反面对照：动态内容进了前缀 ⇒ 每步都击穿缓存
            content += "\n\n运行时信息：\n" + canonical_json(dynamic)
        return Block(role="system", section="prefix", content=content)

    @staticmethod
    def _dynamic_block(dynamic: dict[str, Any]) -> Block:
        return Block(
            role="dynamic",
            section="tail",
            content="运行时信息（每步可能变化）：\n" + canonical_json(dynamic),
        )

    def _render_history(
        self, events: Sequence[Event], *, offloaded: list[str]
    ) -> tuple[list[Block], list[Violation], list[str]]:
        blocks: list[Block] = []
        violations: list[Violation] = []
        dropped: list[str] = []

        batch: list[Event] = []
        batch_results: dict[str, Event] = {}

        def flush() -> None:
            if not batch:
                return
            calls = [e for e in batch if e.type == TreeEventType.TOOL_CALL.value]
            results = batch_results
            call_ids = [str(e.payload.get("tool_call_id")) for e in calls]
            missing = [cid for cid in call_ids if cid not in results]
            if missing:
                # 批次原子：一个调用缺结果 ⇒ 整批丢弃（只删一半会得到不可解释的序列）
                violations.append(
                    Violation(
                        code="VIEW-BATCH-ATOMICITY",
                        detail=f"batch dropped: tool calls without results {missing}",
                        event_id=batch[0].event_id,
                    )
                )
                dropped.extend(e.event_id for e in batch)
                dropped.extend(results[cid].event_id for cid in call_ids if cid in results)
                batch.clear()
                batch_results.clear()
                return
            if calls:
                lines = [
                    "tool_call {}({})".format(
                        e.payload.get("tool"), canonical_json(e.payload.get("args", {}))
                    )
                    for e in calls
                ]
                blocks.append(
                    Block(
                        role="assistant",
                        section="history",
                        content="\n".join(lines),
                        event_ids=tuple(e.event_id for e in calls),
                    )
                )
            for cid in call_ids:
                event = results[cid]
                content, digest = self._render_tool_result(event)
                if digest:
                    offloaded.append(digest)
                blocks.append(
                    Block(
                        role="tool",
                        section="history",
                        content=content,
                        event_ids=(event.event_id,),
                    )
                )
            batch.clear()
            batch_results.clear()

        for event in events:
            if event.type == TreeEventType.AGENT_MESSAGE.value:
                flush()
                text = str(event.payload.get("text", ""))
                blocks.append(
                    Block(
                        role="assistant",
                        section="history",
                        content=text,
                        event_ids=(event.event_id,),
                    )
                )
            elif event.type == TreeEventType.TOOL_CALL.value:
                batch.append(event)
            elif event.type == TreeEventType.TOOL_RESULT.value:
                call_id = str(event.payload.get("tool_call_id"))
                if call_id in batch_results:
                    violations.append(
                        Violation(
                            code="VIEW-OBSERVATION-UNIQUENESS",
                            detail=f"duplicate tool_result for {call_id}",
                            event_id=event.event_id,
                        )
                    )
                    dropped.append(event.event_id)
                    continue
                if not any(
                    str(e.payload.get("tool_call_id")) == call_id
                    for e in batch
                    if e.type == TreeEventType.TOOL_CALL.value
                ):
                    violations.append(
                        Violation(
                            code="VIEW-PAIRING",
                            detail=f"orphan tool_result for {call_id}",
                            event_id=event.event_id,
                        )
                    )
                    dropped.append(event.event_id)
                    continue
                batch_results[call_id] = event
            elif event.type == TreeEventType.USER_MESSAGE.value:
                flush()
                blocks.append(
                    Block(
                        role="user",
                        section="history",
                        content=str(event.payload.get("text", "")),
                        event_ids=(event.event_id,),
                    )
                )
            elif event.type == TreeEventType.INTERRUPT.value:
                # 刻意不 flush：事件顺序天然是 tool_call → interrupt → resume → tool_result，
                # 若在 interrupt 处收束批次，这次调用会被判为"缺结果"整批丢弃，
                # 随后的结果又变成孤儿——等于每次人工审批后，模型都看不到这次写操作的
                # 调用与返回（这正是评审实测到的 view_violations=2）。
                blocks.append(
                    Block(
                        role="user",
                        section="history",
                        content=f"[interrupt] {event.payload.get('reason', '')}",
                        event_ids=(event.event_id,),
                    )
                )
            elif event.type == TreeEventType.RESUME.value:
                blocks.append(
                    Block(
                        role="user",
                        section="history",
                        content=f"[resume] decision={event.payload.get('decision', '')}",
                        event_ids=(event.event_id,),
                    )
                )
        flush()
        return blocks, violations, dropped

    def _render_tool_result(self, event: Event) -> tuple[str, str | None]:
        payload = event.payload
        status = payload.get("status")
        if status != "executed":
            return f"tool_result({status}) error={payload.get('error_class') or '-'}", None
        result = payload.get("result")
        text = canonical_json(result)
        tokens = estimate_message_tokens(text)
        if self._artifacts is None or tokens <= self._max_inline:
            return f"tool_result {text}", None
        ref = self._artifacts.put(text)
        rendered = canonical_json(
            {
                "offloaded": True,
                "digest": ref.digest,
                "bytes": ref.bytes,
                "tokens": ref.tokens,
                "preview": ref.preview,
                "note": "结果过大已卸载；用 read_artifact(digest, offset, limit) 读取",
            }
        )
        return f"tool_result {rendered}", ref.digest
