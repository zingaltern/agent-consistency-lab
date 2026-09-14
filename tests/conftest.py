from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from harness.ids import new_id
from harness.store import SqliteStore


class RunCtx(NamedTuple):
    """一次 run 的三种身份：run（执行）/ branch（日志分支）/ thread（会话持久身份）。"""

    run_id: str
    branch_id: str
    thread_id: str


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    store = SqliteStore(tmp_path / "lab.db")
    store.setup()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture()
def ctx(store: SqliteStore) -> RunCtx:
    run_id = new_id("run")
    thread_id = new_id("thr")
    branch_id = new_id("br")
    store.create_run(run_id, thread_id=thread_id)
    store.create_branch(branch_id, run_id)
    return RunCtx(run_id=run_id, branch_id=branch_id, thread_id=thread_id)


@pytest.fixture()
def run_ctx(ctx: RunCtx) -> tuple[str, str]:
    """事件日志测试只关心 (run_id, branch_id)。"""
    return ctx.run_id, ctx.branch_id
