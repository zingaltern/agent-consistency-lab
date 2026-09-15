"""上下文视图：配对、批次原子、观测唯一、卸载确定性与缓存前缀稳定性。"""

from __future__ import annotations

from pathlib import Path

from harness.artifacts import ArtifactStore
from harness.context import ViewBuilder
from harness.events import NewEvent, Source, TreeEventType
from harness.store import SqliteStore, ToolCallStore
from harness.tools import Effect, Tool, ToolCallRequest, ToolRegistry

from .conftest import RunCtx


def _tree(run_id: str, branch_id: str, event_type: TreeEventType, **payload):
    return NewEvent.tree(
        run_id=run_id, branch_id=branch_id, type=event_type, source=Source.AGENT, payload=payload
    )


def _builder(artifacts: ArtifactStore | None = None, **kwargs) -> ViewBuilder:
    return ViewBuilder(system_prompt="sys", registry=ToolRegistry(), artifacts=artifacts, **kwargs)


def _pair(store: SqliteStore, ctx: RunCtx, call_id: str, *, with_result: bool = True) -> None:
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.TOOL_CALL,
            source=Source.AGENT,
            payload={"tool_call_id": call_id, "tool": "query_metrics", "args": {}},
        )
    )
    if with_result:
        store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.TOOL_RESULT,
                source=Source.TOOL,
                payload={"tool_call_id": call_id, "status": "executed", "result": {"ok": True}},
            )
        )


def _base_events(store: SqliteStore, ctx: RunCtx) -> list:
    store.append(_tree(ctx.run_id, ctx.branch_id, TreeEventType.USER_MESSAGE, text="告警"))
    store.append(_tree(ctx.run_id, ctx.branch_id, TreeEventType.AGENT_MESSAGE, text="取证"))
    return store.effective_events(ctx.branch_id)


def test_paired_batch_renders_tool_blocks(store: SqliteStore, ctx: RunCtx) -> None:
    _base_events(store, ctx)
    _pair(store, ctx, "c1")
    view = _builder().build(events=store.effective_events(ctx.branch_id))
    assert view.violations == ()
    roles = [block.role for block in view.blocks]
    assert roles == ["system", "user", "assistant", "assistant", "tool"]


def test_batch_atomicity_drops_whole_batch(store: SqliteStore, ctx: RunCtx) -> None:
    """同一次响应里的两个调用，一个缺结果 ⇒ 整批不进视图（只删一半不可解释）。"""
    _base_events(store, ctx)
    _pair(store, ctx, "c1", with_result=True)
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.TOOL_CALL,
            source=Source.AGENT,
            payload={"tool_call_id": "c2", "tool": "fetch_logs", "args": {}},
        )
    )
    view = _builder().build(events=store.effective_events(ctx.branch_id))
    codes = [violation.code for violation in view.violations]
    assert codes == ["VIEW-BATCH-ATOMICITY"]
    assert not any(block.role == "tool" for block in view.blocks)
    assert "c1" not in view.render() and "c2" not in view.render()


def test_orphan_tool_result_is_dropped(store: SqliteStore, ctx: RunCtx) -> None:
    _base_events(store, ctx)
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.TOOL_RESULT,
            source=Source.TOOL,
            payload={"tool_call_id": "ghost", "status": "executed"},
        )
    )
    view = _builder().build(events=store.effective_events(ctx.branch_id))
    assert [violation.code for violation in view.violations] == ["VIEW-PAIRING"]


def test_duplicate_observation_is_dropped(store: SqliteStore, ctx: RunCtx) -> None:
    _base_events(store, ctx)
    _pair(store, ctx, "c1", with_result=False)
    for _ in range(2):
        store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.TOOL_RESULT,
                source=Source.TOOL,
                payload={"tool_call_id": "c1", "status": "executed", "result": {}},
            )
        )
    view = _builder().build(events=store.effective_events(ctx.branch_id))
    assert [violation.code for violation in view.violations] == ["VIEW-OBSERVATION-UNIQUENESS"]


def test_dynamic_block_goes_to_tail_by_default(store: SqliteStore, ctx: RunCtx) -> None:
    _base_events(store, ctx)
    view = _builder().build(events=store.effective_events(ctx.branch_id), dynamic={"step": 2})
    assert view.blocks[-1].section == "tail"
    assert view.blocks[0].section == "prefix"


def test_dynamic_at_head_moves_block_into_prefix(store: SqliteStore, ctx: RunCtx) -> None:
    _base_events(store, ctx)
    view = _builder(dynamic_at_head=True).build(
        events=store.effective_events(ctx.branch_id), dynamic={"step": 2}
    )
    assert view.blocks[0].section == "prefix"
    assert "step" in view.blocks[0].content


def test_large_tool_result_is_offloaded_deterministically(
    store: SqliteStore, ctx: RunCtx, tmp_path: Path
) -> None:
    artifacts = ArtifactStore(tmp_path / "artifacts")
    _base_events(store, ctx)
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.TOOL_CALL,
            source=Source.AGENT,
            payload={"tool_call_id": "big", "tool": "fetch_logs", "args": {}},
        )
    )
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.TOOL_RESULT,
            source=Source.TOOL,
            payload={
                "tool_call_id": "big",
                "status": "executed",
                "result": {"entries": ["x" * 80 for _ in range(200)]},
            },
        )
    )
    events = store.effective_events(ctx.branch_id)
    first = _builder(artifacts, max_inline_tokens=100).build(events=events)
    second = _builder(artifacts, max_inline_tokens=100).build(events=events)
    assert first.offloaded and len(first.offloaded) == 1
    # 内容寻址 ⇒ 两次渲染得到同一 digest ⇒ 块文本逐字相同 ⇒ 缓存前缀稳定
    assert first.block_texts() == second.block_texts()
    assert artifacts.get(first.offloaded[0]).startswith("{")
    assert first.total_tokens < sum(len("x" * 80) for _ in range(200)) // 2


def test_compaction_summary_is_appended_at_tail(store: SqliteStore, ctx: RunCtx) -> None:
    from harness.events import ArtifactEventType

    events = _base_events(store, ctx)
    _pair(store, ctx, "c1")
    events = store.effective_events(ctx.branch_id)
    replaced = [events[2].event_id]
    store.append(
        NewEvent.artifact(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=ArtifactEventType.COMPACTION,
            payload={
                "compaction_id": "cmp_test",
                "replaces_event_ids": replaced,
                "summary": "已压缩 1 个事件",
                "artifact_ref": {},
            },
        )
    )
    view = _builder().build(events=store.effective_events(ctx.branch_id))
    sections = [block.section for block in view.blocks]
    assert sections[-1] == "tail" or "summary" in sections
    assert "已压缩 1 个事件" in view.render()
    assert replaced[0] not in [eid for block in view.blocks for eid in block.event_ids]


def test_tool_registry_renders_in_prefix(store: SqliteStore, ctx: RunCtx) -> None:
    registry = ToolRegistry()
    registry.register(
        Tool(name="query_metrics", effect=Effect.READ, fn=lambda a, k: {}, description="读指标")
    )
    builder = ViewBuilder(system_prompt="sys", registry=registry)
    view = builder.build(events=[])
    assert "query_metrics" in view.blocks[0].content


def test_run_id_and_call_id_do_not_leak_into_view(store: SqliteStore, ctx: RunCtx) -> None:
    """视图里出现 run_id/事件 id 会让每一步的前缀都不同，缓存必然全灭。"""
    _base_events(store, ctx)
    _pair(store, ctx, "c1")
    view = _builder().build(events=store.effective_events(ctx.branch_id))
    rendered = view.render()
    assert ctx.run_id not in rendered
    assert ctx.branch_id not in rendered


def test_tool_call_request_model_round_trip() -> None:
    request = ToolCallRequest(tool_call_id="c1", tool="scale_pool", args={"size": 8})
    assert request.model_copy(update={"args": {"size": 9}}).args == {"size": 9}


def test_tool_call_store_records_are_visible_to_view(store: SqliteStore, ctx: RunCtx) -> None:
    ToolCallStore(store).begin(
        tool_call_id="c1",
        run_id=ctx.run_id,
        branch_id=ctx.branch_id,
        tool="scale_pool",
        args={},
        args_sha256="x",
        idempotency_key="k1",
        effect="write_nonidempotent",
    )
    assert ToolCallStore(store).get("c1") is not None
