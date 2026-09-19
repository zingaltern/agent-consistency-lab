#!/usr/bin/env python
"""P0-1 最小复现：`INSERT OR REPLACE` 能不能绕过 append-only？

    .venv/bin/python docs/independent-test-2026-09-19/repro/p0_1_append_only_bypass.py

**修复前（schema v3 及以前）的输出**：UPDATE 与 DELETE 被触发器拒绝，
但 `INSERT OR REPLACE` 撞主键那条**成功了**，payload 被原地换掉、行数不变——
只用普通 DML，连 DDL 权限都不需要。原因：SQLite 把 REPLACE 实现成"删冲突行再插入"，
而那个**隐式 DELETE 只在 `PRAGMA recursive_triggers=ON` 时才触发** `BEFORE DELETE`
（默认 OFF，runtime 也不设它）。

**修复后（schema v4）**：第三条触发器 `events_no_insert_over_existing`
（`BEFORE INSERT`，撞 `event_id` 或撞 `UNIQUE(branch_id, seq)` 即 ABORT）把它拦住，
本脚本退出 0。两条路径（主键 / (branch_id, seq)）都测。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from harness.events import NewEvent, Source, TreeEventType
from harness.store import SqliteStore

UPDATE_SQL = "UPDATE events SET payload_json='{\"text\":\"tampered\"}' WHERE event_id='E'"
INSERT_SQL = (
    "INSERT OR REPLACE INTO events(event_id, run_id, branch_id, seq, kind, type, source,"
    " parent_id, payload_json, created_at) VALUES(?, ?, ?, 0, 'tree_node', 'user_message',"
    " 'user', NULL, ?, 0.0)"
)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteStore(str(Path(tmp) / "runtime.db"))
        store.setup()
        store.create_run("r", "t")
        store.create_branch("b", "r")
        event = store.append(
            NewEvent.tree(
                event_id="E",
                run_id="r",
                branch_id="b",
                type=TreeEventType.USER_MESSAGE,
                source=Source.USER,
                payload={"text": "committed truth"},
            )
        )
        print("recursive_triggers（runtime 连接上的实际取值）:",
              store._conn.execute("PRAGMA recursive_triggers").fetchone()[0])

        for label, sql in (
            ("UPDATE", UPDATE_SQL),
            ("DELETE", "DELETE FROM events WHERE event_id='E'"),
        ):
            try:
                store._conn.execute(sql)
                print(f"  {label}: 被接受（!! 承诺不成立）")
                return 1
            except sqlite3.Error as exc:
                print(f"  {label}: 被拒绝（触发器生效）-> {exc}")

        failures: list[str] = []
        for label, args in (
            ("撞主键 event_id", ("E", "r", "b", '{"text":"tampered in place"}')),
            ("撞 UNIQUE(branch_id, seq)", ("E2", "r", "b", '{"text":"forged"}')),
        ):
            try:
                store._conn.execute(INSERT_SQL, args)
                print(f"  INSERT OR REPLACE（{label}）: 被接受（!! 承诺不成立）")
                failures.append(label)
            except sqlite3.Error as exc:
                print(f"  INSERT OR REPLACE（{label}）: 被拒绝（守卫生效）-> {exc}")

        texts = [e.payload["text"] for e in store.effective_events("b")]
        ids = [e.event_id for e in store.effective_events("b")]
        print("  原文一字未动:", texts, ids)
        store.close()

        if failures or texts != ["committed truth"] or ids != [event.event_id]:
            print("\n失败：append-only 仍可被普通 DML 绕过")
            return 1
        print("\n通过：三条触发器把 UPDATE / DELETE / INSERT-over-existing 全挡住")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
