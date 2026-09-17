"""事件哈希链（R-B3）：genesis / 迁移回填 / 正常链 / 断链 / 分支挂接 / mirror test。

链的用途要说清，否则会被误读成"防篡改"：append-only 触发器挡住的是**数据库层面**的
改写；链挡住的是**数据库之外**的操作——换成一个更旧的副本、绕过触发器改一行、
删掉中间一段再拼上。它不阻止篡改，只让篡改**无法静默**。

因此本文件的中心是那个 **mirror test**：复制一份库 → 篡改一行历史 → 离线校验器
必须非零退出并指出**第一个断点**。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from harness.audit_chain import audit
from harness.events import (
    GENESIS_HASH,
    Event,
    EventKind,
    NewEvent,
    Source,
    TreeEventType,
    compute_event_hash,
    record_bytes,
)
from harness.state import verify_chain
from harness.store import SqliteStore
from harness.store.schema import SCHEMA_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]

V2_DDL = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, status TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE branches (
  branch_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, parent_branch_id TEXT,
  fork_event_id TEXT, created_at REAL NOT NULL);
CREATE TABLE events (
  event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, branch_id TEXT NOT NULL,
  seq INTEGER NOT NULL, kind TEXT NOT NULL, type TEXT NOT NULL, source TEXT NOT NULL,
  parent_id TEXT, payload_json TEXT NOT NULL, created_at REAL NOT NULL,
  trace_id TEXT, span_id TEXT, UNIQUE (branch_id, seq));
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only: UPDATE is forbidden'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only: DELETE is forbidden'); END;
"""


def _seed(store: SqliteStore, *, events: int = 3) -> tuple[str, str]:
    run_id, branch_id = "run-1", "br-1"
    store.create_run(run_id, thread_id="thr-1")
    store.create_branch(branch_id, run_id)
    store.append(
        NewEvent.tree(
            run_id=run_id,
            branch_id=branch_id,
            type=TreeEventType.USER_MESSAGE,
            source=Source.USER,
            payload={"text": "任务"},
        )
    )
    for index in range(1, events):
        store.append(
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.AGENT_MESSAGE,
                source=Source.AGENT,
                payload={"text": f"第 {index} 步", "final": False},
            )
        )
    return run_id, branch_id


# ------------------------------------------------------------------ 基本链


def test_first_event_links_to_genesis(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "runtime.db")
    store.setup()
    try:
        _, branch_id = _seed(store, events=1)
        event = store.effective_events(branch_id)[0]
    finally:
        store.close()
    assert event.prev_hash == GENESIS_HASH
    assert len(event.event_hash) == 64


def test_chain_is_contiguous_and_verifiable(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "runtime.db")
    store.setup()
    try:
        _, branch_id = _seed(store, events=4)
        events = store.effective_events(branch_id)
    finally:
        store.close()
    assert [event.prev_hash for event in events[1:]] == [
        event.event_hash for event in events[:-1]
    ]
    assert verify_chain(events) == []
    assert audit(tmp_path / "runtime.db")["ok"] is True


def test_incremental_verification_accepts_a_start_point(tmp_path: Path) -> None:
    """增量校验：只查新 seq，起点由调用方给（上一批的最后一条哈希）。"""
    store = SqliteStore(tmp_path / "runtime.db")
    store.setup()
    try:
        run_id, branch_id = _seed(store, events=3)
        first_batch = store.effective_events(branch_id)
        store.append(
            NewEvent.tree(
                run_id=run_id,
                branch_id=branch_id,
                type=TreeEventType.AGENT_MESSAGE,
                source=Source.AGENT,
                payload={"text": "第 3 步", "final": False},
            )
        )
        second_batch = store.effective_events(branch_id)[len(first_batch) :]
    finally:
        store.close()
    assert verify_chain(second_batch, start_prev_hash=first_batch[-1].event_hash) == []
    # 起点给错 ⇒ 必须报断链（否则"增量校验"等于没查）
    wrong = verify_chain(second_batch, start_prev_hash="f" * 64)
    assert [violation.code for violation in wrong] == ["INV-008"]


def test_event_without_hash_is_reported_not_ignored(tmp_path: Path) -> None:
    """未迁移的库里事件没有哈希——必须显式报出来，不能当成"链是好的"。"""
    event = Event(
        event_id="e1",
        run_id="r",
        branch_id="b",
        kind="tree_node",
        type="user_message",
        source="user",
        seq=0,
    )
    violations = verify_chain([event])
    assert violations and violations[0].code == "INV-008"
    assert "没有哈希" in violations[0].detail


# ------------------------------------------------------------------ mirror test


def _copy_run_dir(source: Path, target: Path) -> Path:
    import shutil

    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return target / "runtime.db"


def _tamper(db_path: Path, *, seq: int, drop_row: bool = False) -> None:
    """绕过触发器改一行（模拟"库被换过/被改过"）：必须先 drop 触发器，这正是对手会做的事。"""
    con = sqlite3.connect(db_path)
    con.execute("DROP TRIGGER IF EXISTS events_no_update")
    con.execute("DROP TRIGGER IF EXISTS events_no_delete")
    if drop_row:
        con.execute("DELETE FROM events WHERE seq=?", (seq,))
    else:
        con.execute(
            "UPDATE events SET payload_json=? WHERE seq=?",
            (json.dumps({"text": "被人改过的历史"}, ensure_ascii=False), seq),
        )
    con.commit()
    con.close()


def test_mirror_test_detects_a_single_tampered_row(tmp_path: Path) -> None:
    """**mirror test**：复制副本 → 篡改一行历史 → 离线校验器非零退出并指出第一个断点。"""
    source = tmp_path / "source"
    source.mkdir()
    store = SqliteStore(source / "runtime.db")
    store.setup()
    try:
        _seed(store, events=6)
    finally:
        store.close()

    mirrored = _copy_run_dir(source, tmp_path / "mirror")
    _tamper(mirrored, seq=3)

    result = audit(mirrored)
    assert result["ok"] is False
    assert result["violations_total"] >= 1
    first = result["first_break"]
    assert first["code"] == "INV-008"
    assert first["seq"] == 3, "必须指出**第一个**断点的位置"
    assert "内容与哈希不符" in first["detail"]


def test_mirror_test_detects_a_deleted_row(tmp_path: Path) -> None:
    source = tmp_path / "source2"
    source.mkdir()
    store = SqliteStore(source / "runtime.db")
    store.setup()
    try:
        _seed(store, events=6)
    finally:
        store.close()
    mirrored = _copy_run_dir(source, tmp_path / "mirror2")
    _tamper(mirrored, seq=3, drop_row=True)
    result = audit(mirrored)
    assert result["ok"] is False
    assert result["first_break"]["code"] == "INV-008"


def test_cli_exit_codes_are_the_conclusion(tmp_path: Path) -> None:
    """退出码即结论：0 完好 / 1 断链 / 2 读不出来（管道会吞退出码，这里直接断言）。"""
    source = tmp_path / "source3"
    source.mkdir()
    store = SqliteStore(source / "runtime.db")
    store.setup()
    try:
        _seed(store, events=4)
    finally:
        store.close()

    def _run(db: Path) -> int:
        proc = subprocess.run(
            [sys.executable, "-m", "harness.audit_chain", "--db", str(db)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return proc.returncode

    assert _run(source / "runtime.db") == 0
    _tamper(source / "runtime.db", seq=2)
    assert _run(source / "runtime.db") == 1
    assert _run(tmp_path / "nope.db") == 2


# ------------------------------------------------------------------ 分支挂接


def test_forked_branch_links_to_the_fork_event(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "runtime.db")
    store.setup()
    try:
        run_id, branch_id = _seed(store, events=4)
        parent_events = store.effective_events(branch_id)
        fork_point = parent_events[2]
        store.create_branch("br-child", run_id, parent_branch_id=branch_id,
                            fork_event_id=fork_point.event_id)
        child_event = store.append(
            NewEvent.tree(
                run_id=run_id,
                branch_id="br-child",
                type=TreeEventType.AGENT_MESSAGE,
                source=Source.AGENT,
                payload={"text": "子分支的第一步", "final": False},
            )
        )
    finally:
        store.close()

    assert child_event.prev_hash == fork_point.event_hash, (
        "分叉分支的首条必须指向 fork 点事件的 event_hash（不是 genesis、也不是父分支末条）"
    )
    assert child_event.seq == fork_point.seq + 1
    assert audit(tmp_path / "runtime.db")["ok"] is True


def test_tampering_with_the_fork_parent_is_detected(tmp_path: Path) -> None:
    """父分支被改 ⇒ 子分支的挂接点同时失效（链按谱系展开校验的原因）。"""
    store = SqliteStore(tmp_path / "runtime.db")
    store.setup()
    try:
        run_id, branch_id = _seed(store, events=4)
        parent_events = store.effective_events(branch_id)
        store.create_branch(
            "br-child", run_id, parent_branch_id=branch_id,
            fork_event_id=parent_events[2].event_id,
        )
        store.append(
            NewEvent.tree(
                run_id=run_id, branch_id="br-child",
                type=TreeEventType.AGENT_MESSAGE, source=Source.AGENT,
                payload={"text": "子分支", "final": False},
            )
        )
    finally:
        store.close()
    _tamper(tmp_path / "runtime.db", seq=2)
    result = audit(tmp_path / "runtime.db")
    assert result["ok"] is False


# ------------------------------------------------------------------ 迁移


def test_migration_backfills_the_chain_and_recreates_triggers(tmp_path: Path) -> None:
    """v2 → v3 迁移：补链必须**事务内先 DROP 触发器再原样重建**，且重建后仍拒绝 UPDATE。"""
    db_path = tmp_path / "legacy.db"
    con = sqlite3.connect(db_path)
    con.executescript(V2_DDL)
    con.execute("INSERT INTO schema_meta(key, value) VALUES('schema_version', '2')")
    con.execute(
        "INSERT INTO runs(run_id, thread_id, status, created_at, updated_at)"
        " VALUES('run-1', 'thr-1', 'running', 0.0, 0.0)"
    )
    con.execute(
        "INSERT INTO branches(branch_id, run_id, parent_branch_id, fork_event_id, created_at)"
        " VALUES('br-1', 'run-1', NULL, NULL, 0.0)"
    )
    for index in range(3):
        con.execute(
            "INSERT INTO events(event_id, run_id, branch_id, seq, kind, type, source,"
            " parent_id, payload_json, created_at)"
            " VALUES(?, 'run-1', 'br-1', ?, 'tree_node', ?, 'agent', NULL, '{}', ?)",
            (f"e{index}", index, "agent_message" if index else "user_message", float(index)),
        )
    con.commit()
    con.close()

    store = SqliteStore(db_path)
    store.setup()  # 触发 v3 迁移
    try:
        assert store.schema_version == SCHEMA_VERSION == 3
        events = store.effective_events("br-1")
        # 补链的结果必须与"从头写一遍"一致：逐条可校验
        assert verify_chain(events) == []
        assert events[0].prev_hash == GENESIS_HASH
        # 触发器必须回来了，而且真的挡得住
        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute("UPDATE events SET payload_json='{}' WHERE event_id='e0'")
        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute("DELETE FROM events WHERE event_id='e0'")
    finally:
        store.close()
    assert audit(db_path)["ok"] is True


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """重复 setup 不会重新补链（链一旦算错就会把好库变成坏库）。"""
    db_path = tmp_path / "twice.db"
    store = SqliteStore(db_path)
    store.setup()
    try:
        _seed(store, events=3)
        before = [event.event_hash for event in store.effective_events("br-1")]
    finally:
        store.close()
    again = SqliteStore(db_path)
    again.setup()
    try:
        after = [event.event_hash for event in again.effective_events("br-1")]
    finally:
        again.close()
    assert before == after


# ------------------------------------------------------------------ 写入侧的链保护


def test_appending_on_a_broken_tail_is_refused(tmp_path: Path) -> None:
    """**P2-1 回归**：尾行没有哈希时，新事件不许静默挂到 genesis/fork 点。

    修复前会怎样：链在一个无人察觉的位置被"延长"，而这件事只有下一次离线校验才会发现；
    报错放在写入时，至少"谁在写、写什么"是清楚的。
    """
    from harness.store.sqlite_store import StoreError

    store = SqliteStore(tmp_path / "runtime.db")
    store.setup()
    try:
        run_id, branch_id = _seed(store, events=2)
        # 把尾行的哈希清空（模拟"库被外部改过"或"迁移没跑完"）
        store._conn.execute("DROP TRIGGER events_no_update")
        store._conn.execute("UPDATE events SET event_hash='' WHERE seq=1")
        store._conn.execute(
            "CREATE TRIGGER events_no_update BEFORE UPDATE ON events "
            "BEGIN SELECT RAISE(ABORT, 'events is append-only: UPDATE is forbidden'); END"
        )
        with pytest.raises(StoreError) as excinfo:
            store.append(
                NewEvent.tree(
                    run_id=run_id,
                    branch_id=branch_id,
                    type=TreeEventType.AGENT_MESSAGE,
                    source=Source.AGENT,
                    payload={"text": "接在断链上的一条"},
                )
            )
        assert "event_hash" in str(excinfo.value)
    finally:
        store.close()


def test_migration_handles_a_forked_legacy_branch(tmp_path: Path) -> None:
    """**P2-3 回归**：旧库里有分叉分支时，补链必须按谱系顺序算（父分支先算完）。

    修复前会怎样：分叉分支的首条事件要指向**fork 点事件**的哈希，
    而 fork 点的哈希只在父分支补完之后才存在——顺序错了会直接 RuntimeError。
    """
    db_path = tmp_path / "legacy-fork.db"
    con = sqlite3.connect(db_path)
    con.executescript(V2_DDL)
    con.execute("INSERT INTO schema_meta(key, value) VALUES('schema_version', '2')")
    con.execute(
        "INSERT INTO runs(run_id, thread_id, status, created_at, updated_at)"
        " VALUES('run-1', 'thr-1', 'running', 0.0, 0.0)"
    )
    con.execute(
        "INSERT INTO branches(branch_id, run_id, parent_branch_id, fork_event_id, created_at)"
        " VALUES('br-1', 'run-1', NULL, NULL, 0.0)"
    )
    for index in range(3):
        con.execute(
            "INSERT INTO events(event_id, run_id, branch_id, seq, kind, type, source,"
            " parent_id, payload_json, created_at)"
            " VALUES(?, 'run-1', 'br-1', ?, 'tree_node', ?, 'agent', NULL, '{}', ?)",
            (f"p{index}", index, "agent_message" if index else "user_message", float(index)),
        )
    # 子分支：从父分支的 seq=1 分叉出去，自己的 seq 从 2 继续
    con.execute(
        "INSERT INTO branches(branch_id, run_id, parent_branch_id, fork_event_id, created_at)"
        " VALUES('br-2', 'run-1', 'br-1', 'p1', 1.0)"
    )
    con.execute(
        "INSERT INTO events(event_id, run_id, branch_id, seq, kind, type, source,"
        " parent_id, payload_json, created_at)"
        " VALUES('c2', 'run-1', 'br-2', 2, 'tree_node', 'agent_message', 'agent', NULL, '{}', 2.0)"
    )
    con.commit()
    con.close()

    store = SqliteStore(db_path)
    store.setup()  # v2 → v3
    try:
        parent = store.effective_events("br-1")
        fork_point = next(event for event in parent if event.event_id == "p1")
        child = store.effective_events("br-2")
        child_own = [event for event in child if event.branch_id == "br-2"]
        assert child_own, "子分支应当有自己的事件"
        assert child_own[0].prev_hash == fork_point.event_hash, (
            "分叉分支首条必须挂在 fork 点事件的哈希上（迁移要按谱系顺序补链）"
        )
        assert verify_chain(child, start_prev_hash=GENESIS_HASH) == []
    finally:
        store.close()
    assert audit(db_path)["ok"] is True


# ------------------------------------------------------------------ golden vector


def test_chain_bytes_are_pinned_by_a_golden_vector() -> None:
    """把 ``record_bytes`` 与 ``event_hash`` 的**字节格式**钉死（评审 §5 建议 1）。

    为什么需要：链的字节口径是**跨语言兼容面**——第三方要验证副本，就得复刻
    `json.dumps(record, sort_keys=True, ensure_ascii=False)` 的转义与浮点规则。
    没有这条 golden vector 时，"实现悄悄换了序列化（例如加上 `separators`）"
    不会有任何测试变红，而历史链会集体失效。

    ⚠️ 变更这条向量的后果：**所有既有库的链都会变成"断链"**。要改必须先有迁移方案。
    """
    event = Event(
        event_id="evt-golden",
        run_id="run-1",
        branch_id="br-1",
        seq=7,
        kind=EventKind.TREE_NODE,
        type="tool_call",
        source=Source.AGENT,
        payload={"tool": "scale_pool", "args": {"size": 64, "中文键": "值"}, "n": 1.5},
        created_at=1700000000.25,
    )
    expected_record = (
        '{"branch_id": "br-1", "created_at": 1700000000.25, "event_id": "evt-golden",'
        ' "kind": "tree_node", "parent_id": null, "payload": {"args": {"size": 64,'
        ' "中文键": "值"}, "n": 1.5, "tool": "scale_pool"}, "run_id": "run-1",'
        ' "seq": 7, "source": "agent", "type": "tool_call"}'
    )
    assert record_bytes(event) == expected_record
    # 哈希同样钉死（值由真实实现算出后写死；改它等于宣布历史链全部失效）
    assert compute_event_hash(GENESIS_HASH, event) == (
        "23b25b8bdcfa23bc99433767a6bf510fbacc9628991f192ecf4769923979f044"
    )
