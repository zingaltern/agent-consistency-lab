"""SQLite 存储：run / branch / 事件日志。

并发模型（写进语义文档，也是 SQLite WAL 的硬事实）：

* 单写者假设。WAL 允许"多读一写"，但同一时刻只能有一个写事务；本类用一个
  进程内 ``threading.Lock`` 把写路径串行化，跨进程并发需要上层租约
  （W3 的 lease 事件），存储层不假装能解决。
* 每一次 append 都在 ``BEGIN IMMEDIATE`` 事务内完成"读 next_seq → 插入"，
  因此 seq 连续性不依赖调用方。
* 历史只读有三层：三条 SQLite 触发器（DML 层，``schema.py``）、本连接的 DDL 防线
  （``guard.py``：authorizer + DEFENSIVE），以及**写前的逐字核查**
  （``append_many`` 在事务里跑 ``assert_guard_intact``）——防线被拆掉之后，
  下一次追加拿不到"照常写"的通行证。

分叉（fork）不复制历史：子分支只记录 ``parent_branch_id + fork_event_id``，
有效日志 = 父分支有效日志截断到 fork 点 + 子分支自身事件。这样"共享前缀"
是结构性事实，而不是靠复制维持的一致性。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel

from ..events import GENESIS_HASH, Event, EventKind, NewEvent, Source, compute_event_hash
from .guard import (
    AppendOnlyGuardError,
    GuardStatus,
    assert_guard_intact,
    guard_report,
    install_connection_guard,
)
from .schema import DDL, MIGRATIONS, SCHEMA_VERSION


class RunRow(BaseModel):
    run_id: str
    thread_id: str
    status: str
    created_at: float
    updated_at: float


class BranchRow(BaseModel):
    branch_id: str
    run_id: str
    parent_branch_id: str | None
    fork_event_id: str | None
    created_at: float


class StoreError(RuntimeError):
    pass


class SqliteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._guard: GuardStatus | None = None
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    # ------------------------------------------------------------------ setup

    def setup(self, *, allow_repair: bool = False) -> None:
        """建库/迁移/装防线。

        ``allow_repair``（默认 False）只影响一种情况：库记录的版本**已经是最新**、
        却已经没有完整的 append-only 防线。那不是「待跑的迁移」能解释的差异——默认
        拒绝并报错，因为顺手修好会把「这个库曾经不只读」变回静默；调用方要修就得
        显式写 ``setup(allow_repair=True)``，把「我知道它被动过」留在代码里。
        """
        with self._lock:
            recorded = self._recorded_schema_version_locked()
            if recorded == SCHEMA_VERSION and not allow_repair:
                self._refuse_undeclared_drift_locked(recorded)
            self._conn.executescript(DDL)
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                # 全新库：DDL 已建到最新版，迁移历史按 1..SCHEMA_VERSION 补齐登记
                self._conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                for version in range(1, SCHEMA_VERSION + 1):
                    self._conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (version, time.time()),
                    )
            else:
                current = int(row["value"])
                if current > SCHEMA_VERSION:
                    raise StoreError(
                        f"database schema v{current} is newer than code v{SCHEMA_VERSION}"
                    )
                for version in sorted(MIGRATIONS):
                    if current < version <= SCHEMA_VERSION:
                        migration = MIGRATIONS[version]
                        if callable(migration):
                            # 事务内执行：补链失败就整体回滚，绝不留下"触发器没了"的半成品
                            with self._tx():
                                migration(self._conn)
                        else:
                            self._conn.executescript(migration)
                        self._conn.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                            (version, time.time()),
                        )
                        self._conn.execute(
                            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                            (str(version),),
                        )
            self._install_guard_locked()

    def _recorded_schema_version_locked(self) -> int | None:
        """库自己登记的 schema 版本；返 None 表示"没有登记"（全新库，或 DDL 只建了一半）。"""
        try:
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError:
            return None
        return int(row["value"]) if row is not None else None

    def _refuse_undeclared_drift_locked(self, recorded: int) -> None:
        """版本已是最新却防线不全：这不是"待跑的迁移"，是"库被改过"，默认拒绝。"""
        report = guard_report(self._conn)
        if report["ok"]:
            return
        raise AppendOnlyGuardError(
            f"库记录为 schema v{recorded}（当前代码 v{SCHEMA_VERSION}），没有任何迁移待跑，"
            "但 append-only 防线已经不完整：" + "；".join(report["problems"])
            + "。默认**不顺手修**——一修就把「这个库曾经不只读」变回静默，"
            "而挪走触发器的人正是要这个。若你确认这是有意为之（例如本地手工调过 schema），"
            "写 `SqliteStore(path).setup(allow_repair=True)` 显式重建，"
            "并对这段时间里写过的内容当作可能已被改写处理。"
        )

    def _install_guard_locked(self) -> GuardStatus:
        """DDL/迁移之后装防线：先核查（挡住"预先放一个同名空触发器"的库），再装连接层。

        顺序有讲究——v3 迁移要临时 ``DROP TRIGGER`` 再重建，authorizer 装上之后
        那一步会被拒；所以这里必须是 ``setup()`` 的最后一步。
        """
        assert_guard_intact(self._conn)
        self._guard = install_connection_guard(self._conn)
        return self._guard

    @property
    def guard_status(self) -> GuardStatus | None:
        """连接层防线的实际状态；``setup()`` 之前为 None。"""
        return self._guard

    @property
    def schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteStore:
        self.setup()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -------------------------------------------------------------------- runs

    def create_run(self, run_id: str, thread_id: str, status: str = "running") -> RunRow:
        now = time.time()
        with self._lock, self._tx():
            self._conn.execute(
                "INSERT INTO runs(run_id, thread_id, status, created_at, updated_at)"
                " VALUES(?, ?, ?, ?, ?)",
                (run_id, thread_id, status, now, now),
            )
        return RunRow(
            run_id=run_id, thread_id=thread_id, status=status, created_at=now, updated_at=now
        )

    def get_run(self, run_id: str) -> RunRow | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return RunRow(**dict(row)) if row else None

    def set_run_status(self, run_id: str, status: str) -> None:
        with self._lock, self._tx():
            cursor = self._conn.execute(
                "UPDATE runs SET status=?, updated_at=? WHERE run_id=?",
                (status, time.time(), run_id),
            )
            if cursor.rowcount == 0:
                raise StoreError(f"unknown run: {run_id}")

    # ---------------------------------------------------------------- branches

    def create_branch(
        self,
        branch_id: str,
        run_id: str,
        parent_branch_id: str | None = None,
        fork_event_id: str | None = None,
    ) -> BranchRow:
        if (parent_branch_id is None) != (fork_event_id is None):
            raise StoreError("fork requires both parent_branch_id and fork_event_id")
        if parent_branch_id is not None:
            parent_log = self.effective_events(parent_branch_id)
            if not any(e.event_id == fork_event_id for e in parent_log):
                raise StoreError(
                    f"fork_event_id {fork_event_id!r} is not in branch {parent_branch_id!r}"
                )
        now = time.time()
        with self._lock, self._tx():
            self._conn.execute(
                "INSERT INTO branches(branch_id, run_id, parent_branch_id,"
                " fork_event_id, created_at)"
                " VALUES(?, ?, ?, ?, ?)",
                (branch_id, run_id, parent_branch_id, fork_event_id, now),
            )
        return BranchRow(
            branch_id=branch_id,
            run_id=run_id,
            parent_branch_id=parent_branch_id,
            fork_event_id=fork_event_id,
            created_at=now,
        )

    def get_branch(self, branch_id: str) -> BranchRow | None:
        row = self._conn.execute(
            "SELECT * FROM branches WHERE branch_id=?", (branch_id,)
        ).fetchone()
        return BranchRow(**dict(row)) if row else None

    def lineage(self, branch_id: str) -> list[BranchRow]:
        """从根分支到给定分支的链路。"""
        chain: list[BranchRow] = []
        seen: set[str] = set()
        current = self.get_branch(branch_id)
        while current is not None:
            if current.branch_id in seen:
                raise StoreError(f"branch cycle detected at {current.branch_id!r}")
            seen.add(current.branch_id)
            chain.append(current)
            if current.parent_branch_id is None:
                break
            current = self.get_branch(current.parent_branch_id)
        if not chain:
            raise StoreError(f"unknown branch: {branch_id}")
        chain.reverse()
        return chain

    # ------------------------------------------------------------------ events

    def append(self, new: NewEvent) -> Event:
        return self.append_many([new])[0]

    def append_many(self, news: Sequence[NewEvent]) -> list[Event]:
        if not news:
            return []
        stored: list[Event] = []
        with self._lock, self._tx():
            # 写前核查（P2-5）：触发器或表结构被外部改过 ⇒ 这个库的"历史只读"已经不成立，
            # 此时继续追加等于往一个不再可信的日志里写。fail-closed 比"写进去再说"诚实。
            assert_guard_intact(self._conn)
            if self._guard is None:
                # 没跑过 setup()（或 setup() 之前就被写过）：连接层防线此刻补装
                self._guard = install_connection_guard(self._conn)
            for new in news:
                seq = self._next_seq_locked(new.branch_id, new.run_id)
                if new.parent_id is not None:
                    parent = self._conn.execute(
                        "SELECT branch_id FROM events WHERE event_id=?", (new.parent_id,)
                    ).fetchone()
                    if parent is None:
                        raise StoreError(f"unknown parent event: {new.parent_id}")
                    if parent["branch_id"] != new.branch_id:
                        raise StoreError("parent event belongs to a different branch")
                created_at = time.time()
                payload_json = json.dumps(new.payload, sort_keys=True, ensure_ascii=False)
                event = Event(
                    **new.model_dump(exclude={"kind", "source"}),
                    kind=new.kind,
                    source=new.source,
                    seq=seq,
                    created_at=created_at,
                )
                prev_hash = self._prev_hash_locked(event)
                event_hash = compute_event_hash(prev_hash, event)
                self._conn.execute(
                    "INSERT INTO events(event_id, run_id, branch_id, seq, kind, type, source,"
                    " parent_id, payload_json, created_at, trace_id, span_id, prev_hash,"
                    " event_hash)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new.event_id,
                        new.run_id,
                        new.branch_id,
                        seq,
                        new.kind.value,
                        new.type,
                        new.source.value,
                        new.parent_id,
                        payload_json,
                        created_at,
                        new.trace_id,
                        new.span_id,
                        prev_hash,
                        event_hash,
                    ),
                )
                stored.append(
                    event.model_copy(
                        update={"prev_hash": prev_hash, "event_hash": event_hash}
                    )
                )
        return stored

    def _next_seq_locked(self, branch_id: str, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(seq) AS max_seq FROM events WHERE branch_id=?", (branch_id,)
        ).fetchone()
        if row["max_seq"] is not None:
            return int(row["max_seq"]) + 1
        branch = self.get_branch(branch_id)
        if branch is None:
            raise StoreError(f"unknown branch: {branch_id}")
        if branch.run_id != run_id:
            raise StoreError(f"run {run_id!r} does not own branch {branch_id!r}")
        if branch.parent_branch_id is None:
            return 0
        parent_events = self.effective_events(branch.parent_branch_id)
        for event in parent_events:
            if event.event_id == branch.fork_event_id:
                return event.seq + 1
        raise StoreError(f"fork point {branch.fork_event_id!r} missing from parent branch")

    def _prev_hash_locked(self, event: Event) -> str:
        """链的挂接规则（与 semantics.md 一致）：

        * 本分支已有事件 ⇒ 上一条事件的 ``event_hash``；
        * 本分支首条 + 无父分支 ⇒ ``GENESIS_HASH``；
        * 本分支首条 + 有父分支（分叉）⇒ **fork 点事件**的 ``event_hash``。
        """
        row = self._conn.execute(
            "SELECT event_hash FROM events WHERE branch_id=? ORDER BY seq DESC LIMIT 1",
            (event.branch_id,),
        ).fetchone()
        if row is not None:
            tail_hash = str(row["event_hash"] or "")
            if not tail_hash:
                # 评审 P2-1：尾行哈希为空时**静默**挂到 genesis/fork 点，等于在一条断链上
                # 继续追加，而这件事只有下一次离线校验才会被发现。放在写入时报错，
                # 把发现时机提前到"谁在写、写什么"都还清楚的时候。
                raise StoreError(
                    f"分支 {event.branch_id!r} 的最后一条事件没有 event_hash"
                    "（库未迁移到 v3，或这一行被外部改过）：拒绝在当前链上追加。"
                    "先跑一次 store.setup() 触发迁移，或确认库的来源。"
                )
            return tail_hash
        branch = self.get_branch(event.branch_id)
        if branch is not None and branch.parent_branch_id is not None:
            fork = self._conn.execute(
                "SELECT event_hash FROM events WHERE event_id=?", (branch.fork_event_id,)
            ).fetchone()
            if fork is not None and fork["event_hash"]:
                return str(fork["event_hash"])
        return GENESIS_HASH

    def next_seq(self, branch_id: str) -> int:
        branch = self.get_branch(branch_id)
        if branch is None:
            raise StoreError(f"unknown branch: {branch_id}")
        return self._next_seq_locked(branch_id, branch.run_id)

    def own_events(self, branch_id: str, after_seq: int = -1) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE branch_id=? AND seq>? ORDER BY seq",
            (branch_id, after_seq),
        ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def effective_events(self, branch_id: str) -> list[Event]:
        """分支的有效日志：祖先前缀截断到 fork 点 + 本分支事件。"""
        out: list[Event] = []
        for index, branch in enumerate(self.lineage(branch_id)):
            if index == 0:
                out = self.own_events(branch.branch_id)
                continue
            if branch.fork_event_id is None:
                raise StoreError(f"branch {branch.branch_id!r} has no fork point")
            cut = next((i for i, e in enumerate(out) if e.event_id == branch.fork_event_id), None)
            if cut is None:
                raise StoreError(
                    f"fork point {branch.fork_event_id!r} not in ancestor log of {branch_id!r}"
                )
            out = out[: cut + 1] + self.own_events(branch.branch_id)
        return out

    def head(self, branch_id: str) -> Event | None:
        events = self.effective_events(branch_id)
        return events[-1] if events else None

    def last_seq(self, branch_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(seq) AS max_seq FROM events WHERE branch_id=?", (branch_id,)
        ).fetchone()
        if row is None or row["max_seq"] is None:
            return -1
        return int(row["max_seq"])

    def latest_branch_for_thread(self, thread_id: str) -> BranchRow | None:
        row = self._conn.execute(
            "SELECT b.* FROM branches b JOIN runs r ON b.run_id = r.run_id"
            " WHERE r.thread_id=? ORDER BY b.created_at DESC, b.rowid DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        return BranchRow(**dict(row)) if row else None

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            event_id=row["event_id"],
            run_id=row["run_id"],
            branch_id=row["branch_id"],
            kind=EventKind(row["kind"]),
            type=row["type"],
            source=Source(row["source"]),
            payload=json.loads(row["payload_json"]),
            parent_id=row["parent_id"],
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            seq=int(row["seq"]),
            created_at=float(row["created_at"]),
            # 链列是 v3 新增的：老快照/老库读出来可能是 NULL，这里统一成空串。
            # 必须用 row.keys()：sqlite3.Row 的迭代产出的是**值**，`in` 判的不是列名。
            prev_hash=(
                str(row["prev_hash"] or "")
                if "prev_hash" in row.keys()  # noqa: SIM118 - sqlite3.Row 例外
                else ""
            ),
            event_hash=(
                str(row["event_hash"] or "")
                if "event_hash" in row.keys()  # noqa: SIM118 - sqlite3.Row 例外
                else ""
            ),
        )

    # ------------------------------------------------------------------ helper

    @contextmanager
    def write_tx(self) -> Iterator[None]:
        """单写者互斥 + BEGIN IMMEDIATE；跨行写入必须走这里。"""
        with self._lock, self._tx():
            yield

    def _tx(self) -> _ImmediateTransaction:
        return _ImmediateTransaction(self._conn)


class _ImmediateTransaction:
    """BEGIN IMMEDIATE，确保"读 next_seq → 插入"期间持有写锁。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: object, *_: object) -> None:
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")
