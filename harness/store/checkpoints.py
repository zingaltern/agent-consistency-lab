"""Checkpoint 与 writes 的提交协议。

协议（这是本项目的核心资产之一，对照 LangGraph 语义并显式化）：

1. **writes 先于 checkpoint**。一个 task（= 一次节点执行）完成后立即把它的输出
   写入 ``checkpoint_writes``；此时 checkpoint 尚未提交。这样兄弟 task 失败不会
   让已完成的工作重跑。
2. **checkpoint 只在 super-step 边界提交**，且是"整批快照"：它记录该边界上的
   全量 channel 值。崩溃发生在边界之前 ⇒ 该 super-step 未被承认，但已完成
   task 的 writes 仍可被重放（不重跑副作用），这正是 writes 与 checkpoint
   分开存储的原因。
3. **恢复 = 最新完整 checkpoint + 重放其后的 writes + 从未完成 task 继续**。
   注意：checkpoint 里的状态本身不含外部副作用；"不重复副作用"由工具侧
   幂等 / outbox 保证（见 W3），本模块只保证"日志与状态不重不漏"。
4. **metadata 必须原样保留未知键**：跨版本读取时，本版本不认识的 metadata
   键不得丢弃（否则 minor 升级会静默丢字段）。运行时字段统一注入到
   ``_runtime`` 命名空间下，不与调用方键冲突。

保留的 writes 索引（负数）用于控制类写入，普通输出使用 >= 0 的 idx。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from enum import IntEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..ids import new_id
from .sqlite_store import SqliteStore, StoreError

CHECKPOINT_PAYLOAD_VERSION = 1


class WriteIdx(IntEnum):
    """负数索引为控制类写入保留（对齐 LangGraph 的 WRITES_IDX_MAP 语义）。"""

    ERROR = -1
    SCHEDULED = -2
    INTERRUPT = -3
    RESUME = -4


class Checkpoint(BaseModel):
    v: int = CHECKPOINT_PAYLOAD_VERSION
    id: str
    branch_id: str
    parent_checkpoint_id: str | None = None
    created_at: float
    channel_values: dict[str, Any] = Field(default_factory=dict)
    updated_channels: list[str] = Field(default_factory=list)


class Write(BaseModel):
    task_id: str
    idx: int
    channel: str
    payload: Any = None
    task_path: str = ""


class CheckpointTuple(BaseModel):
    checkpoint: Checkpoint
    metadata: dict[str, Any] = Field(default_factory=dict)
    pending_writes: list[Write] = Field(default_factory=list)


TaskOutcome = Literal["completed", "error", "interrupted", "scheduled"]


class RecoveryPlan(BaseModel):
    """恢复计划：W2 的崩溃注入实验直接对该结构断言。"""

    checkpoint: Checkpoint | None
    task_outcomes: dict[str, TaskOutcome]
    replay_writes: list[Write]
    resume_from_seq: int
    notes: str = ""


class SqliteCheckpointSaver:
    def __init__(self, store: SqliteStore) -> None:
        self._store = store
        self._conn = store._conn  # 同一连接：checkpoint 与事件日志共享事务边界

    # ---------------------------------------------------------------- commits

    def put(
        self,
        *,
        thread_id: str,
        branch_id: str,
        channel_values: dict[str, Any],
        source: str,
        parent_checkpoint_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        checkpoint_id: str | None = None,
        checkpoint_ns: str = "",
        step: int | None = None,
    ) -> Checkpoint:
        """在 super-step 边界提交一次全量快照。"""
        checkpoint = Checkpoint(
            id=checkpoint_id or new_id("ckpt"),
            branch_id=branch_id,
            parent_checkpoint_id=parent_checkpoint_id,
            created_at=time.time(),
            channel_values=channel_values,
            updated_channels=sorted(channel_values),
        )
        runtime_meta = {
            "schema_version": CHECKPOINT_PAYLOAD_VERSION,
            "source": source,
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "branch_id": branch_id,
        }
        if step is not None:
            runtime_meta["step"] = step
        stored_meta = dict(metadata or {})
        stored_meta["_runtime"] = runtime_meta
        with self._store.write_tx():
            self._conn.execute(
                "INSERT INTO checkpoints(thread_id, checkpoint_ns, checkpoint_id,"
                " parent_checkpoint_id, branch_id, state_json, metadata_json, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    checkpoint_ns,
                    checkpoint.id,
                    checkpoint.parent_checkpoint_id,
                    branch_id,
                    json.dumps(channel_values, sort_keys=True, ensure_ascii=False),
                    json.dumps(stored_meta, sort_keys=True, ensure_ascii=False),
                    checkpoint.created_at,
                ),
            )
        return checkpoint

    def put_writes(
        self,
        *,
        thread_id: str,
        checkpoint_id: str,
        writes: Sequence[Write],
        checkpoint_ns: str = "",
    ) -> None:
        """幂等 upsert：同一 (checkpoint_id, task_id, idx) 重复写入不产生新行。"""
        if not writes:
            return
        now = time.time()
        with self._store.write_tx():
            for write in writes:
                self._conn.execute(
                    "INSERT INTO checkpoint_writes(thread_id, checkpoint_ns, checkpoint_id,"
                    " task_id, task_path, idx, channel, payload_json, created_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)"
                    " DO UPDATE SET channel=excluded.channel,"
                    " payload_json=excluded.payload_json, task_path=excluded.task_path",
                    (
                        thread_id,
                        checkpoint_ns,
                        checkpoint_id,
                        write.task_id,
                        write.task_path,
                        write.idx,
                        write.channel,
                        json.dumps(write.payload, sort_keys=True, ensure_ascii=False),
                        now,
                    ),
                )

    # ------------------------------------------------------------------ reads

    def get(
        self, thread_id: str, checkpoint_id: str, checkpoint_ns: str = ""
    ) -> CheckpointTuple | None:
        row = self._conn.execute(
            "SELECT * FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?",
            (thread_id, checkpoint_ns, checkpoint_id),
        ).fetchone()
        if row is None:
            return None
        return self._tuple_from_row(row, checkpoint_ns)

    def get_latest(self, thread_id: str, checkpoint_ns: str = "") -> CheckpointTuple | None:
        row = self._conn.execute(
            "SELECT * FROM checkpoints WHERE thread_id=? AND checkpoint_ns=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (thread_id, checkpoint_ns),
        ).fetchone()
        if row is None:
            return None
        return self._tuple_from_row(row, checkpoint_ns)

    def list_checkpoints(
        self, thread_id: str, limit: int = 20, checkpoint_ns: str = ""
    ) -> list[CheckpointTuple]:
        rows = self._conn.execute(
            "SELECT * FROM checkpoints WHERE thread_id=? AND checkpoint_ns=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (thread_id, checkpoint_ns, limit),
        ).fetchall()
        return [self._tuple_from_row(row, checkpoint_ns) for row in rows]

    # --------------------------------------------------------------- recovery

    def recovery_plan(
        self, thread_id: str, checkpoint_id: str | None = None, checkpoint_ns: str = ""
    ) -> RecoveryPlan:
        """恢复算法：最新完整 checkpoint → 重放其后 writes → 未完成 task 继续。"""
        if checkpoint_id is None:
            latest = self.get_latest(thread_id, checkpoint_ns)
        else:
            latest = self.get(thread_id, checkpoint_id, checkpoint_ns)
        if latest is None:
            branch = self._store.latest_branch_for_thread(thread_id)
            resume_seq = 0 if branch is None else self._store.last_seq(branch.branch_id) + 1
            return RecoveryPlan(
                checkpoint=None,
                task_outcomes={},
                replay_writes=[],
                resume_from_seq=resume_seq,
                notes="no checkpoint committed yet; replay from the event log head",
            )
        outcomes = classify_writes(latest.pending_writes)
        replay = [w for w in latest.pending_writes if outcomes.get(w.task_id) == "completed"]
        resume_seq = self._store.last_seq(latest.checkpoint.branch_id) + 1
        return RecoveryPlan(
            checkpoint=latest.checkpoint,
            task_outcomes=outcomes,
            replay_writes=replay,
            resume_from_seq=resume_seq,
            notes=(
                "completed tasks are replayed from writes (side effects must NOT re-run); "
                "incomplete tasks restart from scratch"
            ),
        )

    def _tuple_from_row(self, row: sqlite3.Row, checkpoint_ns: str) -> CheckpointTuple:
        metadata = json.loads(row["metadata_json"])
        if not isinstance(metadata, dict):
            raise StoreError("checkpoint metadata must be a JSON object")
        channel_values = json.loads(row["state_json"])
        writes = self._conn.execute(
            "SELECT * FROM checkpoint_writes WHERE thread_id=? AND checkpoint_ns=?"
            " AND checkpoint_id=? ORDER BY task_id, idx",
            (row["thread_id"], checkpoint_ns, row["checkpoint_id"]),
        ).fetchall()
        return CheckpointTuple(
            checkpoint=Checkpoint(
                v=CHECKPOINT_PAYLOAD_VERSION,
                id=row["checkpoint_id"],
                branch_id=row["branch_id"],
                parent_checkpoint_id=row["parent_checkpoint_id"],
                created_at=float(row["created_at"]),
                channel_values=channel_values,
                updated_channels=sorted(channel_values),
            ),
            metadata=metadata,
            pending_writes=[
                Write(
                    task_id=w["task_id"],
                    idx=int(w["idx"]),
                    channel=w["channel"],
                    payload=json.loads(w["payload_json"]),
                    task_path=w["task_path"],
                )
                for w in writes
            ],
        )


def classify_writes(writes: Sequence[Write]) -> dict[str, TaskOutcome]:
    """按保留索引把 task 归类；一个 task 同时有多个索引时按严重度取一。"""
    outcomes: dict[str, TaskOutcome] = {}
    for write in writes:
        if write.idx == WriteIdx.ERROR:
            candidate: TaskOutcome = "error"
        elif write.idx == WriteIdx.INTERRUPT:
            candidate = "interrupted"
        elif write.idx == WriteIdx.SCHEDULED:
            candidate = "scheduled"
        else:
            candidate = "completed"
        current = outcomes.get(write.task_id)
        if current is None or _SEVERITY[candidate] < _SEVERITY[current]:
            outcomes[write.task_id] = candidate
    return outcomes


_SEVERITY: dict[TaskOutcome, int] = {"error": 0, "interrupted": 1, "scheduled": 2, "completed": 3}
