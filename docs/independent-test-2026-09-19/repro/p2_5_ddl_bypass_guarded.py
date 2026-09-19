#!/usr/bin/env python
"""P2-5 最小复现：`DROP TRIGGER` 这类 **DDL** 绕过路径，现在防得住也检得出吗？

    .venv/bin/python docs/independent-test-2026-09-19/repro/p2_5_ddl_bypass_guarded.py

**修复前的输出**（独立验证 2026-09-19 实测）：默认连接上一句
`DROP TRIGGER events_no_update` 被**接受**，随后的 `UPDATE events SET payload_json=...`
也**成功**——`semantics.md` §2.1 说的"历史改写被物理禁止"只覆盖普通 DML，
触发器本身是 schema 对象，谁都能拆。

**修复后**（三层，见 `docs/semantics.md` §2.1）：

1. 连接层（防）：`store._conn` 上装了 authorizer + `DBCONFIG_DEFENSIVE`，
   `DROP TRIGGER` / `ALTER TABLE ... RENAME` / `PRAGMA writable_schema` 全被拒；
2. 写前核查（检测）：**别的连接**拆掉触发器之后，本进程的下一次 `append` fail-closed
   （`AppendOnlyGuardError`），不会继续往一个不再只读的库里写；
3. 离线核查：`audit_chain` 的 `guard` 字段与 `verify_append_only_guard` 报出
   "防线没了"，并且计入退出码。

本脚本把三条都跑一遍，全过才退出 0。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from harness.audit_chain import audit
from harness.events import NewEvent, Source, TreeEventType
from harness.store import (
    AppendOnlyGuardError,
    SqliteStore,
    verify_append_only_guard,
)

DDL_ATTEMPTS = (
    ("DROP TRIGGER events_no_update", "DROP TRIGGER"),
    ("ALTER TABLE events RENAME TO events_old", "ALTER TABLE ... RENAME"),
    ("PRAGMA writable_schema=ON", "PRAGMA writable_schema"),
    ("UPDATE sqlite_master SET sql='x' WHERE name='events'", "直写 sqlite_master"),
)


def _seed(store: SqliteStore) -> None:
    store.create_run("r", "t")
    store.create_branch("b", "r")
    store.append(
        NewEvent.tree(
            event_id="E",
            run_id="r",
            branch_id="b",
            type=TreeEventType.USER_MESSAGE,
            source=Source.USER,
            payload={"text": "committed truth"},
        )
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "runtime.db"
        store = SqliteStore(db_path)
        store.setup()
        _seed(store)
        status = store.guard_status
        print("连接层防线:", status.describe() if status else "（没装上！）")

        # ---- 第 1 层：本连接上拆不掉
        blocked = 0
        for sql, label in DDL_ATTEMPTS:
            try:
                store._conn.execute(sql)
                print(f"  {label}: **被接受**（!! 防线不成立）")
            except sqlite3.Error as exc:
                blocked += 1
                print(f"  {label}: 被拒绝（防线生效）-> {exc}")

        # ---- 第 2 层：外部连接**只拆防线、不动内容**，下一次追加必须 fail-closed
        con = sqlite3.connect(db_path)  # 对手的连接：没有 authorizer
        con.execute("DROP TRIGGER events_no_update")
        con.execute("DROP TRIGGER events_no_insert_over_existing")
        con.commit()
        con.close()
        print("  对手用另一条连接拆掉两个触发器（不改内容：这样两条结论才分得开）")

        try:
            store.append(
                NewEvent.tree(
                    run_id="r",
                    branch_id="b",
                    type=TreeEventType.AGENT_MESSAGE,
                    source=Source.AGENT,
                    payload={"text": "接在被拆掉防线的库上"},
                )
            )
            print("  追加: **被接受**（!! 迁就了一个不再只读的库）")
            appended = True
        except AppendOnlyGuardError as exc:
            appended = False
            print(f"  追加: 拒绝（fail-closed）-> {str(exc)[:110]}…")
        store.close()

        # ---- 第 3 层：离线核查与退出码。"链完整"与"历史还只读"是两件事：
        # 这里没有改内容，所以链仍然是好的，而防线核查必须报坏。
        offline = verify_append_only_guard(db_path)
        audited = audit(db_path)
        print("  verify_append_only_guard:", offline["guard"]["missing_triggers"])
        print(
            "  audit_chain:",
            {"ok": audited["ok"], "violations_total": audited["violations_total"]},
        )
        print("  链本身完整（只拆了防线，没改历史）:", audited["violations_total"] == 0)

        # ---- 第 4 格：真的改了内容时，链也要断（防线被拆 ⇒ 改写才做得到）
        db2 = Path(tmp) / "rewritten.db"
        _seed_again = SqliteStore(db2)
        _seed_again.setup()
        _seed(_seed_again)
        _seed_again.close()
        con = sqlite3.connect(db2)
        con.execute("DROP TRIGGER events_no_update")
        con.execute("UPDATE events SET payload_json='{\"text\": \"被人改过的历史\"}' WHERE seq=0")
        con.commit()
        con.close()
        rewritten = audit(db2)
        print(
            "  改了内容的那份:",
            {"ok": rewritten["ok"], "violations_total": rewritten["violations_total"]},
        )

        failures = []
        if blocked != len(DDL_ATTEMPTS):
            failures.append(f"连接层漏了 {len(DDL_ATTEMPTS) - blocked} 条 DDL")
        if appended:
            failures.append("外部拆防线之后本进程仍然写进了事件")
        if offline["ok"] or audited["ok"]:
            failures.append("离线核查没看出防线已被拆掉")
        if audited["violations_total"] != 0:
            failures.append("本格不该动内容，链却断了（说明脚本自己在改历史）")
        if rewritten["violations_total"] == 0:
            failures.append("改了内容的那份没被判出断链")

        if failures:
            print("\n失败：" + "；".join(failures))
            return 1
        print(
            "\n通过：本连接拆不掉、外部拆掉后下一次追加拿不到通行证；"
            "离线核查报出「防线没了」而链仍完整（两件事分开记账），"
            "真的改了内容时链另有一笔坏账"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
