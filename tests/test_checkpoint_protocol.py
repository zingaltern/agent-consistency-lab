"""Checkpoint/writes 提交协议：边界提交、幂等写入、metadata 保真、恢复计划。"""

from __future__ import annotations

from harness.store import SqliteCheckpointSaver, SqliteStore, Write, WriteIdx, classify_writes

from .conftest import RunCtx


def _saver(store: SqliteStore) -> SqliteCheckpointSaver:
    return SqliteCheckpointSaver(store)


def test_put_then_get_latest_round_trips_state(store: SqliteStore, ctx: RunCtx) -> None:
    saver = _saver(store)
    checkpoint = saver.put(
        thread_id=ctx.thread_id,
        branch_id=ctx.branch_id,
        channel_values={"step": 1, "seen": ["a"]},
        source="loop",
        step=1,
    )
    latest = saver.get_latest(ctx.thread_id)
    assert latest is not None
    assert latest.checkpoint.id == checkpoint.id
    assert latest.checkpoint.channel_values == {"step": 1, "seen": ["a"]}
    assert latest.checkpoint.updated_channels == ["seen", "step"]


def test_metadata_preserves_unknown_keys(store: SqliteStore, ctx: RunCtx) -> None:
    saver = _saver(store)
    saver.put(
        thread_id=ctx.thread_id,
        branch_id=ctx.branch_id,
        channel_values={},
        source="loop",
        metadata={"future_field": {"nested": [1, 2]}, "step": 7},
    )
    latest = saver.get_latest(ctx.thread_id)
    assert latest is not None
    assert latest.metadata["future_field"] == {"nested": [1, 2]}
    assert latest.metadata["step"] == 7
    assert latest.metadata["_runtime"]["schema_version"] == 1
    assert latest.metadata["_runtime"]["source"] == "loop"


def test_put_writes_is_idempotent(store: SqliteStore, ctx: RunCtx) -> None:
    saver = _saver(store)
    checkpoint = saver.put(
        thread_id=ctx.thread_id, branch_id=ctx.branch_id, channel_values={}, source="loop"
    )
    write = Write(task_id="task_1", idx=0, channel="node_output", payload={"summary": "ok"})
    saver.put_writes(thread_id=ctx.thread_id, checkpoint_id=checkpoint.id, writes=[write])
    saver.put_writes(thread_id=ctx.thread_id, checkpoint_id=checkpoint.id, writes=[write])
    latest = saver.get_latest(ctx.thread_id)
    assert latest is not None
    assert len(latest.pending_writes) == 1


def test_classify_writes_uses_severity_order() -> None:
    writes = [
        Write(task_id="t_done", idx=0, channel="out", payload=None),
        Write(task_id="t_err", idx=WriteIdx.ERROR, channel="error", payload={"message": "x"}),
        Write(task_id="t_int", idx=WriteIdx.INTERRUPT, channel="interrupt", payload={}),
        Write(task_id="t_sched", idx=WriteIdx.SCHEDULED, channel="scheduled", payload={}),
    ]
    outcomes = classify_writes(writes)
    assert outcomes == {
        "t_done": "completed",
        "t_err": "error",
        "t_int": "interrupted",
        "t_sched": "scheduled",
    }


def test_recovery_plan_without_checkpoint_resumes_from_log_head(
    store: SqliteStore, ctx: RunCtx
) -> None:
    from harness.events import NewEvent, Source, TreeEventType

    store.append(
        NewEvent.tree(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            type=TreeEventType.USER_MESSAGE,
            source=Source.USER,
            payload={"text": "hi"},
        )
    )
    plan = _saver(store).recovery_plan(ctx.thread_id)
    assert plan.checkpoint is None
    assert plan.resume_from_seq == 1
    assert "event log head" in plan.notes


def test_recovery_plan_replays_completed_writes_only(store: SqliteStore, ctx: RunCtx) -> None:
    saver = _saver(store)
    checkpoint = saver.put(
        thread_id=ctx.thread_id, branch_id=ctx.branch_id, channel_values={"step": 1}, source="loop"
    )
    saver.put_writes(
        thread_id=ctx.thread_id,
        checkpoint_id=checkpoint.id,
        writes=[
            Write(task_id="t_done", idx=0, channel="out", payload={"v": 1}),
            Write(task_id="t_partial", idx=0, channel="out", payload={"v": 2}),
            Write(task_id="t_partial", idx=WriteIdx.ERROR, channel="error", payload={"e": "crash"}),
        ],
    )
    plan = saver.recovery_plan(ctx.thread_id)
    assert plan.checkpoint is not None
    assert plan.task_outcomes == {"t_done": "completed", "t_partial": "error"}
    assert [w.task_id for w in plan.replay_writes] == ["t_done"]
    assert "side effects must NOT re-run" in plan.notes


def test_latest_checkpoint_wins_by_created_at(store: SqliteStore, ctx: RunCtx) -> None:
    saver = _saver(store)
    first = saver.put(
        thread_id=ctx.thread_id, branch_id=ctx.branch_id, channel_values={"step": 1}, source="loop"
    )
    second = saver.put(
        thread_id=ctx.thread_id,
        branch_id=ctx.branch_id,
        channel_values={"step": 2},
        source="loop",
        parent_checkpoint_id=first.id,
    )
    latest = saver.get_latest(ctx.thread_id)
    assert latest is not None and latest.checkpoint.id == second.id
    assert latest.checkpoint.parent_checkpoint_id == first.id
