"""最小 agent loop：被测对象。

每一轮 = 一个 super-step：构建视图 → 模型响应 → 执行工具 → 提交边界 checkpoint。
三个命名崩溃窗口嵌在工具执行路径上（见 harness/chaos.py）。

恢复语义（与 docs/semantics.md 保持一致）：

* 权威是事件日志：恢复时先折叠日志，凡日志里已有结论的调用一律**重放不重跑**。
* 日志里没有结论的未闭合调用按 at-least-once 重新执行；若工具是
  ``write_nonidempotent`` 且崩溃发生在"效果已发生、记录未落盘"窗口，
  重复副作用**必然发生**——这是 W2 要用数据证明的结论，W3 用 outbox + unknown
  对账收敛它。
* checkpoint 只在 super-step 边界提交；writes 先于 checkpoint 落盘，
  因此崩溃后可能留下"孤儿 writes"（指向未提交的 checkpoint），恢复时被忽略。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field

from .chaos import Chaos
from .events import Event, NewEvent, Source, TreeEventType
from .ids import new_id
from .state import DerivedState, RunStatus, reduce_events
from .store.checkpoints import SqliteCheckpointSaver, Write
from .store.sqlite_store import SqliteStore
from .store.tool_calls import ToolCallStore
from .tools import (
    Effect,
    ToolCallRequest,
    ToolRegistry,
    canonical_args_sha256,
    canonical_json,
    idempotency_key,
)


class Message(BaseModel):
    role: str
    content: str


class ModelTurn(BaseModel):
    text: str = ""
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)


class Model(Protocol):
    """模型接口；本项目的实现是确定性的脚本模型（fakeworld/model.py）。"""

    def next_turn(self, *, step: int, view: Sequence[Message]) -> ModelTurn: ...


class RunOutcome(BaseModel):
    status: str
    step: int
    executed: int
    replayed: int
    checkpoints: int


@dataclass
class _Ctx:
    run_id: str
    thread_id: str
    branch_id: str


@dataclass
class _Counters:
    executed: int = 0
    replayed: int = 0
    checkpoints: int = 0


class Loop:
    def __init__(
        self,
        store: SqliteStore,
        saver: SqliteCheckpointSaver,
        *,
        dedup: bool = True,
        chaos: Chaos | None = None,
        max_steps: int = 12,
        checkpoint_ns: str = "",
    ) -> None:
        self._store = store
        self._saver = saver
        self._tool_calls = ToolCallStore(store)
        self._dedup = dedup
        self._chaos = chaos or Chaos.disabled()
        self._max_steps = max_steps
        self._ns = checkpoint_ns

    # ------------------------------------------------------------------ public

    def start(
        self,
        *,
        run_id: str,
        thread_id: str,
        branch_id: str,
        model: Model,
        registry: ToolRegistry,
        task: str,
    ) -> RunOutcome:
        ctx = _Ctx(run_id, thread_id, branch_id)
        self._store.append(
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.USER_MESSAGE,
                source=Source.USER,
                payload={"text": task},
            )
        )
        return self._drive(ctx, model, registry, _Counters())

    def resume(
        self,
        *,
        run_id: str,
        thread_id: str,
        branch_id: str,
        model: Model,
        registry: ToolRegistry,
    ) -> RunOutcome:
        ctx = _Ctx(run_id, thread_id, branch_id)
        counters = _Counters()
        state = self._state(ctx)
        for call_id, _ in sorted(state.open_tool_calls.items()):
            request = self._tool_request_from_log(ctx, call_id)
            if request is not None:
                self._execute_tool(ctx, request, registry, counters)
        self._chaos.hit("after_resume")
        return self._drive(ctx, model, registry, counters)

    # ------------------------------------------------------------------- drive

    def _drive(
        self, ctx: _Ctx, model: Model, registry: ToolRegistry, counters: _Counters
    ) -> RunOutcome:
        for _ in range(self._max_steps):
            state = self._state(ctx)
            if state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                break
            view = self._build_view(ctx)
            turn = model.next_turn(step=state.step, view=view)
            self._append_agent_message(ctx, turn, final=not turn.tool_calls)
            task_id = f"model:{state.step}"
            writes = [
                Write(
                    task_id=task_id,
                    idx=0,
                    channel="messages",
                    payload={
                        "text": turn.text,
                        "tool_calls": [c.tool_call_id for c in turn.tool_calls],
                    },
                )
            ]
            if not turn.tool_calls:
                self._commit(ctx, task_id, writes, step=state.step, counters=counters)
                break
            for request in turn.tool_calls:
                self._execute_tool(ctx, request, registry, counters)
                writes.append(
                    Write(
                        task_id=f"tool:{request.tool_call_id}",
                        idx=0,
                        channel="tool_output",
                        payload={"tool": request.tool, "tool_call_id": request.tool_call_id},
                    )
                )
            self._commit(ctx, task_id, writes, step=state.step, counters=counters)
        final = self._state(ctx)
        return RunOutcome(
            status=final.status.value,
            step=final.step,
            executed=counters.executed,
            replayed=counters.replayed,
            checkpoints=counters.checkpoints,
        )

    def _execute_tool(
        self,
        ctx: _Ctx,
        request: ToolCallRequest,
        registry: ToolRegistry,
        counters: _Counters,
    ) -> None:
        tool = registry.get(request.tool)
        key = idempotency_key(ctx.run_id, request.tool_call_id)
        args_sha256 = canonical_args_sha256(request.args)

        # 1) 日志权威：已有结论的调用只重放，不再产生副作用
        if self._has_result(ctx, request.tool_call_id):
            counters.replayed += 1
            return

        self._ensure_tool_call_event(ctx, request, tool.effect, key, args_sha256)

        # 2) 运行时去重表：同一键已执行过则复用结果
        if self._dedup:
            existing = self._tool_calls.lookup(key)
            if existing is not None and existing.status == "executed":
                counters.replayed += 1
                self._append_tool_result(ctx, request.tool_call_id, "executed", existing.result)
                return

        self._chaos.hit("pre_tool_exec")
        result = tool.fn(request.args, key)
        self._chaos.hit("post_tool_effect_pre_record")

        # 顺序有意为之：权威记录（事件日志）先落，去重行后落。
        # 于是"行已写、事件未写"这个危险微窗口不存在；反向残留（事件在、行缺失）
        # 由日志权威兜住，最坏只是审计少一行。
        self._append_tool_result(ctx, request.tool_call_id, "executed", result)
        self._tool_calls.record(
            tool_call_id=request.tool_call_id,
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            tool=request.tool,
            args=request.args,
            args_sha256=args_sha256,
            idempotency_key=key,
            effect=tool.effect.value,
            status="executed",
            result=result,
        )
        counters.executed += 1
        self._chaos.hit("post_record_pre_commit")

    def _commit(
        self,
        ctx: _Ctx,
        task_id: str,
        writes: list[Write],
        *,
        step: int,
        counters: _Counters,
    ) -> None:
        """writes 先落盘、checkpoint 后提交：崩溃可能留下孤儿 writes（被恢复忽略）。"""
        checkpoint_id = new_id("ckpt")
        self._saver.put_writes(
            thread_id=ctx.thread_id,
            checkpoint_id=checkpoint_id,
            writes=writes,
            checkpoint_ns=self._ns,
        )
        latest = self._saver.get_latest(ctx.thread_id, self._ns)
        state = self._state(ctx)
        self._saver.put(
            thread_id=ctx.thread_id,
            branch_id=ctx.branch_id,
            channel_values={
                "step": state.step,
                "status": state.status.value,
                "last_event_id": state.last_event_id,
            },
            source="loop",
            parent_checkpoint_id=latest.checkpoint.id if latest else None,
            checkpoint_id=checkpoint_id,
            checkpoint_ns=self._ns,
            step=step,
            metadata={"task_id": task_id, "last_seq": state.last_seq},
        )
        counters.checkpoints += 1

    # ------------------------------------------------------------------ helpers

    def _state(self, ctx: _Ctx) -> DerivedState:
        state, _ = reduce_events(self._log(ctx))
        return state

    def _log(self, ctx: _Ctx) -> list[Event]:
        return self._store.effective_events(ctx.branch_id)

    def _has_result(self, ctx: _Ctx, tool_call_id: str) -> bool:
        return tool_call_id in self._state(ctx).closed_tool_calls

    def _ensure_tool_call_event(
        self,
        ctx: _Ctx,
        request: ToolCallRequest,
        effect: Effect,
        key: str,
        args_sha256: str,
    ) -> None:
        state = self._state(ctx)
        if (
            request.tool_call_id in state.open_tool_calls
            or request.tool_call_id in state.closed_tool_calls
        ):
            return
        self._store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.TOOL_CALL,
                source=Source.AGENT,
                payload={
                    "tool_call_id": request.tool_call_id,
                    "tool": request.tool,
                    "args": request.args,
                    "args_sha256": args_sha256,
                    "idempotency_key": key,
                    "effect": effect.value,
                },
            )
        )

    def _append_tool_result(
        self, ctx: _Ctx, tool_call_id: str, status: str, result: dict[str, Any] | None
    ) -> None:
        self._store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.TOOL_RESULT,
                source=Source.TOOL,
                payload={"tool_call_id": tool_call_id, "status": status, "result": result},
            )
        )

    def _append_agent_message(self, ctx: _Ctx, turn: ModelTurn, *, final: bool) -> None:
        self._store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.AGENT_MESSAGE,
                source=Source.AGENT,
                payload={"text": turn.text, "final": final, "usage": turn.usage},
            )
        )

    def _tool_request_from_log(self, ctx: _Ctx, tool_call_id: str) -> ToolCallRequest | None:
        for event in self._log(ctx):
            if (
                event.type == TreeEventType.TOOL_CALL.value
                and event.payload.get("tool_call_id") == tool_call_id
            ):
                return ToolCallRequest(
                    tool_call_id=tool_call_id,
                    tool=event.payload["tool"],
                    args=event.payload.get("args", {}),
                )
        return None

    def _build_view(self, ctx: _Ctx) -> list[Message]:
        """W2 的最简视图：把树事件摊平成文本。W4 会替换为真正的上下文工程。"""
        lines: list[str] = []
        for event in self._log(ctx):
            if not event.is_tree_node:
                continue
            if event.type == TreeEventType.USER_MESSAGE.value:
                lines.append(f"user: {event.payload.get('text', '')}")
            elif event.type == TreeEventType.AGENT_MESSAGE.value:
                lines.append(f"agent: {event.payload.get('text', '')}")
            elif event.type == TreeEventType.TOOL_CALL.value:
                args_json = canonical_json(event.payload.get("args", {}))
                lines.append(f"tool_call {event.payload.get('tool')}: {args_json}")
            elif event.type == TreeEventType.TOOL_RESULT.value:
                lines.append(f"tool_result: {canonical_json(event.payload.get('result'))}")
        return [Message(role="transcript", content="\n".join(lines))]
