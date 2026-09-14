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
