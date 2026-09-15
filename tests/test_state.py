"""派生状态折叠：不变量与重放确定性。"""

from __future__ import annotations

from harness.events import ArtifactEventType, NewEvent, Source, TreeEventType
from harness.state import RunStatus, Violation, reduce_events
from harness.store import SqliteStore


def _tool_call(run_id: str, branch_id: str, call_id: str, tool: str = "query_metrics") -> NewEvent:
    return NewEvent.tree(
        run_id=run_id,
        branch_id=branch_id,
        type=TreeEventType.TOOL_CALL,
        source=Source.AGENT,
        payload={"tool_call_id": call_id, "tool": tool, "args": {}},
    )


def _tool_result(run_id: str, branch_id: str, call_id: str, status: str = "executed") -> NewEvent:
    return NewEvent.tree(
        run_id=run_id,
        branch_id=branch_id,
        type=TreeEventType.TOOL_RESULT,
        source=Source.TOOL,
        payload={"tool_call_id": call_id, "status": status},
    )


def _codes(violations: list[Violation]) -> list[str]:
    return [v.code for v in violations]


def test_happy_path_reaches_completed(store: SqliteStore, run_ctx: tuple[str, str]) -> None:
    run_id, branch_id = run_ctx
    store.append_many(
        [
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.USER_MESSAGE,
                source=Source.USER,
                payload={"text": "cpu 告警"},
            ),
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.AGENT_MESSAGE,
                source=Source.AGENT,
                payload={"text": "先看指标"},
            ),
            _tool_call(run_id, branch_id, "call_1"),
            _tool_result(run_id, branch_id, "call_1"),
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.AGENT_MESSAGE,
                source=Source.AGENT,
                payload={"text": "根因是连接池耗尽", "final": True},
            ),
        ]
    )
    state, violations = reduce_events(store.effective_events(branch_id))
    assert violations == []
    assert state.status is RunStatus.COMPLETED
    assert state.step == 2
    assert state.open_tool_calls == {}
    assert state.closed_tool_calls == ("call_1",)


def test_orphan_tool_result_is_flagged(store: SqliteStore, run_ctx: tuple[str, str]) -> None:
    run_id, branch_id = run_ctx
    store.append(_tool_result(run_id, branch_id, "call_missing"))
    _, violations = reduce_events(store.effective_events(branch_id))
    assert _codes(violations) == ["INV-002"]


def test_duplicate_tool_call_id_is_flagged(store: SqliteStore, run_ctx: tuple[str, str]) -> None:
    run_id, branch_id = run_ctx
    store.append_many(
        [_tool_call(run_id, branch_id, "call_1"), _tool_call(run_id, branch_id, "call_1")]
    )
    _, violations = reduce_events(store.effective_events(branch_id))
    assert _codes(violations) == ["INV-001"]


def test_double_tool_result_is_flagged(store: SqliteStore, run_ctx: tuple[str, str]) -> None:
    run_id, branch_id = run_ctx
    store.append_many(
        [
            _tool_call(run_id, branch_id, "call_1"),
            _tool_result(run_id, branch_id, "call_1"),
            _tool_result(run_id, branch_id, "call_1"),
        ]
    )
    _, violations = reduce_events(store.effective_events(branch_id))
    assert _codes(violations) == ["INV-003"]


def test_interrupt_without_resume_waits_for_human(
    store: SqliteStore, run_ctx: tuple[str, str]
) -> None:
    run_id, branch_id = run_ctx
    store.append(
        NewEvent.tree(
            run_id=run_id,
            branch_id=branch_id,
            type=TreeEventType.INTERRUPT,
            source=Source.AGENT,
            payload={"interrupt_id": "int_1", "reason": "高风险操作需审批"},
        )
    )
    state, violations = reduce_events(store.effective_events(branch_id))
    assert violations == []
    assert state.status is RunStatus.WAITING_HUMAN
    assert state.pending_interrupt_id == "int_1"


def test_resume_without_interrupt_is_flagged(store: SqliteStore, run_ctx: tuple[str, str]) -> None:
    run_id, branch_id = run_ctx
    store.append(
        NewEvent.tree(
            run_id=run_id,
            branch_id=branch_id,
            type=TreeEventType.RESUME,
            source=Source.USER,
            payload={"interrupt_id": "int_1", "values": {"decision": "approved"}},
        )
    )
    _, violations = reduce_events(store.effective_events(branch_id))
    assert _codes(violations) == ["INV-005"]


def test_double_interrupt_is_flagged(store: SqliteStore, run_ctx: tuple[str, str]) -> None:
    run_id, branch_id = run_ctx
    for index in (1, 2):
        store.append(
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.INTERRUPT,
                source=Source.AGENT,
                payload={"interrupt_id": f"int_{index}"},
            )
        )
    _, violations = reduce_events(store.effective_events(branch_id))
    assert _codes(violations) == ["INV-004"]


def test_resume_after_interrupt_returns_to_running(
    store: SqliteStore, run_ctx: tuple[str, str]
) -> None:
    run_id, branch_id = run_ctx
    store.append_many(
        [
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.INTERRUPT,
                source=Source.AGENT,
                payload={"interrupt_id": "int_1"},
            ),
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.RESUME,
                source=Source.USER,
                payload={"interrupt_id": "int_1", "values": {"decision": "approved"}},
            ),
        ]
    )
    state, violations = reduce_events(store.effective_events(branch_id))
    assert violations == []
    assert state.status is RunStatus.RUNNING
    assert state.pending_interrupt_id is None


def test_tree_node_after_terminal_is_flagged(store: SqliteStore, run_ctx: tuple[str, str]) -> None:
    run_id, branch_id = run_ctx
    store.append_many(
        [
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.AGENT_MESSAGE,
                source=Source.AGENT,
                payload={"text": "done", "final": True},
            ),
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.USER_MESSAGE,
                source=Source.USER,
                payload={"text": "还在吗"},
            ),
        ]
    )
    _, violations = reduce_events(store.effective_events(branch_id))
    assert _codes(violations) == ["INV-006"]


def test_fatal_error_marks_failed_and_blocks_further_nodes(
    store: SqliteStore, run_ctx: tuple[str, str]
) -> None:
    run_id, branch_id = run_ctx
    store.append_many(
        [
            NewEvent.artifact(
                run_id=run_id,
                branch_id=branch_id,
                type=ArtifactEventType.ERROR,
                payload={"error_class": "provider_timeout", "message": "boom", "fatal": True},
            ),
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.AGENT_MESSAGE,
                source=Source.AGENT,
                payload={"text": "继续"},
            ),
        ]
    )
    state, violations = reduce_events(store.effective_events(branch_id))
    assert state.status is RunStatus.FAILED
    assert _codes(violations) == ["INV-006"]


def test_replay_is_deterministic(store: SqliteStore, run_ctx: tuple[str, str]) -> None:
    run_id, branch_id = run_ctx
    store.append_many(
        [
            _tool_call(run_id, branch_id, "call_1"),
            _tool_result(run_id, branch_id, "call_1"),
            NewEvent.artifact(
                run_id=run_id,
                branch_id=branch_id,
                type=ArtifactEventType.BUDGET_UPDATE,
                payload={"bucket": "main", "tokens_in": 120},
            ),
        ]
    )
    log = store.effective_events(branch_id)
    first, _ = reduce_events(log)
    second, _ = reduce_events(log)
    assert first.fingerprint() == second.fingerprint()


# ------------------------------------------------- 审计补充：缺失的不变量分支


def test_inv007_resume_index_mismatch_is_flagged(
    store: SqliteStore, run_ctx: tuple[str, str]
) -> None:
    """INV-007：resume 携带的 interrupt_index 与未闭合 interrupt 不一致。

    这条被审计点名为"零覆盖"：只对 id 不打索引的实现会把 resume 值接到错误的
    interrupt 上，而当时删掉这段校验，整个测试套件仍然全绿。
    """
    run_id, branch_id = run_ctx
    store.append_many(
        [
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.INTERRUPT,
                source=Source.AGENT,
                payload={"interrupt_id": "int_1", "interrupt_index": 0},
            ),
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.RESUME,
                source=Source.USER,
                payload={"interrupt_id": "int_1", "interrupt_index": 1, "decision": "approved"},
            ),
        ]
    )
    _, violations = reduce_events(store.effective_events(branch_id))
    assert _codes(violations) == ["INV-007"]


def test_inv007_matching_index_passes(store: SqliteStore, run_ctx: tuple[str, str]) -> None:
    run_id, branch_id = run_ctx
    store.append_many(
        [
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.INTERRUPT,
                source=Source.AGENT,
                payload={"interrupt_id": "int_1", "interrupt_index": 3},
            ),
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.RESUME,
                source=Source.USER,
                payload={"interrupt_id": "int_1", "interrupt_index": 3, "decision": "approved"},
            ),
        ]
    )
    state, violations = reduce_events(store.effective_events(branch_id))
    assert violations == []
    assert state.pending_interrupt_id is None


def test_inv001_duplicate_after_close_is_flagged(
    store: SqliteStore, run_ctx: tuple[str, str]
) -> None:
    """INV-001 的另一半：调用已闭合后再出现同一 id（此前只测了未闭合时的重复）。"""
    run_id, branch_id = run_ctx
    store.append_many(
        [
            _tool_call(run_id, branch_id, "call_1"),
            _tool_result(run_id, branch_id, "call_1"),
            _tool_call(run_id, branch_id, "call_1"),
        ]
    )
    _, violations = reduce_events(store.effective_events(branch_id))
    assert _codes(violations) == ["INV-001"]


def test_inv001_missing_call_id_is_flagged(store: SqliteStore, run_ctx: tuple[str, str]) -> None:
    run_id, branch_id = run_ctx
    store.append(
        NewEvent.tree(
            run_id=run_id,
            branch_id=branch_id,
            type=TreeEventType.TOOL_CALL,
            source=Source.AGENT,
            payload={"tool": "query_metrics", "args": {}},
        )
    )
    _, violations = reduce_events(store.effective_events(branch_id))
    assert _codes(violations) == ["INV-001"]


def test_inv006_final_with_open_calls_is_flagged(
    store: SqliteStore, run_ctx: tuple[str, str]
) -> None:
    """INV-006 的另一半：模型宣布终局，但仍有权调用没闭合（此前该分支从未执行）。"""
    run_id, branch_id = run_ctx
    store.append_many(
        [
            _tool_call(run_id, branch_id, "call_1"),
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.AGENT_MESSAGE,
                source=Source.AGENT,
                payload={"text": "结束了", "final": True},
            ),
        ]
    )
    _, violations = reduce_events(store.effective_events(branch_id))
    assert _codes(violations) == ["INV-006"]
    assert "open tool calls" in violations[0].detail


def test_fingerprint_distinguishes_different_logs(
    store: SqliteStore, run_ctx: tuple[str, str]
) -> None:
    """指纹必须能区分不同日志（此前只做自比较，返回常量也能通过）。"""
    run_id, branch_id = run_ctx
    store.append(_tool_call(run_id, branch_id, "call_1"))
    first, _ = reduce_events(store.effective_events(branch_id))
    store.append(_tool_result(run_id, branch_id, "call_1"))
    second, _ = reduce_events(store.effective_events(branch_id))
    assert first.fingerprint() != second.fingerprint()
