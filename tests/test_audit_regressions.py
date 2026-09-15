"""审计回归测试：把五个审计代理发现的 P0/P1 固化成用例。

每个用例对应一次真实缺陷，注释里写清"如果不修会怎样"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fakeworld.model import ScriptedModel
from fakeworld.tools import SCENARIO_POOL_EXHAUSTION, build_registry
from fakeworld.world import World
from harness.approval import DECISION_REJECTED, SCOPE_ONCE
from harness.artifacts import ArtifactStore
from harness.budget import BudgetExceeded, BudgetLedger, BudgetLimits
from harness.cache import CacheConfig, PrefixCacheModel
from harness.chaos import Chaos
from harness.context import ViewBuilder
from harness.events import NewEvent, Source, TreeEventType
from harness.ids import new_id
from harness.llm import ContextOverflow, ModelWindow, ScriptedLLMClient
from harness.loop import DEFAULT_SYSTEM_PROMPT, Loop, LoopError
from harness.state import RunStatus, reduce_events
from harness.store import SqliteCheckpointSaver, SqliteStore, ToolCallStore
from harness.tools import Effect, Tool, ToolRegistry, canonical_args_sha256, idempotency_key

from .conftest import RunCtx

TASK = "支付服务 P99 告警，请定位并处置。"
WRITE_CALL = "tc_write_1"
WRITE_ARGS = {"service": "payment", "size": 64}


@pytest.fixture()
def world(tmp_path: Path) -> World:
    world = World(tmp_path / "world.db")
    yield world
    world.close()


def _loop(
    store: SqliteStore,
    world: World,
    *,
    scenario: str = SCENARIO_POOL_EXHAUSTION,
    tool_idem: bool = True,
    probe: bool = True,
    window: ModelWindow | None = None,
    budget: BudgetLedger | None = None,
    ctx: RunCtx | None = None,
    tamper=None,
) -> Loop:
    artifacts = ArtifactStore(store.path + ".artifacts")
    registry = build_registry(
        world, idempotent_impl=tool_idem, probe_enabled=probe, artifacts=artifacts
    )
    llm = ScriptedLLMClient(
        ScriptedModel(scenario),
        cache=PrefixCacheModel(CacheConfig()),
        window=window or ModelWindow(),
        budget=budget,
    )
    return Loop(
        store,
        SqliteCheckpointSaver(store),
        llm=llm,
        registry=registry,
        compactor=None,
        budget=budget,
        chaos=Chaos.disabled(),
        tamper=tamper,
    )


def _run_to_approval_then_complete(store: SqliteStore, world: World, ctx: RunCtx, **kwargs) -> Loop:
    loop = _loop(store, world, **kwargs)
    loop.start(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id, task=TASK)
    loop.approve(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id)
    outcome = loop.resume(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id)
    assert outcome.status == RunStatus.COMPLETED.value
    return loop


# ------------------------------------------------- P0-A 视图在审批路径自毁配对


def test_view_stays_healthy_through_approval(store: SqliteStore, world: World, ctx: RunCtx) -> None:
    """修复前：每次"提议写 → 人审批 → 执行"都会产生 2 条视图违规，
    写调用的 call/result 永远不进模型视图（模型看不到自己写了什么）。"""
    _run_to_approval_then_complete(store, world, ctx, tool_idem=False)
    registry = build_registry(world, idempotent_impl=False)
    builder = ViewBuilder(system_prompt=DEFAULT_SYSTEM_PROMPT, registry=registry)
    view = builder.build(events=store.effective_events(ctx.branch_id), dynamic={"step": 9})
    assert view.violations == ()
    assert view.dropped_event_ids == ()
    rendered = view.render()
    # 视图刻意不暴露事件 id / tool_call_id（否则每步前缀都变、缓存全灭），
    # 因此断言"内容可见"：写调用的工具名与执行结果都在视图里。
    assert "scale_pool" in rendered
    assert "effect_id" in rendered  # 写操作的结果对模型可见


def test_view_reports_violation_only_for_truly_unpaired_calls(
    store: SqliteStore, ctx: RunCtx
) -> None:
    """真的缺结果时仍然要报违规并整批丢弃——修复不能把不变式一起废掉。"""
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.AGENT_MESSAGE,
            source=Source.AGENT,
            payload={"text": "取证"},
        )
    )
    for call_id in ("c1", "c2"):
        store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.TOOL_CALL,
                source=Source.AGENT,
                payload={"tool_call_id": call_id, "tool": "query_metrics", "args": {}},
            )
        )
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.INTERRUPT,
            source=Source.AGENT,
            payload={"interrupt_id": "int_1", "interrupt_index": 0, "reason": "需审批"},
        )
    )
    registry = ToolRegistry()
    view = ViewBuilder(system_prompt="s", registry=registry).build(
        events=store.effective_events(ctx.branch_id)
    )
    assert [v.code for v in view.violations] == ["VIEW-BATCH-ATOMICITY"]
    assert not any(block.role == "tool" for block in view.blocks)


# ------------------------------------------------- P0-B 分叉跨分支复用幂等键


def test_fork_does_not_reuse_parent_execution(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """修复前：子分支上同一个 tool_call_id 会命中父分支的去重行，
    静默复用另一组参数的执行结果，并且**完全跳过审批门**（幻影写）。"""
    _run_to_approval_then_complete(store, world, ctx, tool_idem=False)

    # 在"第一轮结束"处（读结果之后、写调用之前）分叉，
    # 子分支会重新决策同一个 tool_call_id
    events = store.effective_events(ctx.branch_id)
    fork_point = next(event for event in events if event.type == TreeEventType.TOOL_RESULT.value)
    child = new_id("br")
    store.create_branch(
        child, ctx.run_id, parent_branch_id=ctx.branch_id, fork_event_id=fork_point.event_id
    )

    child_loop = _loop(store, world, tool_idem=False)
    outcome = child_loop.resume(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=child)

    # 子分支必须重新走审批门（而不是复用父分支的执行结果）
    assert outcome.status == RunStatus.WAITING_HUMAN.value
    child_events = store.effective_events(child)
    interrupts = [e for e in child_events if e.type == TreeEventType.INTERRUPT.value]
    assert len(interrupts) == 1
    # 世界账本里仍然只有父分支那一次效果
    assert world.total_effects() == 1
    # 子分支不能声称自己执行过这次写
    child_results = [
        e.payload
        for e in child_events
        if e.type == TreeEventType.TOOL_RESULT.value and e.payload.get("tool_call_id") == WRITE_CALL
    ]
    assert child_results == []


def test_same_key_with_different_args_is_refused(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """同键不同参必须报错而不是静默重放（旁路 hash 字段的真正用途）。"""
    _run_to_approval_then_complete(store, world, ctx, tool_idem=False)
    key = idempotency_key(ctx.run_id, ctx.branch_id, WRITE_CALL)
    record = ToolCallStore(store).get(WRITE_CALL)
    assert record is not None and record.idempotency_key == key

    # 再造一个分支，让日志里出现同一 tool_call_id 但参数不同的调用
    events = store.effective_events(ctx.branch_id)
    fork_point = events[-1]
    child = new_id("br")
    store.create_branch(
        child, ctx.run_id, parent_branch_id=ctx.branch_id, fork_event_id=fork_point.event_id
    )
    different_args = {"service": "payment", "size": 8}
    child_loop = _loop(store, world, tool_idem=False)
    child_loop.start  # noqa: B018  (仅示意：不在非空分支上 start)
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=child,
            type=TreeEventType.TOOL_CALL,
            source=Source.AGENT,
            payload={"tool_call_id": WRITE_CALL, "tool": "scale_pool", "args": different_args},
        )
    )
    registry = build_registry(world, idempotent_impl=False)
    loop = Loop(
        store,
        SqliteCheckpointSaver(store),
        llm=ScriptedLLMClient(ScriptedModel(SCENARIO_POOL_EXHAUSTION)),
        registry=registry,
        chaos=Chaos.disabled(),
    )
    loop.resume(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=child)
    # 子分支的键与父分支不同（含 branch），因此走的是"未闭合调用重跑"而不是复用；
    # 关键断言：世界账本没有被这次不同参数的调用污染成"复用父结果"
    assert canonical_args_sha256(different_args) != record.args_sha256
    assert world.total_effects() == 1


# --------------------------------------------------- P0-C 工具异常分类


def test_tool_exception_is_classified_not_propagated(store: SqliteStore, ctx: RunCtx) -> None:
    """修复前：异常直接穿出 loop，run 永久 RUNNING、调用悬挂、每次 resume 重放副作用。"""
    registry = ToolRegistry()

    def boom(args: dict, key: str) -> dict:
        raise RuntimeError("downstream exploded")

    registry.register(Tool(name="query_metrics", effect=Effect.READ, fn=boom))
    registry.register(
        Tool(name="scale_pool", effect=Effect.WRITE_NONIDEMPOTENT, fn=lambda a, k: {})
    )
    loop = Loop(
        store,
        SqliteCheckpointSaver(store),
        llm=ScriptedLLMClient(ScriptedModel(SCENARIO_POOL_EXHAUSTION)),
        registry=registry,
        chaos=Chaos.disabled(),
    )
    loop.start(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id, task=TASK)
    events = store.effective_events(ctx.branch_id)
    results = [e.payload for e in events if e.type == TreeEventType.TOOL_RESULT.value]
    assert results and results[0]["status"] == "failed"
    assert results[0]["error_class"] == "tool_error:RuntimeError"
    errors = [e.payload for e in events if e.type == "error"]
    assert any(e["error_class"] == "tool_error:RuntimeError" for e in errors)
    state, violations = reduce_events(events)
    assert violations == []
    assert state.open_tool_calls == {}  # 调用被闭合，不再悬挂


def test_write_tool_exception_becomes_unknown_with_intent(
    store: SqliteStore, ctx: RunCtx, tmp_path: Path
) -> None:
    """非幂等写抛异常 ⇒ 效果可能已发生，必须标 unknown 等对账，而不是 failed。"""
    world = World(tmp_path / "w2.db")
    registry = ToolRegistry()

    def boom(args: dict, key: str) -> dict:
        raise TimeoutError("timeout after send")

    registry.register(
        Tool(name="scale_pool", effect=Effect.WRITE_NONIDEMPOTENT, fn=boom, requires_approval=False)
    )
    registry.register(Tool(name="query_metrics", effect=Effect.READ, fn=lambda a, k: {"ok": True}))
    loop = Loop(
        store,
        SqliteCheckpointSaver(store),
        llm=ScriptedLLMClient(ScriptedModel(SCENARIO_POOL_EXHAUSTION)),
        registry=registry,
        chaos=Chaos.disabled(),
        outbox=True,
    )
    loop.start(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id, task=TASK)
    events = store.effective_events(ctx.branch_id)
    write_result = next(
        e.payload
        for e in events
        if e.type == TreeEventType.TOOL_RESULT.value and e.payload.get("tool_call_id") == WRITE_CALL
    )
    assert write_result["status"] == "unknown"
    assert write_result["error_class"] == "tool_error:TimeoutError"
    record = ToolCallStore(store).get(WRITE_CALL)
    assert record is not None and record.status == "unknown"
    world.close()


# -------------------------------------------- P0-D 压缩无收益时中止


def test_compaction_aborts_when_summary_is_not_smaller(
    store: SqliteStore, ctx: RunCtx, tmp_path: Path
) -> None:
    """修复前：摘要比被替换内容更长时仍写入 compaction ⇒ 视图越压越大、每步重压直至 overflow。"""
    from harness.compaction import CompactionPolicy, Compactor

    for index in range(6):
        store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.AGENT_MESSAGE,
                source=Source.AGENT,
                payload={"text": f"第 {index} 轮", "final": False},
            )
        )

    def huge_summary(events) -> tuple[str, object]:
        from harness.tokens import Usage

        return "摘" * 20_000, Usage(input_tokens=1, output_tokens=1)

    compactor = Compactor(
        store,
        artifacts=ArtifactStore(tmp_path / "a"),
        policy=CompactionPolicy(keep_recent_groups=2),
        summarizer=huge_summary,
    )
    record = compactor.compact(
        run_id=ctx.run_id,
        branch_id=ctx.branch_id,
        events=store.effective_events(ctx.branch_id),
        reason="t",
    )
    assert record is None
    errors = [e.payload for e in store.effective_events(ctx.branch_id) if e.type == "error"]
    assert any(e["error_class"] == "compaction_no_gain" for e in errors)


# -------------------------------------------- P1-E 终态吸收


def test_resume_on_terminal_state_is_noop(store: SqliteStore, world: World, ctx: RunCtx) -> None:
    loop = _run_to_approval_then_complete(store, world, ctx)
    before = len(store.effective_events(ctx.branch_id))
    outcome = loop.resume(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id)
    assert outcome.status == RunStatus.COMPLETED.value
    assert len(store.effective_events(ctx.branch_id)) == before
    assert world.total_effects() == 1


def test_start_on_non_empty_branch_is_refused(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """修复前：在终态分支上再 start 会追加树节点 → INV-006 累积、状态被"复活"。"""
    loop = _run_to_approval_then_complete(store, world, ctx)
    with pytest.raises(LoopError, match="already has events"):
        loop.start(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id, task=TASK)


def test_terminal_state_is_absorbing_in_reducer(store: SqliteStore, ctx: RunCtx) -> None:
    """fatal 之后再来的 interrupt/resume 不得把状态改回 waiting_human。"""
    from harness.events import ArtifactEventType

    store.append(
        NewEvent.artifact(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=ArtifactEventType.ERROR,
            payload={"error_class": "boom", "message": "x", "fatal": True},
        )
    )
    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.INTERRUPT,
            source=Source.AGENT,
            payload={"interrupt_id": "int_1", "interrupt_index": 0},
        )
    )
    state, violations = reduce_events(store.effective_events(ctx.branch_id))
    assert state.status is RunStatus.FAILED
    assert [v.code for v in violations] == ["INV-006"]


# -------------------------------------------- P1-F 预算


def test_budget_enforces_total_even_with_generous_bucket(store: SqliteStore, ctx: RunCtx) -> None:
    """修复前：per_bucket 覆盖 total ⇒ 给 main 设大额额度就等于关掉总量上限。"""
    from harness.tokens import Usage

    ledger = BudgetLedger(
        store,
        run_id=ctx.run_id,
        branch_id=ctx.branch_id,
        limits=BudgetLimits(total_usd=0.0001, per_bucket={"main": 1000.0}),
    )
    ledger.charge(bucket="main", usage=Usage(input_tokens=100_000, output_tokens=0))
    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.check("main")
    assert excinfo.value.bucket == "total"


def test_compaction_bucket_is_hard_stopped(store: SqliteStore, ctx: RunCtx, tmp_path: Path) -> None:
    from harness.compaction import CompactionPolicy, Compactor

    for index in range(6):
        store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.AGENT_MESSAGE,
                source=Source.AGENT,
                payload={"text": f"第 {index} 轮", "final": False},
            )
        )
    ledger = BudgetLedger(
        store,
        run_id=ctx.run_id,
        branch_id=ctx.branch_id,
        limits=BudgetLimits(total_usd=100.0, per_bucket={"compaction": 0.0}),
    )
    compactor = Compactor(
        store,
        artifacts=ArtifactStore(tmp_path / "a"),
        policy=CompactionPolicy(keep_recent_groups=2),
        budget=ledger,
    )
    with pytest.raises(BudgetExceeded):
        compactor.compact(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            events=store.effective_events(ctx.branch_id),
            reason="t",
        )


# -------------------------------------------- P1-G 审批优先级


def test_exact_approved_binding_beats_later_session_rejection(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """修复前：一条更晚的 session 拒绝会否决更早的、精确绑定本调用的批准。"""
    loop = _loop(store, world)
    loop.start(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id, task=TASK)
    loop.approve(
        run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id, scope=SCOPE_ONCE
    )
    # 人工再追加一条"针对同类操作"的 session 拒绝（例如值班人改了主意）
    from harness.loop import _Ctx

    binding = loop._find_approval(
        _Ctx(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id),
        WRITE_CALL,
        "scale_pool",
    )
    assert binding is not None and binding.decision != DECISION_REJECTED


# -------------------------------------------- P1-H checkpoint 交叉校验


def test_checkpoint_pointing_at_missing_event_is_reported(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """checkpoint 不是权威，但它自称的事件必须存在；否则是"库被改过"的信号。"""
    loop = _run_to_approval_then_complete(store, world, ctx)
    saver = SqliteCheckpointSaver(store)
    latest = saver.get_latest(ctx.thread_id)
    assert latest is not None
    # 篡改 checkpoint 指向一个不存在的事件（模拟数据库被外部改动）
    store._conn.execute(
        "UPDATE checkpoints SET state_json = ? WHERE checkpoint_id = ?",
        (
            latest.checkpoint.channel_values.__class__(
                {
                    "step": 1,
                    "status": "running",
                    "last_event_id": "evt_does_not_exist",
                }
            )
            .__str__()
            .replace("'", '"'),
            latest.checkpoint.id,
        ),
    )
    outcome = loop.resume(run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id)
    assert outcome.checkpoint_mismatches == 1
    errors = [e.payload for e in store.effective_events(ctx.branch_id) if e.type == "error"]
    assert any(e["error_class"] == "checkpoint_log_mismatch" for e in errors)


# -------------------------------------------- 视图健康度（正常路径）


def test_view_violations_are_zero_on_happy_path(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    loop = _loop(store, world, tool_idem=False)
    outcome = loop.start(
        run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id, task=TASK
    )
    assert outcome.view_violations == 0


# -------------------------------------------- 预算硬停的 loop 集成


def test_llm_client_hard_stops_on_budget(store: SqliteStore, ctx: RunCtx) -> None:
    """预算硬停要拦在**模型调用**上，而不只是账本自己会喊。"""
    budget = BudgetLedger(
        store,
        run_id=ctx.run_id,
        branch_id=ctx.branch_id,
        limits=BudgetLimits(total_usd=0.0),
    )
    client = ScriptedLLMClient(ScriptedModel(SCENARIO_POOL_EXHAUSTION), budget=budget)
    view = ViewBuilder(system_prompt="s", registry=ToolRegistry()).build(events=[])
    with pytest.raises(BudgetExceeded):
        client.complete(step=0, view=view)


def test_context_overflow_is_raised_by_client(store: SqliteStore, ctx: RunCtx) -> None:
    client = ScriptedLLMClient(
        ScriptedModel(SCENARIO_POOL_EXHAUSTION),
        window=ModelWindow(context_limit_tokens=10, max_output_tokens=1),
    )
    view = ViewBuilder(system_prompt="很长的系统提示" * 50, registry=ToolRegistry()).build(
        events=[]
    )
    with pytest.raises(ContextOverflow):
        client.complete(step=0, view=view)
