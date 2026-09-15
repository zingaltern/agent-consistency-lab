"""loop 与 resume：in-process 的 happy path、重复 resume、未闭合调用恢复、去重重放。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fakeworld.model import ScriptedModel
from fakeworld.tools import SCENARIO_POOL_EXHAUSTION, build_registry
from fakeworld.world import World
from harness.chaos import Chaos
from harness.events import NewEvent, Source, TreeEventType
from harness.loop import Loop
from harness.state import RunStatus, reduce_events
from harness.store import SqliteCheckpointSaver, SqliteStore, ToolCallStore
from harness.tools import canonical_args_sha256, idempotency_key

from .conftest import RunCtx

TASK = "支付服务 P99 告警，请定位并处置。"


@pytest.fixture()
def world(tmp_path: Path) -> World:
    world = World(tmp_path / "world.db")
    yield world
    world.close()


def _loop(store: SqliteStore, *, dedup: bool = True) -> Loop:
    return Loop(store, SqliteCheckpointSaver(store), dedup=dedup, chaos=Chaos.disabled())


def _start(store: SqliteStore, world: World, ctx: RunCtx, **kwargs):
    tool_idem = kwargs.pop("tool_idem", True)
    return _loop(store, **kwargs).start(
        run_id=ctx.run_id,
        thread_id=ctx.thread_id,
        branch_id=ctx.branch_id,
        model=ScriptedModel(SCENARIO_POOL_EXHAUSTION),
        registry=build_registry(world, idempotent_impl=tool_idem),
        task=TASK,
    )


def _resume(store: SqliteStore, world: World, ctx: RunCtx, **kwargs):
    tool_idem = kwargs.pop("tool_idem", True)
    return _loop(store, **kwargs).resume(
        run_id=ctx.run_id,
        thread_id=ctx.thread_id,
        branch_id=ctx.branch_id,
        model=ScriptedModel(SCENARIO_POOL_EXHAUSTION),
        registry=build_registry(world, idempotent_impl=tool_idem),
    )


def test_happy_path_completes_with_exactly_one_effect(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    outcome = _start(store, world, ctx)
    assert outcome.status == RunStatus.COMPLETED.value
    assert outcome.step == 3
    assert outcome.executed == 2  # 读 + 写
    assert outcome.checkpoints == 3
    assert world.total_effects() == 1
    assert world.duplicate_keys() == {}

    state, violations = reduce_events(store.effective_events(ctx.branch_id))
    assert violations == []
    assert state.open_tool_calls == {}


def test_resume_after_completion_is_a_noop(store: SqliteStore, world: World, ctx: RunCtx) -> None:
    _start(store, world, ctx)
    events_before = len(store.effective_events(ctx.branch_id))
    outcome = _resume(store, world, ctx)
    assert outcome.status == RunStatus.COMPLETED.value
    assert len(store.effective_events(ctx.branch_id)) == events_before
    assert world.total_effects() == 1


def test_resume_finishes_an_open_tool_call(store: SqliteStore, world: World, ctx: RunCtx) -> None:
    """模拟"崩溃在工具执行前"：日志里有 tool_call 但没有 result。"""
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.USER_MESSAGE,
            source=Source.USER,
            payload={"text": TASK},
        )
    )
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.AGENT_MESSAGE,
            source=Source.AGENT,
            payload={"text": "先取指标", "final": False},
        )
    )
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.TOOL_CALL,
            source=Source.AGENT,
            payload={
                "tool_call_id": "tc_read_1",
                "tool": "query_metrics",
                "args": {"service": "payment"},
            },
        )
    )
    state, _ = reduce_events(store.effective_events(ctx.branch_id))
    assert list(state.open_tool_calls) == ["tc_read_1"]

    outcome = _resume(store, world, ctx)
    assert outcome.status == RunStatus.COMPLETED.value
    assert outcome.executed == 2  # 补跑读工具 + 后续写工具
    assert world.total_effects() == 1
    final_state, violations = reduce_events(store.effective_events(ctx.branch_id))
    assert violations == []
    assert final_state.open_tool_calls == {}


def test_dedup_replays_recorded_result_without_reexecuting(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """记录已存在但日志缺 result：去重路径必须复用结果，不得再次执行。"""
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.USER_MESSAGE,
            source=Source.USER,
            payload={"text": TASK},
        )
    )
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.AGENT_MESSAGE,
            source=Source.AGENT,
            payload={"text": "先取指标", "final": False},
        )
    )
    args = {"service": "payment"}
    stored_result = {"service": "payment", "p99_ms": 9999, "from": "dedup"}
    # 未闭合的调用：日志里有 tool_call（无 result），去重表里已有执行结果
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.TOOL_CALL,
            source=Source.AGENT,
            payload={"tool_call_id": "tc_read_1", "tool": "query_metrics", "args": args},
        )
    )
    ToolCallStore(store).record(
        tool_call_id="tc_read_1",
        run_id=ctx.run_id,
        branch_id=ctx.branch_id,
        tool="query_metrics",
        args=args,
        args_sha256=canonical_args_sha256(args),
        idempotency_key=idempotency_key(ctx.run_id, "tc_read_1"),
        effect="read",
        status="executed",
        result=stored_result,
    )

    outcome = _resume(store, world, ctx)
    assert outcome.replayed >= 1
    assert outcome.status == RunStatus.COMPLETED.value
    results = [
        e.payload
        for e in store.effective_events(ctx.branch_id)
        if e.type == TreeEventType.TOOL_RESULT.value
    ]
    assert results[0]["result"] == stored_result  # 复用既有结果而非重新执行
    assert world.total_effects() == 1  # 只有后面那次写工具产生了效果


def test_dedup_off_still_replays_from_event_log(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """去重表关闭时，日志仍然是权威：已完成调用不会重复执行。"""
    _start(store, world, ctx, dedup=True)
    outcome = _resume(store, world, ctx, dedup=False)
    assert outcome.executed == 0
    assert world.total_effects() == 1
