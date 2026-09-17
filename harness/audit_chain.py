"""离线链校验 CLI：把"日志没有被删改"变成一条任何人可跑的命令（R-B3）。

```bash
.venv/bin/python -m harness.audit_chain --run-dir /tmp/demo
.venv/bin/python -m harness.audit_chain --db /tmp/demo/runtime.db --json-out /tmp/chain.json
```

三条纪律：

1. **读库连 `-wal`/`-shm` 一起快照**（外部审计实测的坑：SIGKILL 之后已提交的事件可能
   还在 WAL 里，只拷主库会读到旧状态，把"链是好的"或"链断了"判反）；
2. **报第一个断点**并给出定位信息（分支 / seq / 事件 id），不打印一堆无用的"全部正常"；
3. **退出码即结论**：0 = 链完整；1 = 发现断链或内容与哈希不符；2 = 库读不出来。

链的挂接规则（`harness/store/sqlite_store.py::_prev_hash_locked` 与 semantics.md 一致）：
同分支上一条的 `event_hash`；分支首条指向 genesis（无父分支）或 **fork 点事件**的
`event_hash`（有父分支）。因此校验必须按"分支 + 谱系"展开，而不是把全库事件拍平排序。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from .events import GENESIS_HASH, Event, EventKind, Source
from .state import verify_chain
from .store.snapshot import snapshot_db

# 快照纪律的唯一实现在 harness/store/snapshot.py（P2-17：三份实现会漂移）
_snapshot = snapshot_db


def _load_events(conn: sqlite3.Connection) -> list[Event]:
    rows = conn.execute(
        "SELECT event_id, run_id, branch_id, seq, kind, type, source, parent_id,"
        " payload_json, created_at, prev_hash, event_hash FROM events"
    ).fetchall()
    return [
        Event(
            event_id=row[0],
            run_id=row[1],
            branch_id=row[2],
            seq=int(row[3]),
            kind=EventKind(row[4]),
            type=row[5],
            source=Source(row[6]),
            parent_id=row[7],
            payload=json.loads(row[8]),
            created_at=float(row[9]),
            prev_hash=str(row[10] or ""),
            event_hash=str(row[11] or ""),
        )
        for row in rows
    ]


def audit(db_path: Path) -> dict[str, Any]:
    """全量校验：按分支谱系顺序展开，逐分支调用 ``verify_chain``。"""
    if not db_path.exists():
        return {"ok": False, "error": f"库不存在：{db_path}", "violations": []}
    snapshot = _snapshot(db_path)
    try:
        conn = sqlite3.connect(snapshot)
        conn.row_factory = sqlite3.Row
        events = _load_events(conn)
        branches = {
            row["branch_id"]: dict(row)
            for row in conn.execute(
                "SELECT branch_id, parent_branch_id, fork_event_id FROM branches"
            ).fetchall()
        }
    except sqlite3.DatabaseError as exc:
        message = f"库无法读取：{type(exc).__name__}: {exc}"
        if "no such column: prev_hash" in str(exc) or "no such column: event_hash" in str(exc):
            # 评审 P2-18：v2 库上的报错原样透出 SQLite 信息，读者不知道下一步该做什么
            message += (
                "\n  这是 **schema v2 的库**（没有链列）：先跑一次 `SqliteStore(path).setup()`"
                " 触发 v3 迁移（补链并重建 append-only 触发器），再重新校验。"
            )
        return {"ok": False, "error": message, "violations": []}
    finally:
        with suppress(NameError, UnboundLocalError):  # 连接都没建起来时不必关闭
            conn.close()

    by_branch: dict[str, list[Event]] = {}
    for event in events:
        by_branch.setdefault(event.branch_id, []).append(event)
    for items in by_branch.values():
        items.sort(key=lambda item: item.seq)

    hashes = {event.event_id: event.event_hash for event in events}
    depth: dict[str, int] = {}

    def _depth(branch_id: str, seen: frozenset[str] = frozenset()) -> int:
        if branch_id in depth:
            return depth[branch_id]
        if branch_id in seen:
            return 99  # 环：交给校验把它当断链报出来
        parent = (branches.get(branch_id) or {}).get("parent_branch_id")
        value = 0 if parent is None else _depth(parent, seen | {branch_id}) + 1
        depth[branch_id] = value
        return value

    violations: list[dict[str, Any]] = []
    checked = 0
    for branch_id in sorted(by_branch, key=_depth):
        items = by_branch[branch_id]
        parent = (branches.get(branch_id) or {}).get("parent_branch_id")
        fork_event_id = (branches.get(branch_id) or {}).get("fork_event_id")
        if parent is None:
            start_prev = GENESIS_HASH
        else:
            start_prev = hashes.get(str(fork_event_id), "")
            if not start_prev:
                violations.append(
                    {
                        "code": "INV-008",
                        "branch_id": branch_id,
                        "detail": f"分叉分支的 fork 点事件 {fork_event_id} 不存在或没有哈希",
                    }
                )
                start_prev = None
        checked += len(items)
        for violation in verify_chain(items, start_prev_hash=start_prev):
            violations.append(
                {
                    "code": violation.code,
                    "branch_id": branch_id,
                    "seq": next(
                        (
                            event.seq
                            for event in items
                            if violation.event_id is not None
                            and event.event_id == violation.event_id
                        ),
                        None,
                    ),
                    "event_id": violation.event_id,
                    "detail": violation.detail,
                }
            )
    return {
        "ok": not violations,
        "db": str(db_path),
        "events_checked": checked,
        "branches_checked": len(by_branch),
        "violations": violations[:20],
        "violations_total": len(violations),
        "first_break": violations[0] if violations else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness.audit_chain")
    parser.add_argument("--run-dir", default="", help="run 目录（会自动找 runtime.db）")
    parser.add_argument("--db", default="", help="直接指定 runtime.db")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)

    if args.db:
        db_path = Path(args.db)
    elif args.run_dir:
        db_path = Path(args.run_dir) / "runtime.db"
    else:
        print("需要 --run-dir 或 --db 之一")
        return 2

    result = audit(db_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if "error" in result:
        return 2
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
