"""事件日志：append-only、seq 连续性、kind/type 约束、分叉语义。"""

from __future__ import annotations

import sqlite3

import pytest

from harness.events import ArtifactEventType, EventKind, NewEvent, Source, TreeEventType
from harness.ids import new_id
from harness.store import SqliteStore, StoreError


def _user_msg(run_id: str, branch_id: str, text: str = "hello") -> NewEvent:
    return NewEvent.tree(
        run_id=run_id,
        branch_id=branch_id,
        type=TreeEventType.USER_MESSAGE,
        source=Source.USER,
        payload={"text": text},
    )


class TestAppend:
    def test_assigns_contiguous_seq(self, store: SqliteStore, run_ctx: tuple[str, str]) -> None:
        run_id, branch_id = run_ctx
        events = store.append_many([_user_msg(run_id, branch_id) for _ in range(3)])
        assert [e.seq for e in events] == [0, 1, 2]
        assert all(e.kind is EventKind.TREE_NODE for e in events)

    def test_next_seq_continues_after_reload(
        self, store: SqliteStore, run_ctx: tuple[str, str]
    ) -> None:
        run_id, branch_id = run_ctx
        store.append(_user_msg(run_id, branch_id))
        assert store.next_seq(branch_id) == 1

    def test_rejects_unknown_branch(self, store: SqliteStore) -> None:
        with pytest.raises(StoreError, match="unknown branch"):
            store.append(_user_msg("run_x", "br_missing"))

    def test_rejects_parent_from_other_branch(
        self, store: SqliteStore, run_ctx: tuple[str, str]
    ) -> None:
        run_id, branch_a = run_ctx
        branch_b = new_id("br")
        store.create_branch(branch_b, run_id)
        first = store.append(_user_msg(run_id, branch_a))
        with pytest.raises(StoreError, match="different branch"):
            store.append(
                NewEvent.tree(
                    run_id=run_id,
                    branch_id=branch_b,
                    type=TreeEventType.AGENT_MESSAGE,
                    source=Source.AGENT,
                    parent_id=first.event_id,
                )
            )

    def test_rejects_unknown_parent(self, store: SqliteStore, run_ctx: tuple[str, str]) -> None:
        run_id, branch_id = run_ctx
        with pytest.raises(StoreError, match="unknown parent"):
            store.append(
                NewEvent.tree(
                    run_id=run_id,
                    branch_id=branch_id,
                    type=TreeEventType.AGENT_MESSAGE,
                    source=Source.AGENT,
                    parent_id="evt_nope",
                )
            )


class TestAppendOnly:
    def test_sql_trigger_blocks_update(self, store: SqliteStore, run_ctx: tuple[str, str]) -> None:
        run_id, branch_id = run_ctx
        event = store.append(_user_msg(run_id, branch_id))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._conn.execute(
                "UPDATE events SET type='agent_message' WHERE event_id=?", (event.event_id,)
            )

    def test_sql_trigger_blocks_delete(self, store: SqliteStore, run_ctx: tuple[str, str]) -> None:
        run_id, branch_id = run_ctx
        event = store.append(_user_msg(run_id, branch_id))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._conn.execute("DELETE FROM events WHERE event_id=?", (event.event_id,))

    def test_history_survives_across_connections(self, tmp_path) -> None:
        path = tmp_path / "persist.db"
        with SqliteStore(path) as store:
            run_id, branch_id = new_id("run"), new_id("br")
            store.create_run(run_id, thread_id="t1")
            store.create_branch(branch_id, run_id)
            store.append(_user_msg(run_id, branch_id, "persisted"))
        with SqliteStore(path) as reopened:
            events = reopened.effective_events(branch_id)
            assert [e.payload["text"] for e in events] == ["persisted"]


class TestKindTypeValidation:
    def test_tree_type_requires_tree_kind(self) -> None:
        with pytest.raises(ValueError, match="not a valid"):
            NewEvent(
                run_id="r",
                branch_id="b",
                kind=EventKind.ARTIFACT,
                type=TreeEventType.USER_MESSAGE.value,
                source=Source.USER,
            )

    def test_artifact_type_requires_artifact_kind(self) -> None:
        with pytest.raises(ValueError, match="not a valid"):
            NewEvent(
                run_id="r",
                branch_id="b",
                kind=EventKind.TREE_NODE,
                type=ArtifactEventType.ERROR.value,
                source=Source.SYSTEM,
            )

    def test_artifact_events_are_not_tree_nodes(
        self, store: SqliteStore, run_ctx: tuple[str, str]
    ) -> None:
        run_id, branch_id = run_ctx
        store.append(_user_msg(run_id, branch_id))
        store.append(
            NewEvent.artifact(
                run_id=run_id,
                branch_id=branch_id,
                type=ArtifactEventType.BUDGET_UPDATE,
                payload={"bucket": "main", "tokens_in": 10},
            )
        )
        tree_nodes = [e for e in store.effective_events(branch_id) if e.is_tree_node]
        assert [e.type for e in tree_nodes] == [TreeEventType.USER_MESSAGE.value]


class TestFork:
    def _seed(self, store: SqliteStore, run_id: str, branch_id: str) -> list:
        events = [
            _user_msg(run_id, branch_id, "step-0"),
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.AGENT_MESSAGE,
                source=Source.AGENT,
                payload={"text": "step-1"},
            ),
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.AGENT_MESSAGE,
                source=Source.AGENT,
                payload={"text": "step-2"},
            ),
        ]
        return store.append_many(events)

    def test_fork_shares_prefix_and_truncates_tail(
        self, store: SqliteStore, run_ctx: tuple[str, str]
    ) -> None:
        run_id, root = run_ctx
        events = self._seed(store, run_id, root)
        child = new_id("br")
        store.create_branch(child, run_id, parent_branch_id=root, fork_event_id=events[1].event_id)

        forked = store.effective_events(child)
        assert [e.payload.get("text") for e in forked] == ["step-0", "step-1"]
        assert [e.payload.get("text") for e in store.effective_events(root)] == [
            "step-0",
            "step-1",
            "step-2",
        ]

    def test_child_seq_continues_from_fork_point(
        self, store: SqliteStore, run_ctx: tuple[str, str]
    ) -> None:
        run_id, root = run_ctx
        events = self._seed(store, run_id, root)
        child = new_id("br")
        store.create_branch(child, run_id, parent_branch_id=root, fork_event_id=events[1].event_id)
        appended = store.append(_user_msg(run_id, child, "child-0"))
        assert appended.seq == events[1].seq + 1

    def test_fork_rejects_event_outside_parent_log(
        self, store: SqliteStore, run_ctx: tuple[str, str]
    ) -> None:
        run_id, root = run_ctx
        events = self._seed(store, run_id, root)
        with pytest.raises(StoreError, match="not in branch"):
            store.create_branch(
                new_id("br"), run_id, parent_branch_id=root, fork_event_id=events[2].event_id + "x"
            )

    def test_fork_requires_both_arguments(
        self, store: SqliteStore, run_ctx: tuple[str, str]
    ) -> None:
        run_id, root = run_ctx
        with pytest.raises(StoreError, match="requires both"):
            store.create_branch(new_id("br"), run_id, parent_branch_id=root, fork_event_id=None)

    def test_fork_of_fork_keeps_nested_prefix(
        self, store: SqliteStore, run_ctx: tuple[str, str]
    ) -> None:
        run_id, root = run_ctx
        events = self._seed(store, run_id, root)
        child = new_id("br")
        store.create_branch(child, run_id, parent_branch_id=root, fork_event_id=events[1].event_id)
        child_event = store.append(_user_msg(run_id, child, "child-0"))
        grandchild = new_id("br")
        store.create_branch(
            grandchild, run_id, parent_branch_id=child, fork_event_id=child_event.event_id
        )
        texts = [e.payload.get("text") for e in store.effective_events(grandchild)]
        assert texts == ["step-0", "step-1", "child-0"]
