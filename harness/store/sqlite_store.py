"""SQLite 存储：run / branch / 事件日志。

并发模型（写进语义文档，也是 SQLite WAL 的硬事实）：

* 单写者假设。WAL 允许"多读一写"，但同一时刻只能有一个写事务；本类用一个
  进程内 ``threading.Lock`` 把写路径串行化，跨进程并发需要上层租约
  （W3 的 lease 事件），存储层不假装能解决。
* 每一次 append 都在 ``BEGIN IMMEDIATE`` 事务内完成"读 next_seq → 插入"，
  因此 seq 连续性不依赖调用方。

分叉（fork）不复制历史：子分支只记录 ``parent_branch_id + fork_event_id``，
有效日志 = 父分支有效日志截断到 fork 点 + 子分支自身事件。这样"共享前缀"
是结构性事实，而不是靠复制维持的一致性。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel

from ..events import Event, EventKind, NewEvent, Source
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
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    # ------------------------------------------------------------------ setup

    def setup(self) -> None:
        with self._lock:
            self._conn.executescript(DDL)
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                    (SCHEMA_VERSION, time.time()),
                )
                return
            current = int(row["value"])
            if current > SCHEMA_VERSION:
                raise StoreError(f"database schema v{current} is newer than code v{SCHEMA_VERSION}")
            for version in sorted(MIGRATIONS):
                if current < version <= SCHEMA_VERSION:
                    self._conn.executescript(MIGRATIONS[version])
                    self._conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                        (version, time.time()),
                    )
                    self._conn.execute(
                        "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                        (str(version),),
                    )

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
                self._conn.execute(
                    "INSERT INTO events(event_id, run_id, branch_id, seq, kind, type, source,"
                    " parent_id, payload_json, created_at, trace_id, span_id)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    ),
                )
                stored.append(
                    Event(
                        **new.model_dump(exclude={"kind", "source"}),
                        kind=new.kind,
                        source=new.source,
                        seq=seq,
                        created_at=created_at,
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

    def get_event(self, event_id: str) -> Event | None:
        row = self._conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return self._row_to_event(row) if row else None

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


def iter_tree_nodes(events: Iterable[Event]) -> Iterable[Event]:
    return (e for e in events if e.kind is EventKind.TREE_NODE)
