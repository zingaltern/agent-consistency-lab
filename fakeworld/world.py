"""外部世界仿真：只有一个副作用账本。

账本是整个崩溃矩阵的**唯一裁判**：runtime 的日志、checkpoint、去重表都可能
因为崩溃而不完整，但"效果到底发生了几次"只能由外部系统记录。因此账本独立于
runtime 数据库（独立文件、独立连接），并且每次真实执行都无条件追加一行。

幂等工具实现会额外维护 ``dedup`` 表：按键命中即返回既有结果、不再追加效果行。
这正是"下游是否支持幂等键"这一实验变量的物理含义。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DDL = """
CREATE TABLE IF NOT EXISTS effects (
  effect_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key TEXT,
  operation       TEXT NOT NULL,
  payload_json    TEXT NOT NULL,
  created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dedup (
  idempotency_key TEXT PRIMARY KEY,
  effect_id       INTEGER NOT NULL,
  result_json     TEXT NOT NULL,
  created_at      REAL NOT NULL
);
"""


class World:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(DDL)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> World:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def apply(
        self,
        *,
        operation: str,
        payload: dict[str, Any],
        idempotency_key: str,
        idempotent_impl: bool,
    ) -> dict[str, Any]:
        """执行一次写操作。幂等实现按键去重，非幂等实现无条件追加效果。"""
        if idempotent_impl:
            row = self._conn.execute(
                "SELECT result_json FROM dedup WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if row is not None:
                return {**json.loads(row["result_json"]), "deduplicated": True}
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._conn.execute(
                "INSERT INTO effects(idempotency_key, operation, payload_json, created_at)"
                " VALUES(?, ?, ?, ?)",
                (
                    idempotency_key,
                    operation,
                    json.dumps(payload, sort_keys=True, ensure_ascii=False),
                    time.time(),
                ),
            )
            effect_id = int(cursor.lastrowid or 0)
            result = {"operation": operation, "effect_id": effect_id, "applied": True}
            if idempotent_impl:
                self._conn.execute(
                    "INSERT INTO dedup(idempotency_key, effect_id, result_json, created_at)"
                    " VALUES(?, ?, ?, ?) ON CONFLICT(idempotency_key) DO NOTHING",
                    (
                        idempotency_key,
                        effect_id,
                        json.dumps(result, sort_keys=True, ensure_ascii=False),
                        time.time(),
                    ),
                )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return result

    # ------------------------------------------------------------------ 账本读取

    def total_effects(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM effects").fetchone()
        return int(row["n"])

    def effects_for(self, idempotency_key: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM effects WHERE idempotency_key=? ORDER BY effect_id", (idempotency_key,)
        ).fetchall()

    def effect_counts_by_key(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT idempotency_key, COUNT(*) AS n FROM effects"
            " WHERE idempotency_key IS NOT NULL GROUP BY idempotency_key"
        ).fetchall()
        return {row["idempotency_key"]: int(row["n"]) for row in rows}

    def duplicate_keys(self) -> dict[str, int]:
        return {key: n for key, n in self.effect_counts_by_key().items() if n > 1}

    def all_effects(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM effects ORDER BY effect_id").fetchall()
        return [dict(row) for row in rows]
