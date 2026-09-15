"""loop 与 resume（W3）：审批门、改参、TOCTOU、outbox 对账与 unknown 语义。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fakeworld.model import ScriptedModel
from fakeworld.tools import SCENARIO_POOL_EXHAUSTION, SCENARIO_TWO_WRITES, build_registry
from fakeworld.world import World
from harness.approval import ApprovalError
from harness.chaos import Chaos
from harness.events import ArtifactEventType, NewEvent, Source, TreeEventType
from harness.loop import Loop, ModelTurn, RunOutcome
from harness.state import RunStatus, reduce_events
from harness.store import SqliteCheckpointSaver, SqliteStore, ToolCallStore
from harness.tools import canonical_args_sha256, idempotency_key

from .conftest import RunCtx

TASK = "支付服务 P99 告警，请定位并处置。"
WRITE_CALL = "tc_write_1"
WRITE_ARGS = {"service": "payment", "size": 64}


@pytest.fixture()
def world(tmp_path: Path) -> World:
    world = World(tmp_path / "world.db")
    yield world
    world.close()


def _loop(store: SqliteStore, **kwargs) -> Loop:
    return Loop(store, SqliteCheckpointSaver(store), chaos=Chaos.disabled(), **kwargs)


def _registry(world: World, *, tool_idem: bool = True, probe: bool = True):
    return build_registry(world, idempotent_impl=tool_idem, probe_enabled=probe)


def _start(store: SqliteStore, world: World, ctx: RunCtx, **kwargs) -> RunOutcome:
    tool_idem = kwargs.pop("tool_idem", True)
    probe = kwargs.pop("probe", True)
    return _loop(store, **kwargs).start(
        run_id=ctx.run_id,
        thread_id=ctx.thread_id,
        branch_id=ctx.branch_id,
        model=ScriptedModel(SCENARIO_POOL_EXHAUSTION),
        registry=_registry(world, tool_idem=tool_idem, probe=probe),
        task=TASK,
    )


def _approve(store: SqliteStore, ctx: RunCtx, **kwargs):
    return _loop(store).approve(
        run_id=ctx.run_id, thread_id=ctx.thread_id, branch_id=ctx.branch_id, **kwargs
    )


def _resume(store: SqliteStore, world: World, ctx: RunCtx, **kwargs) -> RunOutcome:
    tool_idem = kwargs.pop("tool_idem", True)
    probe = kwargs.pop("probe", True)
    return _loop(store, **kwargs).resume(
        run_id=ctx.run_id,
        thread_id=ctx.thread_id,
        branch_id=ctx.branch_id,
        model=ScriptedModel(SCENARIO_POOL_EXHAUSTION),
        registry=_registry(world, tool_idem=tool_idem, probe=probe),
    )


def _results(store: SqliteStore, ctx: RunCtx) -> list[dict]:
    return [
        e.payload
        for e in store.effective_events(ctx.branch_id)
        if e.type == TreeEventType.TOOL_RESULT.value
    ]


def _seed_pending_intent(store: SqliteStore, ctx: RunCtx) -> str:
    key = idempotency_key(ctx.run_id, WRITE_CALL)
    ToolCallStore(store).begin(
        tool_call_id=WRITE_CALL,
        run_id=ctx.run_id,
        branch_id=ctx.branch_id,
        tool="scale_pool",
        args=WRITE_ARGS,
        args_sha256=canonical_args_sha256(WRITE_ARGS),
        idempotency_key=key,
        effect="write_nonidempotent",
    )
    return key


# --------------------------------------------------------------------- 审批门


def test_run_stops_at_approval_gate(store: SqliteStore, world: World, ctx: RunCtx) -> None:
    outcome = _start(store, world, ctx)
    assert outcome.status == RunStatus.WAITING_HUMAN.value
    assert outcome.executed == 1  # 只跑了只读工具
    assert world.total_effects() == 0
    state, violations = reduce_events(store.effective_events(ctx.branch_id))
    assert violations == []
    assert state.pending_interrupt_id is not None


def test_resume_before_approval_is_noop(store: SqliteStore, world: World, ctx: RunCtx) -> None:
    _start(store, world, ctx)
    events_before = len(store.effective_events(ctx.branch_id))
    outcome = _resume(store, world, ctx)
    assert outcome.status == RunStatus.WAITING_HUMAN.value
    assert len(store.effective_events(ctx.branch_id)) == events_before
    assert world.total_effects() == 0


def test_approve_then_resume_completes(store: SqliteStore, world: World, ctx: RunCtx) -> None:
    _start(store, world, ctx)
    binding = _approve(store, ctx)
    assert binding.decision == "approved" and not binding.edited
    outcome = _resume(store, world, ctx)
    assert outcome.status == RunStatus.COMPLETED.value
    assert world.total_effects() == 1
    state, violations = reduce_events(store.effective_events(ctx.branch_id))
    assert violations == []
    assert state.open_tool_calls == {}


def test_rejected_approval_produces_no_effect(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    _start(store, world, ctx)
    _approve(store, ctx, decision="rejected", actor="human:sre")
    outcome = _resume(store, world, ctx)
    assert outcome.status == RunStatus.COMPLETED.value
    assert world.total_effects() == 0
    assert _results(store, ctx)[-1]["status"] == "rejected"


def test_expired_approval_is_rejected_at_execution(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    _start(store, world, ctx)
    _approve(store, ctx, ttl_seconds=-1)
    outcome = _resume(store, world, ctx)
    assert world.total_effects() == 0
    assert outcome.rejected == 1
    assert _results(store, ctx)[-1]["error_class"] == "approval_expired"
    errors = [
        e.payload["error_class"]
        for e in store.effective_events(ctx.branch_id)
        if e.type == ArtifactEventType.ERROR.value
    ]
    assert "approval_expired" in errors


def test_approve_without_open_interrupt_raises(store: SqliteStore, ctx: RunCtx) -> None:
    with pytest.raises(ApprovalError, match="no open interrupt"):
        _approve(store, ctx)


def test_approve_twice_raises(store: SqliteStore, world: World, ctx: RunCtx) -> None:
    _start(store, world, ctx)
    _approve(store, ctx)
    with pytest.raises(ApprovalError, match="no open interrupt"):
        _approve(store, ctx)


def test_approval_after_crash_is_revalidated_not_resent(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """批准后崩溃、恢复重跑：不应再次要求审批，也不应重复执行。"""
    _start(store, world, ctx)
    _approve(store, ctx)
    first = _resume(store, world, ctx)
    second = _resume(store, world, ctx)
    assert first.status == second.status == RunStatus.COMPLETED.value
    assert world.total_effects() == 1
    assert second.executed == 0


# ------------------------------------------------------------------ 改参与 TOCTOU


def test_edited_approval_creates_new_call_and_key(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    _start(store, world, ctx)
    edited_args = {"service": "payment", "size": 32}
    binding = _approve(store, ctx, approved_args=edited_args)
    assert binding.edited and binding.tool_call_id == f"{WRITE_CALL}__edit1"

    outcome = _resume(store, world, ctx)
    assert outcome.status == RunStatus.COMPLETED.value
    effects = world.all_effects()
    assert len(effects) == 1
    assert json.loads(effects[0]["payload_json"]) == edited_args
    # 改参 ⇒ 换键：执行用的键必须是新调用的键
    assert effects[0]["idempotency_key"] == idempotency_key(ctx.run_id, f"{WRITE_CALL}__edit1")
    assert effects[0]["idempotency_key"] != idempotency_key(ctx.run_id, WRITE_CALL)
    statuses = {r["tool_call_id"]: r["status"] for r in _results(store, ctx)}
    assert statuses[WRITE_CALL] == "superseded"
    assert statuses[f"{WRITE_CALL}__edit1"] == "executed"


def test_rejection_cannot_carry_edited_args(store: SqliteStore, world: World, ctx: RunCtx) -> None:
    _start(store, world, ctx)
    with pytest.raises(ApprovalError, match="rejection cannot carry edited args"):
        _approve(store, ctx, decision="rejected", approved_args={"service": "payment", "size": 8})


def test_tampered_args_are_rejected_before_execution(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """批准 A、执行 B：执行前的 hash 复核必须拦住，且不产生任何副作用。"""
    _start(store, world, ctx)
    _approve(store, ctx)

    def tamper(request):
        if request.tool_call_id == WRITE_CALL and isinstance(request.args.get("size"), int):
            return request.model_copy(
                update={"args": {**request.args, "size": request.args["size"] + 8}}
            )
        return request

    outcome = _resume(store, world, ctx, tamper=tamper)
    assert world.total_effects() == 0
    assert outcome.rejected == 1
    assert _results(store, ctx)[-1]["error_class"] == "args_sha256_mismatch"


def test_edited_args_are_what_gets_executed(store: SqliteStore, world: World, ctx: RunCtx) -> None:
    _start(store, world, ctx)
    binding = _approve(store, ctx, approved_args={"service": "payment", "size": 32})
    assert binding.approved_args_sha256 == canonical_args_sha256({"service": "payment", "size": 32})
    assert binding.requested_args_sha256 != binding.approved_args_sha256


# --------------------------------------------------------------------- outbox


def test_pending_intent_with_probe_applied_is_reconciled(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """崩溃在"效果已发生、结果未记录"处：有意图行 + 有读回 → 对账，不重跑。"""
    _start(store, world, ctx)
    _approve(store, ctx)
    key = _seed_pending_intent(store, ctx)
    world.apply(
        operation="scale_pool",
        payload=WRITE_ARGS,
        idempotency_key=key,
        idempotent_impl=False,
    )

    outcome = _resume(store, world, ctx)
    assert outcome.status == RunStatus.COMPLETED.value
    assert outcome.reconciled == 1
    assert world.total_effects() == 1  # 对账而非重跑
    assert world.duplicate_keys() == {}
    assert _results(store, ctx)[-1]["result"]["reconstructed_from_probe"] is True


def test_pending_intent_with_probe_not_applied_executes_once(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """崩溃在"意图已写、效果未发生"处：探针确认未生效 → 可以安全执行。"""
    _start(store, world, ctx)
    _approve(store, ctx)
    _seed_pending_intent(store, ctx)

    outcome = _resume(store, world, ctx)
    assert outcome.probes == 1
    assert outcome.executed == 1
    assert world.total_effects() == 1


def test_pending_intent_without_probe_becomes_unknown(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """没有按键读回：不重跑，标记 unknown 交人工对账（把静默重复变成显式未知）。"""
    _start(store, world, ctx, probe=False)
    _approve(store, ctx)
    _seed_pending_intent(store, ctx)

    outcome = _resume(store, world, ctx, probe=False)
    assert outcome.unknown == 1
    assert outcome.executed == 0  # 绝不自动重跑
    assert world.total_effects() == 0
    record = ToolCallStore(store).get(WRITE_CALL)
    assert record is not None and record.status == "unknown"
    assert _results(store, ctx)[-1]["status"] == "unknown"


def test_unknown_row_is_not_retried_on_later_resume(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    test_pending_intent_without_probe_becomes_unknown(store, world, ctx)
    outcome = _resume(store, world, ctx, probe=False)
    assert outcome.executed == 0
    assert world.total_effects() == 0


def test_outbox_records_intent_before_execution(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """非幂等写在正常路径上也留下可审计的意图行（闭合为 executed）。"""
    _start(store, world, ctx, tool_idem=False)
    _approve(store, ctx)
    _resume(store, world, ctx, tool_idem=False)
    record = ToolCallStore(store).get(WRITE_CALL)
    assert record is not None and record.status == "executed"
    assert record.effect == "write_nonidempotent"


# ----------------------------------------------------------- 既有行为回归（W1/W2）


def test_unclosed_call_without_intent_is_reexecuted(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """W2 语义仍然成立：没有意图行的未闭合调用按 at-least-once 重跑。

    补跑完成后流程继续，并在写工具的审批门处停下（W3 新增的门）。
    """
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
    outcome = _resume(store, world, ctx)
    assert outcome.status == RunStatus.WAITING_HUMAN.value  # 走到写工具的审批门停下
    assert outcome.executed == 1  # 只补跑了那只读工具
    statuses = {r["tool_call_id"]: r["status"] for r in _results(store, ctx)}
    assert statuses["tc_read_1"] == "executed"


def test_read_tool_does_not_need_approval(store: SqliteStore, world: World, ctx: RunCtx) -> None:
    """不需要审批的工具在 resume 后立即执行，不产生 interrupt。"""
    _start(store, world, ctx)
    interrupts = [
        e for e in store.effective_events(ctx.branch_id) if e.type == TreeEventType.INTERRUPT.value
    ]
    assert len(interrupts) == 1  # 只有那次写操作触发了审批
    assert interrupts[0].payload["tool"] == "scale_pool"


def test_scripted_model_signature_matches_loop_protocol() -> None:
    model = ScriptedModel(SCENARIO_POOL_EXHAUSTION)
    turn: ModelTurn = model.next_turn(step=0, view=[])
    assert turn.tool_calls[0].tool == "query_metrics"


# ------------------------------------------------------------------ session scope


def _start_two_writes(store: SqliteStore, world: World, ctx: RunCtx) -> RunOutcome:
    return _loop(store).start(
        run_id=ctx.run_id,
        thread_id=ctx.thread_id,
        branch_id=ctx.branch_id,
        model=ScriptedModel(SCENARIO_TWO_WRITES),
        registry=_registry(world),
        task=TASK,
    )


def _resume_two_writes(store: SqliteStore, world: World, ctx: RunCtx) -> RunOutcome:
    return _loop(store).resume(
        run_id=ctx.run_id,
        thread_id=ctx.thread_id,
        branch_id=ctx.branch_id,
        model=ScriptedModel(SCENARIO_TWO_WRITES),
        registry=_registry(world),
    )


def test_session_scope_authorizes_second_identical_call(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """session 批准覆盖"同工具+同参数"的后续调用：第二次不再触发审批门。"""
    _start_two_writes(store, world, ctx)
    _approve(store, ctx, scope="session")
    outcome = _resume_two_writes(store, world, ctx)
    assert outcome.status == RunStatus.COMPLETED.value
    assert world.total_effects() == 2
    assert len({e["idempotency_key"] for e in world.all_effects()}) == 2
    interrupts = [
        e for e in store.effective_events(ctx.branch_id) if e.type == TreeEventType.INTERRUPT.value
    ]
    assert len(interrupts) == 1  # 只问过一次


def test_once_scope_requires_approval_for_each_call(
    store: SqliteStore, world: World, ctx: RunCtx
) -> None:
    """once 批准只覆盖绑定的那次调用：第二次必须再开审批门。"""
    _start_two_writes(store, world, ctx)
    _approve(store, ctx, scope="once")
    outcome = _resume_two_writes(store, world, ctx)
    assert outcome.status == RunStatus.WAITING_HUMAN.value
    assert world.total_effects() == 1
    interrupts = [
        e for e in store.effective_events(ctx.branch_id) if e.type == TreeEventType.INTERRUPT.value
    ]
    assert len(interrupts) == 2
