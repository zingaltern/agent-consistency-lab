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

SCHEMA_VERSION = 2

DDL: str = """
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
  UNIQUE (branch_id, seq),
  FOREIGN KEY (run_id) REFERENCES runs(run_id),
  FOREIGN KEY (branch_id) REFERENCES branches(branch_id),
  FOREIGN KEY (parent_id) REFERENCES events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_events_branch_seq ON events(branch_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_parent ON events(parent_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(run_id, type);

-- append-only 由数据库强制，而非靠约定
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
  SELECT RAISE(ABORT, 'events is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
  SELECT RAISE(ABORT, 'events is append-only: DELETE is forbidden');
END;

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

# 迁移按版本号顺序执行；v1 为初始版本，后续新增写 ALTER/CREATE 语句。
# 新建库由 DDL 直接建到最新版；老库走 MIGRATIONS 补齐（两条路径必须收敛到同一 schema）。
MIGRATIONS: dict[int, str] = {
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
"""
}
