from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from harness.ids import new_id
from harness.store import SqliteStore

# 测试进程**不得**读系统代理配置。两个理由，第二个是硬的：
#
# 1. 用例只连回环地址（本地 OTLP 接收器、HTTP 接收器），本来就不该走代理；
#    在配了系统代理的机器上，`urlopen` 会把到 127.0.0.1 的请求交给代理——那是运气，不是设计。
# 2. `urllib.request.getproxies()` 在 macOS 上回落到 `_scproxy.get_proxy_settings()`
#    （SystemConfiguration）。**父进程用过一次之后再 fork 出来的子进程里调它，会 SIGSEGV**
#    （崩在 `_os_log_preferences_refresh`）。mutmut 默认用 fork 隔离变异体，且父进程
#    跑 clean tests 时已经热过一遍 ⇒ 子进程拿到 -11，被记成 `segfault`。
#    `segfault` 不计入任何计数 ⇒ **真幸存变异从变异门禁的视野里消失**。
#    调查、最小复现与反例见 `docs/design/2026-09-18-mutation-segfault-investigation.md`。
#
# 让 `getproxies_environment()` 在环境变量这一层就返回非空，`getproxies()` 便短路，
# 永远不碰 SystemConfiguration（`no_proxy=*` 同时让回环请求直连，语义上也对）。
# 必须写在 import 期：mutmut 的父进程要先跑到这里，它 fork 出的子进程才继承得到。
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"


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
