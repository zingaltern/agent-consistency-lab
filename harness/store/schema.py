"""存储层 DDL 与 schema 版本管理。

两个不可协商的约束：

* **events 表物理只读历史**：UPDATE / DELETE 由触发器直接 ABORT。任何"修改历史"
  的实现都必须退化为"追加新事件 + 派生视图"。
* **WAL + synchronous=NORMAL**：kill -9 不丢已提交事务（OS page cache 仍在），
  但掉电可能丢失最后一个事务。崩溃实验的结论只对 kill -9 语义成立，这一点
  写进 docs/semantics.md，不允许含糊。

checkpoint 相关表沿用 LangGraph 的字段命名（thread_id / checkpoint_ns /
checkpoint_id / task_id / idx），目的是让"自研 vs LangGraph"的对照测试可以
共用同一套断言与恢复算法描述。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

SCHEMA_VERSION = 4

# 触发器语句：DDL 与迁移共用同一份文本（迁移里手抄一份迟早漂移）。
# 拆成**逐条语句**而不是 executescript：sqlite3 的 executescript 会先隐式 COMMIT，
# 在事务里调用它会把"补链事务"提前提交——那样迁移就不再是原子的了。
TRIGGER_STATEMENTS: tuple[str, ...] = (
    """
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
  SELECT RAISE(ABORT, 'events is append-only: UPDATE is forbidden');
END;
""",
    """
CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
  SELECT RAISE(ABORT, 'events is append-only: DELETE is forbidden');
END;
""",
    # v4：堵 `INSERT OR REPLACE`。SQLite 把 REPLACE 实现成"删冲突行再插入"，而那个**隐式
    # DELETE 只在 `PRAGMA recursive_triggers=ON` 时才触发 BEFORE DELETE**
    # （默认 OFF，本项目不设该 pragma）——于是"改写一行已提交历史"可以只用普通 DML 完成，
    # 不需要任何 DDL 权限（独立验证 2026-09-19 · P0-1 实测）。
    # 守卫必须在 INSERT 侧：REPLACE 的冲突判定发生在插入之前，只有不变量式的
    # "不许往已存在的 (event_id) / (branch_id, seq) 上插"能一次覆盖 PK 与 UNIQUE 两条冲突路径。
    """
CREATE TRIGGER IF NOT EXISTS events_no_insert_over_existing
BEFORE INSERT ON events
WHEN EXISTS (
  SELECT 1 FROM events
  WHERE event_id = NEW.event_id
     OR (branch_id = NEW.branch_id AND seq = NEW.seq)
)
BEGIN
  SELECT RAISE(ABORT,
    'events is append-only: INSERT over an existing event is forbidden');
END;
""",
)

DDL_TRIGGERS = "\n".join(TRIGGER_STATEMENTS)

DDL_TABLES: str = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  run_id     TEXT PRIMARY KEY,
  thread_id  TEXT NOT NULL,
  status     TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS branches (
  branch_id        TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL,
  parent_branch_id TEXT,
  fork_event_id    TEXT,
  created_at       REAL NOT NULL,
  FOREIGN KEY (run_id) REFERENCES runs(run_id),
  FOREIGN KEY (parent_branch_id) REFERENCES branches(branch_id)
);

CREATE TABLE IF NOT EXISTS events (
  event_id     TEXT PRIMARY KEY,
  run_id       TEXT NOT NULL,
  branch_id    TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  kind         TEXT NOT NULL,
  type         TEXT NOT NULL,
  source       TEXT NOT NULL,
  parent_id    TEXT,
  payload_json TEXT NOT NULL,
  created_at   REAL NOT NULL,
  trace_id     TEXT,
  span_id      TEXT,
  -- v3（R-B3）：事件哈希链。event_hash = sha256(prev_hash ‖ record_bytes)，
  -- 起点用常量 GENESIS_HASH；分叉分支首条指向上游 fork 点事件的 event_hash。
  prev_hash    TEXT NOT NULL DEFAULT '',
  event_hash   TEXT NOT NULL DEFAULT '',
  UNIQUE (branch_id, seq),
  FOREIGN KEY (run_id) REFERENCES runs(run_id),
  FOREIGN KEY (branch_id) REFERENCES branches(branch_id),
  FOREIGN KEY (parent_id) REFERENCES events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_events_branch_seq ON events(branch_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_parent ON events(parent_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(run_id, type);

CREATE TABLE IF NOT EXISTS checkpoints (
  thread_id            TEXT NOT NULL,
  checkpoint_ns        TEXT NOT NULL DEFAULT '',
  checkpoint_id        TEXT NOT NULL,
  parent_checkpoint_id TEXT,
  branch_id            TEXT NOT NULL,
  state_json           TEXT NOT NULL,
  metadata_json        TEXT NOT NULL,
  created_at           REAL NOT NULL,
  PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_branch
  ON checkpoints(branch_id, created_at);

CREATE TABLE IF NOT EXISTS checkpoint_writes (
  thread_id       TEXT NOT NULL,
  checkpoint_ns   TEXT NOT NULL DEFAULT '',
  checkpoint_id   TEXT NOT NULL,
  task_id         TEXT NOT NULL,
  task_path       TEXT NOT NULL DEFAULT '',
  idx             INTEGER NOT NULL,
  channel         TEXT NOT NULL,
  payload_json    TEXT NOT NULL,
  created_at      REAL NOT NULL,
  PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at REAL NOT NULL
);

-- v2：工具调用的幂等记录。idempotency_key = hash(run_id, tool_call_id)，
-- 刻意不含参数：参数 hash 只作旁路校验（改参检测），不参与键（见 semantics.md §4）。
CREATE TABLE IF NOT EXISTS tool_calls (
  tool_call_id    TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  tool            TEXT NOT NULL,
  args_json       TEXT NOT NULL,
  args_sha256     TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  effect          TEXT NOT NULL,
  status          TEXT NOT NULL,
  result_json     TEXT,
  error_class     TEXT,
  started_at      REAL NOT NULL,
  ended_at        REAL,
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id, started_at);
"""

# 触发器文本只在 TRIGGER_STATEMENTS 里存一份：新建库（DDL）与迁移都引用它，
# 两条路径因此不可能漂移。放在末尾而不是 events 表定义中途——executescript 顺序执行，
# 触发器只引用 events，位置不影响语义。
DDL: str = DDL_TABLES + "\n" + DDL_TRIGGERS

# 迁移按版本号顺序执行；v1 为初始版本，后续新增写 ALTER/CREATE 语句。
# 新建库由 DDL 直接建到最新版；老库走 MIGRATIONS 补齐（两条路径必须收敛到同一 schema）。
#
# v3 之后迁移可以是**可调用对象**：SQLite 里算不了 sha256，补链必须在 Python 侧做。
# 无论哪一种，迁移都在事务里执行（由 sqlite_store.setup 保证）。
Migration = str | Callable[[sqlite3.Connection], None]

MIGRATIONS: dict[int, object] = {
    2: """
CREATE TABLE IF NOT EXISTS tool_calls (
  tool_call_id    TEXT PRIMARY KEY,
  run_id          TEXT NOT NULL,
  branch_id       TEXT NOT NULL,
  tool            TEXT NOT NULL,
  args_json       TEXT NOT NULL,
  args_sha256     TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  effect          TEXT NOT NULL,
  status          TEXT NOT NULL,
  result_json     TEXT,
  error_class     TEXT,
  started_at      REAL NOT NULL,
  ended_at        REAL,
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id, started_at);
""",
}


def _migrate_v3_add_hash_chain(conn: sqlite3.Connection) -> None:
    """v2 → v3：给 events 补 ``prev_hash`` / ``event_hash`` 并回填整条链。

    **关键坑**（对抗审查第 14 条）：``events`` 上的 append-only 触发器对任何 UPDATE 都
    ``RAISE(ABORT)``，而回填必须 UPDATE。正确做法是"事务内先 DROP TRIGGER → 补链 →
    原样重建触发器"：

    * 这不是绕过边界纪律：事件的 payload 与排序**一字未动**，只是补两个新列的哈希值；
    * 一旦补链中途失败，整个事务回滚——不会留下"触发器没了"的半成品库；
    * 迁移自测断言"迁移后触发器存在且 UPDATE / DELETE 仍被拒"
      （`tests/test_hash_chain.py::test_migration_backfills_the_chain_and_recreates_triggers`）。

    回填顺序：按 branch 的谱系深度排序（父分支先算），因为分叉分支的首条事件要指向
    **fork 点事件**的 ``event_hash``。
    """
    import json as _json

    from harness.events import GENESIS_HASH, Event, compute_event_hash

    conn.execute("DROP TRIGGER IF EXISTS events_no_update")
    conn.execute("DROP TRIGGER IF EXISTS events_no_delete")
    conn.execute("ALTER TABLE events ADD COLUMN prev_hash TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE events ADD COLUMN event_hash TEXT NOT NULL DEFAULT ''")

    branches = conn.execute(
        "SELECT branch_id, parent_branch_id, fork_event_id FROM branches"
    ).fetchall()
    depth: dict[str, int] = {}

    def _depth(branch_id: str, seen: frozenset[str] = frozenset()) -> int:
        if branch_id in depth:
            return depth[branch_id]
        if branch_id in seen:
            raise RuntimeError(f"分支谱系出现环：{branch_id}")
        row = next((item for item in branches if item[0] == branch_id), None)
        if row is None or row[1] is None:
            depth[branch_id] = 0
            return 0
        value = _depth(row[1], seen | {branch_id}) + 1
        depth[branch_id] = value
        return value

    hashes: dict[str, str] = {}
    for row in sorted(branches, key=lambda item: _depth(item[0])):
        branch_id, parent_branch_id, fork_event_id = row[0], row[1], row[2]
        prev_hash = GENESIS_HASH
        if parent_branch_id is not None:
            if fork_event_id not in hashes:
                raise RuntimeError(f"fork 点 {fork_event_id} 的事件哈希尚未计算")
            prev_hash = hashes[fork_event_id]
        events = conn.execute(
            "SELECT event_id, run_id, branch_id, seq, kind, type, source, parent_id,"
            " payload_json, created_at FROM events WHERE branch_id=? ORDER BY seq",
            (branch_id,),
        ).fetchall()
        for event_row in events:
            event = Event(
                event_id=event_row[0],
                run_id=event_row[1],
                branch_id=event_row[2],
                seq=int(event_row[3]),
                kind=event_row[4],
                type=event_row[5],
                source=event_row[6],
                parent_id=event_row[7],
                payload=_json.loads(event_row[8]),
                created_at=float(event_row[9]),
            )
            event_hash = compute_event_hash(prev_hash, event)
            conn.execute(
                "UPDATE events SET prev_hash=?, event_hash=? WHERE event_id=?",
                (prev_hash, event_hash, event.event_id),
            )
            hashes[event.event_id] = event_hash
            prev_hash = event_hash

    for statement in TRIGGER_STATEMENTS:
        conn.execute(statement)


# 注册在函数定义之后：dict 字面量在 def 之前，提前引用会 NameError
MIGRATIONS[3] = _migrate_v3_add_hash_chain


def _migrate_v4_append_only_insert_guard(conn: sqlite3.Connection) -> None:
    """v3 → v4：补上 ``BEFORE INSERT`` 守卫（堵 `INSERT OR REPLACE` 改写已提交历史）。

    为什么这是**扩大**而不是重新定义 §2.1：v3 及以前只有 UPDATE / DELETE 两条触发器，
    而 SQLite 的 REPLACE 在默认 pragma 下绕过 BEFORE DELETE（见 ``TRIGGER_STATEMENTS``
    里第三条的注释与 ``tests/test_event_log.py`` 的回归用例）。存量库缺的正是这条，
    因此迁移只做"补齐触发器"这一件事——payload、排序、哈希链一字未动，
    也不需要像 v3 那样临时 DROP 触发器（没有 UPDATE 回填）。幂等：
    全部语句都是 ``CREATE TRIGGER IF NOT EXISTS``，重复执行不会失败。
    """
    for statement in TRIGGER_STATEMENTS:
        conn.execute(statement)


MIGRATIONS[4] = _migrate_v4_append_only_insert_guard
