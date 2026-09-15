"""工具调用记录：outbox 意图行（pending → executed / failed / unknown）。

``idempotency_key`` 上的 UNIQUE 约束是运行时去重的唯一依据。自 W3 起，非幂等写
会先 ``begin()`` 预写一行 ``pending`` 意图，执行成功再 ``complete()`` 闭合；
崩溃留下的 ``pending`` 行是"效果可能已发生"的唯一线索，恢复时必须先探针对账
（``mark_unknown`` 是探针不可用/不确定时的落点），绝不能直接重跑。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from pydantic import BaseModel

from .sqlite_store import SqliteStore


class ToolCallRecord(BaseModel):
    tool_call_id: str
    run_id: str
    branch_id: str
    tool: str
    args_json: str
    args_sha256: str
    idempotency_key: str
    effect: str
    status: str  # executed | failed | unknown
    result: dict[str, Any] | None = None
    error_class: str | None = None
    started_at: float
    ended_at: float | None = None


class ToolCallStore:
    def __init__(self, store: SqliteStore) -> None:
        self._store = store
        self._conn = store._conn

    def lookup(self, idempotency_key: str) -> ToolCallRecord | None:
        row = self._conn.execute(
            "SELECT * FROM tool_calls WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def get(self, tool_call_id: str) -> ToolCallRecord | None:
        row = self._conn.execute(
            "SELECT * FROM tool_calls WHERE tool_call_id=?", (tool_call_id,)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def begin(
        self,
        *,
        tool_call_id: str,
        run_id: str,
        branch_id: str,
        tool: str,
        args: dict[str, Any],
        args_sha256: str,
        idempotency_key: str,
        effect: str,
    ) -> bool:
        """outbox 预写意图：在任何副作用之前落一行 status='pending'。

        返回 True 表示首次尝试；False 表示键已存在（重放或并发），调用方必须按既有
        状态处置，绝不能直接执行。
        """
        return self.record(
            tool_call_id=tool_call_id,
            run_id=run_id,
            branch_id=branch_id,
            tool=tool,
            args=args,
            args_sha256=args_sha256,
            idempotency_key=idempotency_key,
            effect=effect,
            status="pending",
            result=None,
        )

    def complete(self, idempotency_key: str, result: dict[str, Any] | None) -> None:
        """执行成功后闭合意图行。"""
        with self._store.write_tx():
            self._conn.execute(
                "UPDATE tool_calls SET status='executed', result_json=?, ended_at=?"
                " WHERE idempotency_key=?",
                (
                    None
                    if result is None
                    else json.dumps(result, sort_keys=True, ensure_ascii=False),
                    time.time(),
                    idempotency_key,
                ),
            )

    def record(
        self,
        *,
        tool_call_id: str,
        run_id: str,
        branch_id: str,
        tool: str,
        args: dict[str, Any],
        args_sha256: str,
        idempotency_key: str,
        effect: str,
        status: str,
        result: dict[str, Any] | None,
        error_class: str | None = None,
        started_at: float | None = None,
    ) -> bool:
        """写入记录；键已存在时返回 False（表示这次调用不需要真正执行）。"""
        now = time.time()
        with self._store.write_tx():
            cursor = self._conn.execute(
                "INSERT INTO tool_calls(tool_call_id, run_id, branch_id, tool, args_json,"
                " args_sha256, idempotency_key, effect, status, result_json, error_class,"
                " started_at, ended_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    tool_call_id,
                    run_id,
                    branch_id,
                    tool,
                    json.dumps(args, sort_keys=True, ensure_ascii=False),
                    args_sha256,
                    idempotency_key,
                    effect,
                    status,
                    None
                    if result is None
                    else json.dumps(result, sort_keys=True, ensure_ascii=False),
                    error_class,
                    started_at if started_at is not None else now,
                    now,
                ),
            )
        return cursor.rowcount == 1

    def mark_unknown(self, idempotency_key: str, reason: str) -> None:
        """把一条处于 pending 的记录标记为 unknown（W3 的对账入口）。"""
        with self._store.write_tx():
            self._conn.execute(
                "UPDATE tool_calls SET status='unknown', error_class=?, ended_at=?"
                " WHERE idempotency_key=?",
                (reason, time.time(), idempotency_key),
            )

    def count(self, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM tool_calls WHERE run_id=?", (run_id,)
        ).fetchone()
        return int(row["n"])

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ToolCallRecord:
        return ToolCallRecord(
            tool_call_id=row["tool_call_id"],
            run_id=row["run_id"],
            branch_id=row["branch_id"],
            tool=row["tool"],
            args_json=row["args_json"],
            args_sha256=row["args_sha256"],
            idempotency_key=row["idempotency_key"],
            effect=row["effect"],
            status=row["status"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error_class=row["error_class"],
            started_at=float(row["started_at"]),
            ended_at=float(row["ended_at"]) if row["ended_at"] is not None else None,
        )
