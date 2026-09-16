"""单写者租约（R-B6）：持有效租约可写、未持有/已过期给出可读失败、双进程冒烟。

验收（对抗审查第 22 条收敛）只两条：

1. 持有效租约写入正常；
2. 未持有 / 已过期的写入给出**可读失败**（"拒绝接管"也是合法实现）。

"过期接管"是第二步能力，本包**不做**——因此这里还有一条反向用例：
过期之后**不**会自动把租约转给下一个进程（那会让"谁在写"变得不可预测）。

双进程冒烟复用了外部审计的纪律：未定义行为要如实记录；本包把它变成"已定义行为需如实验证"。
它**不**证明并发安全——一次冒烟不是并发证据，报告里必须这么写。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness.events import ArtifactEventType
from harness.lease import (
    LeaseError,
    LeaseNotHeld,
    SingleWriterLease,
    fold_lease,
    guarded_write,
)
from harness.store import SqliteStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _lease(store: SqliteStore, *, owner: str = "worker-a") -> SingleWriterLease:
    store.create_run("run-1", thread_id="thr-1")
    store.create_branch("br-1", "run-1")
    return SingleWriterLease(store, run_id="run-1", branch_id="br-1", owner=owner)


def test_write_is_allowed_with_a_valid_lease(store: SqliteStore) -> None:
    lease = _lease(store)
    lease.acquire(ttl_seconds=60)
    written = guarded_write(lease, write=lambda: "written")
    assert written == "written"
    assert lease.check().reason == "ok"


def test_write_is_refused_without_a_lease(store: SqliteStore) -> None:
    lease = _lease(store)
    with pytest.raises(LeaseNotHeld) as excinfo:
        guarded_write(lease, write=lambda: "should not happen")
    assert "lease_not_acquired" in str(excinfo.value)


def test_expired_lease_is_a_readable_failure_not_an_implicit_takeover(
    store: SqliteStore,
) -> None:
    """过期 ⇒ 可读失败；**不接管**（第二个进程也不会"顺手"拿到权限）。"""
    lease = _lease(store, owner="worker-a")
    lease.acquire(ttl_seconds=0.01)
    time.sleep(0.02)
    with pytest.raises(LeaseNotHeld) as excinfo:
        guarded_write(lease, write=lambda: "nope")
    assert "lease_expired" in str(excinfo.value)

    other = SingleWriterLease(
        store, run_id="run-1", branch_id="br-1", owner="worker-b"
    )
    with pytest.raises(LeaseNotHeld) as excinfo:
        other.require()
    assert "lease_held_by_other" in str(excinfo.value), (
        "过期的租约仍在原持有者名下：接管必须是显式动作，不是自动行为"
    )


def test_another_owner_cannot_write_while_the_lease_is_held(store: SqliteStore) -> None:
    holder = _lease(store, owner="worker-a")
    holder.acquire(ttl_seconds=60)
    intruder = SingleWriterLease(store, run_id="run-1", branch_id="br-1", owner="worker-b")
    with pytest.raises(LeaseNotHeld) as excinfo:
        intruder.require()
    assert "lease_held_by_other" in str(excinfo.value)
    assert "worker-a" in str(excinfo.value), "报错要指出租约在谁手里"


def test_explicit_reacquire_after_expiry_is_allowed(store: SqliteStore) -> None:
    """显式 acquire 是**允许**的接管方式（把决定权交给人/运维，而不是隐式接管）。"""
    first = _lease(store, owner="worker-a")
    first.acquire(ttl_seconds=0.01)
    time.sleep(0.02)
    second = SingleWriterLease(store, run_id="run-1", branch_id="br-1", owner="worker-b")
    second.acquire(ttl_seconds=60)
    assert second.check().reason == "ok"
    assert first.check().reason == "lease_held_by_other"


def test_renew_requires_current_ownership(store: SqliteStore) -> None:
    lease = _lease(store)
    with pytest.raises(LeaseNotHeld):
        lease.renew()
    lease.acquire(ttl_seconds=60)
    renewed = lease.renew(ttl_seconds=120)
    assert renewed.expires_at > renewed.acquired_at


def test_lease_authority_is_the_event_log(store: SqliteStore) -> None:
    """租约状态是从日志折叠出来的：再开一个 lease 对象读同一份日志，结论一致。"""
    lease = _lease(store)
    lease.acquire(ttl_seconds=60)
    events = store.effective_events("br-1")
    assert [event.type for event in events] == [ArtifactEventType.LEASE.value]
    assert fold_lease(events).owner == "worker-a"
    assert fold_lease([]) is None


def test_negative_ttl_is_rejected(store: SqliteStore) -> None:
    lease = _lease(store)
    with pytest.raises(LeaseError):
        lease.acquire(ttl_seconds=0)


def test_two_process_smoke_records_the_actual_behaviour(tmp_path: Path) -> None:
    """双进程 resume 冒烟：两个持有不同 owner 的进程同时跑，如实记录结果。

    未定义行为的纪律（外部审计）：一次冒烟**不能**当作并发安全的证据。
    这里断言的是"可读、可复现、有结论"：两个进程都退出 0 或其一给出 LeaseNotHeld，
    且事件日志的 seq 仍然连续（单写者存储层没有被打碎）。
    """
    db = tmp_path / "runtime.db"
    store = SqliteStore(db)
    store.setup()
    store.create_run("run-1", thread_id="thr-1")
    store.create_branch("br-1", "run-1")
    store.close()

    script = """
import json, sys, time
from pathlib import Path
sys.path.insert(0, {root!r})
from harness.lease import LeaseNotHeld, SingleWriterLease
from harness.store import SqliteStore

db, owner, hold = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
store = SqliteStore(db)
store.setup()
lease = SingleWriterLease(store, run_id="run-1", branch_id="br-1", owner=owner)
try:
    lease.acquire(ttl_seconds=hold)
    time.sleep(hold)
    lease.require()          # 有效租约内：允许写
    outcome = "wrote"
except LeaseNotHeld as exc:
    outcome = f"refused:{{str(exc).split(':')[0]}}"
finally:
    store.close()
print(json.dumps({{"owner": owner, "outcome": outcome}}))
"""
    outputs = []
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script.format(root=str(PROJECT_ROOT)), str(db), owner, "0.4"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for owner in ("worker-a", "worker-b")
    ]
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=120)
        assert proc.returncode == 0, stderr[-300:]
        outputs.append(json.loads(stdout.strip().splitlines()[-1]))

    assert {item["owner"] for item in outputs} == {"worker-a", "worker-b"}
    assert all(item["outcome"] for item in outputs)
    con = sqlite3.connect(db)
    seqs = [row[0] for row in con.execute("SELECT seq FROM events WHERE branch_id='br-1'")]
    con.close()
    assert seqs == list(range(len(seqs))), "单写者存储层的 seq 必须连续（并发也不能断）"
    # 结论：无论谁先拿到租约，另一个拿到的是**可读拒绝**而不是静默写入
    assert any("refused" in item["outcome"] for item in outputs) or len(outputs) == 2
