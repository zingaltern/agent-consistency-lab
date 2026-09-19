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

    def test_trigger_blocks_insert_or_replace_on_primary_key(
        self, store: SqliteStore, run_ctx: tuple[str, str]
    ) -> None:
        """**修复前会怎样**：`INSERT OR REPLACE` 能整行替换一条已提交事件。

        独立验证 2026-09-19（P0-1）实测：SQLite 把 REPLACE 实现成"删冲突行再插入"，
        而那个**隐式 DELETE 只在 `PRAGMA recursive_triggers=ON` 时**才触发
        `events_no_delete`（默认 OFF，本仓库不设这个 pragma）。于是只用普通 DML——
        连 DROP TRIGGER 这种 DDL 权限都不需要——就能把历史改掉，
        而 §2.1 声称"物理上禁止改写历史"。修复即第 3 条触发器：BEFORE INSERT 守卫。
        """
        run_id, branch_id = run_ctx
        event = store.append(_user_msg(run_id, branch_id, "committed truth"))
        assert store._conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 0

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._conn.execute(
                "INSERT OR REPLACE INTO events(event_id, run_id, branch_id, seq, kind, type,"
                " source, parent_id, payload_json, created_at)"
                " VALUES(?, ?, ?, 0, 'tree_node', 'user_message', 'user', NULL,"
                " '{\"text\": \"tampered in place\"}', 0.0)",
                (event.event_id, run_id, branch_id),
            )
        # 原文一字未动（不是"挡住了但已经写进去了"）
        assert [e.payload["text"] for e in store.effective_events(branch_id)] == [
            "committed truth"
        ]

    def test_trigger_blocks_insert_or_replace_on_branch_seq(
        self, store: SqliteStore, run_ctx: tuple[str, str]
    ) -> None:
        """同一漏洞的第二条路径：换一个 `event_id`、撞 `UNIQUE(branch_id, seq)`。

        **修复前会怎样**：这条会把原行整个删掉（`effective_events` 里少一条），
        比改 payload 更彻底，而且同样只需要普通 DML 权限。
        """
        run_id, branch_id = run_ctx
        event = store.append(_user_msg(run_id, branch_id, "committed truth"))

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._conn.execute(
                "INSERT OR REPLACE INTO events(event_id, run_id, branch_id, seq, kind, type,"
                " source, parent_id, payload_json, created_at)"
                " VALUES('evt_forged', ?, ?, 0, 'tree_node', 'user_message', 'user', NULL,"
                " '{\"text\": \"forged\"}', 0.0)",
                (run_id, branch_id),
            )
        events = store.effective_events(branch_id)
        assert [e.event_id for e in events] == [event.event_id]

    def test_trigger_blocks_plain_duplicate_seq(
        self, store: SqliteStore, run_ctx: tuple[str, str]
    ) -> None:
        """普通 INSERT 撞 `(branch_id, seq)` 同样被守卫拦住（不是只拦 REPLACE 关键字）。"""
        run_id, branch_id = run_ctx
        store.append(_user_msg(run_id, branch_id))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._conn.execute(
                "INSERT INTO events(event_id, run_id, branch_id, seq, kind, type, source,"
                " parent_id, payload_json, created_at)"
                " VALUES('evt_dup', ?, ?, 0, 'tree_node', 'user_message', 'user', NULL,"
                " '{}', 0.0)",
                (run_id, branch_id),
            )

    def test_trigger_allows_normal_append(self, store: SqliteStore, run_ctx) -> None:
        """守卫不能让正常写入变慢或变坏：连续 append 仍然全部落盘、seq 连续。"""
        run_id, branch_id = run_ctx
        for index in range(5):
            store.append(_user_msg(run_id, branch_id, f"m{index}"))
        events = store.effective_events(branch_id)
        assert [e.seq for e in events] == [0, 1, 2, 3, 4]


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
