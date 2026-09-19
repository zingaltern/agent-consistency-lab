"""append-only 的 DDL 防线（独立验证 2026-09-19 · P2-5）：拆触发器这件事要变难，而且要能被检出。

三层各测一类，缺一层就有一种绕过活下来：

* **连接层（防）**：authorizer 挡 ``DROP TRIGGER`` / ``ALTER TABLE RENAME`` /
  ``DROP TABLE|VIEW|INDEX`` / ``sqlite_master`` 写，DEFENSIVE 让 ``writable_schema`` 失效；
* **写前核查（检测）**：触发器被**别的连接**拆掉之后，本进程的下一次追加必须 fail-closed；
* **离线核查**：``verify_append_only_guard`` 与 ``audit_chain`` 的 ``guard`` 字段
  能在事后指出"防线没了"（链可能还是完整的）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harness.audit_chain import audit
from harness.audit_chain import main as audit_main
from harness.events import NewEvent, Source, TreeEventType
from harness.store import (
    AppendOnlyGuardError,
    SqliteStore,
    assert_guard_intact,
    guard_report,
    install_connection_guard,
    verify_append_only_guard,
)
from harness.store.schema import DDL_TABLES


def _user_msg(run_id: str, branch_id: str, text: str = "hello") -> NewEvent:
    return NewEvent.tree(
        run_id=run_id,
        branch_id=branch_id,
        type=TreeEventType.USER_MESSAGE,
        source=Source.USER,
        payload={"text": text},
    )


def _seed(db_path: Path, *, events: int = 2) -> tuple[str, str]:
    """建一个已 setup 的库并写入几条事件，返回 (run_id, branch_id)。"""
    store = SqliteStore(db_path)
    store.setup()
    try:
        run_id, branch_id = "run-1", "br-1"
        store.create_run(run_id, thread_id="thr-1")
        store.create_branch(branch_id, run_id)
        for index in range(events):
            store.append(_user_msg(run_id, branch_id, f"m{index}"))
        return run_id, branch_id
    finally:
        store.close()


def _raw(db_path: Path) -> sqlite3.Connection:
    """**对手的连接**：没有防线、没有 authorizer，模拟"拿到库文件的人"。"""
    con = sqlite3.connect(db_path)
    con.isolation_level = None
    return con


# ------------------------------------------------------- 第一层：连接层（防）


def test_dropping_a_guard_trigger_is_refused_on_the_store_connection(
    store: SqliteStore, run_ctx: tuple[str, str]
) -> None:
    """**修复前会怎样**：``DROP TRIGGER events_no_update`` 一句成功，紧接着的 ``UPDATE``
    就把一行已提交历史改掉了（独立验证 2026-09-19 实测的绕过路径）。

    修复后：authorizer 直接拒绝这条 DDL，历史仍然是物理只读的。
    """
    run_id, branch_id = run_ctx
    store.append(_user_msg(run_id, branch_id))

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        store._conn.execute("DROP TRIGGER events_no_update")
    # 触发器还在 ⇒ 改写仍然被 ABORT（拒绝的是同一件事，不是"拒绝但已经拆掉了"）
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("UPDATE events SET payload_json='{}' WHERE seq=0")


def test_alter_table_rename_and_schema_writes_are_refused(store: SqliteStore) -> None:
    """``ALTER TABLE events RENAME`` 会让触发器跟着表走（新表上就没有守卫了）；
    ``PRAGMA writable_schema`` 与直接写 ``sqlite_master`` 是改 schema 文本的两条路。
    三条都必须被拒——**修复前会怎样**：任一条成功，守卫的定义就从库里"消失"了。"""
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        store._conn.execute("ALTER TABLE events RENAME TO events_old")
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("PRAGMA writable_schema=ON")
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("UPDATE sqlite_master SET sql='x' WHERE name='events'")
    assert guard_report(store._conn)["ok"] is True


@pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "setconfig"),
    reason="SQLITE_DBCONFIG_DEFENSIVE 需要 Python 3.12+（3.11 上由 authorizer 顶住这条路）",
)
def test_defensive_alone_already_kills_writable_schema(tmp_path: Path) -> None:
    """DEFENSIVE 这一层单独的作用：``PRAGMA writable_schema=ON`` 变成**静默 no-op**，
    ``sqlite_master`` 不可写。这里用一条**裸连接**验证机制本身，与 authorizer 无关。"""
    db_path = tmp_path / "plain.db"
    con = sqlite3.connect(db_path)
    con.executescript("CREATE TABLE t (x TEXT);")
    con.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    con.execute("PRAGMA writable_schema=ON")
    assert con.execute("PRAGMA writable_schema").fetchone()[0] == 0
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE sqlite_master SET sql='x' WHERE name='t'")
    con.close()


def test_install_refuses_a_connection_without_an_authorizer() -> None:
    """authorizer 是**必需**的那一层（没有它，DDL 防线只剩一句空话）。
    **修复前会怎样**：没有它也能"装成功"，于是调用方以为防线在，实际上谁都能拆触发器。"""
    with pytest.raises(AppendOnlyGuardError, match="set_authorizer"):
        install_connection_guard(object())  # type: ignore[arg-type]


def test_guard_status_reports_which_layers_are_on(store: SqliteStore) -> None:
    """防线状态要能读出「到底装上了哪几层」——`off` 与 `on` 的差别不是细节，
    3.11 上就是少一层（`DBCONFIG_DEFENSIVE` 需要 3.12+）。"""
    status = store.guard_status
    assert status is not None
    assert status.authorizer is True
    assert "authorizer=on" in status.describe()
    assert "defensive=" in status.describe()
    # 装防线时观察到的 schema cookie：将来任何 DDL 都会让它变化，
    # 因此它是"这个库在装防线之后有没有被改过 schema"的廉价旁证
    assert status.schema_version == store._conn.execute("PRAGMA schema_version").fetchone()[0]


def test_normal_writes_are_not_slowed_or_blocked(store: SqliteStore, run_ctx) -> None:
    """正对照：装防线不能把正常写入弄坏（DDL 白名单里必须留着 CREATE TRIGGER，
    否则第二次 ``setup()`` 的 ``CREATE TRIGGER IF NOT EXISTS`` 会被拒）。"""
    run_id, branch_id = run_ctx
    for index in range(5):
        store.append(_user_msg(run_id, branch_id, f"m{index}"))
    store.setup()  # 幂等重跑
    assert [e.seq for e in store.effective_events(branch_id)] == [0, 1, 2, 3, 4]
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


# --------------------------------------------------- 第二层：写前核查（检测）


def test_preplanted_trigger_with_the_right_name_is_rejected(tmp_path: Path) -> None:
    """**修复前会怎样**：DDL 用的是 ``CREATE TRIGGER IF NOT EXISTS``，名字撞上就**静默跳过**。
    对手只要预先放一个"名字对、内容是空壳"的触发器，``setup()`` 就以为它是好的，
    库看起来迁移完毕，而历史已经不设防。核查比对的是**定义文本**，不是名字。

    这条是"逐字比对"而不是"存在性检查"的直接理由。
    """
    db_path = tmp_path / "preplanted.db"
    con = _raw(db_path)
    con.executescript(DDL_TABLES)  # 表结构完全规范，唯一的问题是那条空壳触发器
    con.execute(
        "CREATE TRIGGER events_no_insert_over_existing BEFORE INSERT ON events"
        " BEGIN SELECT 1; END"
    )
    con.close()

    store = SqliteStore(db_path)
    try:
        with pytest.raises(AppendOnlyGuardError, match="events_no_insert_over_existing"):
            store.setup()
    finally:
        store.close()


def test_appending_after_an_external_drop_fails_closed(tmp_path: Path) -> None:
    """**核心用例**：防线装好之后被**别的连接**拆掉，本进程的下一次追加必须拒绝。

    修复前会怎样：``append`` 照常成功，事件被追加进一个"历史已经不只读"的库，
    而没有任何一处报错——承诺在下一次离线审计之前都是假的。
    """
    db_path = tmp_path / "drifted.db"
    run_id, branch_id = _seed(db_path, events=2)

    store = SqliteStore(db_path)
    store.setup()  # 防线装在这条连接上
    try:
        # 对手用另一条连接拆防线（我们的 authorizer 管不到别人的连接）
        con = _raw(db_path)
        con.execute("DROP TRIGGER events_no_update")
        con.execute("DROP TRIGGER events_no_insert_over_existing")
        con.execute("UPDATE events SET payload_json='{\"text\": \"被人改过的历史\"}' WHERE seq=0")
        con.close()

        with pytest.raises(AppendOnlyGuardError) as excinfo:
            store.append(_user_msg(run_id, branch_id, "接在被拆掉防线的库上"))
        assert "events_no_update" in str(excinfo.value)
        assert "events_no_insert_over_existing" in str(excinfo.value)
        # 拒绝的是"继续写"，不是"写坏了"：日志里不多不少还是那两条
        assert [e.seq for e in store.effective_events(branch_id)] == [0, 1]
    finally:
        store.close()


def test_setup_on_a_drifted_database_refuses_by_default(tmp_path: Path) -> None:
    """版本已是最新却防线不全 ⇒ ``setup()`` **默认不顺手修**。

    修复前会怎样：``CREATE TRIGGER IF NOT EXISTS`` 会把丢掉的触发器补回来，于是
    "这个库曾经不只读"变回静默——而挪走触发器的人正是要这个。要修必须显式写
    ``allow_repair=True``，把"我知道它被动过"留在调用点上。
    """
    db_path = tmp_path / "drifted-setup.db"
    _seed(db_path, events=1)
    con = _raw(db_path)
    con.execute("DROP TRIGGER events_no_update")
    con.close()

    store = SqliteStore(db_path)
    try:
        with pytest.raises(AppendOnlyGuardError, match="events_no_update"):
            store.setup()
        # 显式逃生口：知道自己在做什么，才允许重建
        store.setup(allow_repair=True)
        assert guard_report(store._conn)["ok"] is True
    finally:
        store.close()


def test_changed_table_structure_is_reported(tmp_path: Path) -> None:
    """结构指纹的作用：对手把 ``events`` 重建成"没有 UNIQUE(branch_id, seq)"的版本
    （触发器文本可以照抄），第三层 INSERT 守卫的冲突判定就失去依据了。
    **修复前会怎样**：只看触发器在不在，这种"结构被换掉"的库看起来完全正常。"""
    db_path = tmp_path / "restructured.db"
    _seed(db_path, events=1)
    con = _raw(db_path)
    con.executescript(
        "ALTER TABLE events RENAME TO events_old;"
        "CREATE TABLE events (event_id TEXT, run_id TEXT, branch_id TEXT, seq INTEGER,"
        " kind TEXT, type TEXT, source TEXT, parent_id TEXT, payload_json TEXT,"
        " created_at REAL, trace_id TEXT, span_id TEXT,"
        " prev_hash TEXT NOT NULL DEFAULT '', event_hash TEXT NOT NULL DEFAULT '');"
    )
    report = guard_report(con)
    con.close()
    assert report["ok"] is False
    assert report["table_ok"] is False  # 表在，只是结构不对
    assert any("结构与规范 DDL 不一致" in problem for problem in report["problems"])


def test_unexpected_trigger_on_events_is_reported(tmp_path: Path) -> None:
    """多出来的触发器改不了三条守卫的语义（守卫是 ABORT），但它是"有人在动这个库"的
    信号——登记它比放过它便宜。"""
    db_path = tmp_path / "extra-trigger.db"
    _seed(db_path, events=1)
    con = _raw(db_path)
    con.execute(
        "CREATE TRIGGER events_audit_notes AFTER INSERT ON events BEGIN SELECT 1; END"
    )
    report = guard_report(con)
    con.close()
    assert report["unexpected_triggers"] == ["events_audit_notes"]
    assert report["ok"] is False


def test_a_legitimately_migrated_table_is_not_a_false_positive(tmp_path: Path) -> None:
    """合法迁移**不能**被误报：v2→v3 的补链列是 ``ALTER TABLE ... ADD COLUMN`` 加的，
    SQLite 会把新列定义写进原文本尾部，于是老库的表 DDL 文本与新建库**必然**不同。
    所以表这一项比的是结构指纹（列 / NOT NULL / 主键 / 表级 UNIQUE），不是文本。"""
    db_path = tmp_path / "legacy-v2.db"
    con = _raw(db_path)
    con.executescript(
        """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE runs (run_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, status TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE branches (branch_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, parent_branch_id TEXT,
  fork_event_id TEXT, created_at REAL NOT NULL);
CREATE TABLE events (event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, branch_id TEXT NOT NULL,
  seq INTEGER NOT NULL, kind TEXT NOT NULL, type TEXT NOT NULL, source TEXT NOT NULL,
  parent_id TEXT, payload_json TEXT NOT NULL, created_at REAL NOT NULL, trace_id TEXT,
  span_id TEXT, UNIQUE (branch_id, seq));
INSERT INTO schema_meta(key, value) VALUES('schema_version', '2');
"""
    )
    con.close()

    store = SqliteStore(db_path)
    try:
        store.setup()
        assert guard_report(store._conn)["ok"] is True
    finally:
        store.close()


# --------------------------------------------------------- 第三层：离线核查


def test_verify_append_only_guard_on_a_healthy_database(tmp_path: Path) -> None:
    db_path = tmp_path / "healthy.db"
    _seed(db_path, events=2)
    result = verify_append_only_guard(db_path)
    assert result["ok"] is True
    assert result["guard"]["missing_triggers"] == []
    assert result["guard"]["problems"] == []


def test_verify_append_only_guard_detects_a_missing_trigger(tmp_path: Path) -> None:
    """离线核查必须能看见"防线没了"，即使链是完整的——两件事都要问。"""
    db_path = tmp_path / "offline-drift.db"
    _seed(db_path, events=2)
    assert audit(db_path)["ok"] is True

    con = _raw(db_path)
    con.execute("DROP TRIGGER events_no_delete")
    con.close()

    result = verify_append_only_guard(db_path)
    assert result["ok"] is False
    assert result["guard"]["missing_triggers"] == ["events_no_delete"]
    assert audit(db_path)["ok"] is False, "链没断，但库已经不符合 append-only 的前提"


def test_verify_append_only_guard_snapshots_the_wal(tmp_path: Path) -> None:
    """快照纪律：只拷主库会读到旧状态（改动还只在 ``-wal`` 里）。
    **修复前会怎样**：核查读到"没被改过"的那一份，报 ok，结论整个反过来。"""
    db_path = tmp_path / "wal.db"
    _seed(db_path, events=2)
    con = _raw(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("DROP TRIGGER events_no_delete")
    # 连接**不关**：关了会 checkpoint，改动就落进主库、这条用例也就测不到快照纪律了
    try:
        assert Path(str(db_path) + "-wal").exists()
        result = verify_append_only_guard(db_path)
        assert result["ok"] is False
        assert result["guard"]["missing_triggers"] == ["events_no_delete"]
    finally:
        con.close()


def test_audit_chain_reports_the_guard_and_its_exit_code(tmp_path: Path, capsys) -> None:
    """退出码即结论：链完整 + 防线被拆 ⇒ 仍然是 1，并且理由能一眼读到。"""
    db_path = tmp_path / "cli.db"
    _seed(db_path, events=2)
    assert audit_main(["--db", str(db_path)]) == 0

    con = _raw(db_path)
    con.execute("DROP TRIGGER events_no_insert_over_existing")
    con.close()

    result = audit(db_path)
    assert result["ok"] is False
    assert result["violations_total"] == 0, "链本身没断"
    assert result["first_break"] is None
    assert result["guard"]["missing_triggers"] == ["events_no_insert_over_existing"]

    code = audit_main(["--db", str(db_path)])
    captured = capsys.readouterr()
    assert code == 1
    assert "append-only 防线不完整" in captured.err
    assert "events_no_insert_over_existing" in captured.err


def test_assert_guard_intact_passes_on_a_healthy_database(store: SqliteStore) -> None:
    """不变量断言的正对照：健康的库必须**报告**通过，而不是"恰好没抛异常"。"""
    report = assert_guard_intact(store._conn)
    assert report["ok"] is True
    assert report["missing_triggers"] == []
    assert report["altered_triggers"] == []
    assert report["unexpected_triggers"] == []
    assert report["expected_triggers"] == sorted(
        ["events_no_update", "events_no_delete", "events_no_insert_over_existing"]
    )
