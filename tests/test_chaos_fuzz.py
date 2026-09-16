"""oracle 与 fuzz 驱动的单测（合成数据）。

为什么用合成数据而不是真实 run 目录：oracle 的价值在于"**该报的时候真的报**"，
所以这里构造成"恰好违反某一条不变量"的最小数据库，逐条验证它被抓到——
真实的 30× fuzz 由 `experiments.chaos_fuzz` 在 nightly 跑，两者互补：

* 这里证明 oracle **敏感**（否则 fuzz 全绿毫无意义）；
* nightly 证明系统在随机注入下**没有**新类违例。

合成的三条不变量各自对应一个"历史上真的会发生的坏状态"：
seq 断洞（漏写/删改）、已执行却无 tool_result（调用悬挂）、
账本重复且没有 unknown 呈报（静默重复）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from opsenv.oracle import (
    audit_run,
    check_closed_calls,
    check_effect_accounting,
    check_no_tamper,
    check_termination,
    read_effects,
)


def _make_runtime_db(path: Path, rows: list[tuple]) -> None:
    """建一个最小 events 表：``(branch_id, seq, event_id, type, payload_json)``。"""
    con = sqlite3.connect(path)
    # 刻意**不建**主键约束：真实库里 event_id 是主键（数据库自己会挡住重复），
    # 但 oracle 要防的是"副本被改过/被别的东西写过"——所以判定必须独立于约束存在。
    con.execute(
        "CREATE TABLE events (event_id TEXT, run_id TEXT, branch_id TEXT,"
        " seq INTEGER, kind TEXT, type TEXT, source TEXT, parent_id TEXT,"
        " payload_json TEXT, created_at REAL)"
    )
    con.execute(
        "CREATE TABLE tool_calls (tool_call_id TEXT PRIMARY KEY, run_id TEXT, branch_id TEXT,"
        " tool TEXT, args_json TEXT, args_sha256 TEXT, idempotency_key TEXT, effect TEXT,"
        " status TEXT, result_json TEXT, error_class TEXT, started_at REAL, ended_at REAL)"
    )
    for branch_id, seq, event_id, event_type, payload in rows:
        con.execute(
            "INSERT INTO events(event_id, run_id, branch_id, seq, kind, type, source,"
            " payload_json, created_at) VALUES(?, 'run', ?, ?, 'tree_node', ?, 'agent', ?, 0.0)",
            (event_id, branch_id, seq, event_type, payload),
        )
    con.commit()
    con.close()


def _make_world_db(path: Path, keys: list[str]) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE effects (effect_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " idempotency_key TEXT, operation TEXT, payload_json TEXT, created_at REAL)"
    )
    for key in keys:
        con.execute(
            "INSERT INTO effects(idempotency_key, operation, payload_json, created_at)"
            " VALUES(?, 'op', '{}', 0.0)",
            (key,),
        )
    con.commit()
    con.close()


def test_clean_run_dir_passes_every_invariant(tmp_path: Path) -> None:
    """先证明"干净的数据不会乱报"——否则下面的红可能是 oracle 无脑报错。"""
    _make_runtime_db(
        tmp_path / "runtime.db",
        [
            ("br", 0, "e0", "user_message", "{}"),
            ("br", 1, "e1", "agent_message", "{}"),
            ("br", 2, "e2", "tool_call", '{"tool_call_id": "tc1"}'),
            ("br", 3, "e3", "tool_result", '{"tool_call_id": "tc1", "status": "executed"}'),
        ],
    )
    _make_world_db(tmp_path / "world.db", ["idem_1"])
    result = audit_run(tmp_path, status="completed")
    assert result.ok, result.codes()


def test_seq_gap_is_caught(tmp_path: Path) -> None:
    """inv_no_tamper：seq 出现断洞（漏写或删改的痕迹）必须被抓到。"""
    _make_runtime_db(
        tmp_path / "runtime.db",
        [("br", 0, "e0", "user_message", "{}"), ("br", 2, "e2", "agent_message", "{}")],
    )
    _make_world_db(tmp_path / "world.db", [])
    findings, _ = check_no_tamper(tmp_path / "runtime.db")
    assert [finding.code for finding in findings] == ["inv_no_tamper"]
    assert "不连续" in findings[0].detail


def test_duplicate_event_id_is_caught(tmp_path: Path) -> None:
    """inv_no_tamper：event_id 重复（同一事件写了两遍）必须被抓到。"""
    _make_runtime_db(
        tmp_path / "runtime.db",
        [("br", 0, "same", "user_message", "{}"), ("br", 1, "same", "agent_message", "{}")],
    )
    findings, _ = check_no_tamper(tmp_path / "runtime.db")
    assert [finding.code for finding in findings] == ["inv_no_tamper"]
    assert "重复" in findings[0].detail


def test_executed_row_without_tool_result_is_caught(tmp_path: Path) -> None:
    """inv_closed_calls：工具行说"执行完了"，日志里却没有 tool_result ⇒ 调用悬挂。"""
    runtime_db = tmp_path / "runtime.db"
    _make_runtime_db(runtime_db, [("br", 0, "e0", "user_message", "{}")])
    con = sqlite3.connect(runtime_db)
    con.execute(
        "INSERT INTO tool_calls(tool_call_id, run_id, branch_id, tool, args_json, args_sha256,"
        " idempotency_key, effect, status, started_at) VALUES"
        "('tc1', 'run', 'br', 'scale_pool', '{}', 'sha', 'idem_x',"
        " 'write_nonidempotent', 'executed', 0.0)"
    )
    con.commit()
    con.close()
    findings, facts = check_closed_calls(runtime_db)
    assert [finding.code for finding in findings] == ["inv_closed_calls"]
    assert facts["tool_results"] == 0


def test_event_without_row_is_not_a_violation(tmp_path: Path) -> None:
    """反向不构成违规：事件在、意图行缺失是关掉 outbox/dedup 时的正常形态。"""
    _make_runtime_db(
        tmp_path / "runtime.db",
        [("br", 0, "e0", "tool_result", '{"tool_call_id": "tc1", "status": "executed"}')],
    )
    findings, _ = check_closed_calls(tmp_path / "runtime.db")
    assert findings == []


def test_duplicate_ledger_rows_without_unknown_are_caught(tmp_path: Path) -> None:
    """inv_effect_accounting：账本 2 行且没有 unknown 呈报 = 静默重复。"""
    _make_runtime_db(tmp_path / "runtime.db", [("br", 0, "e0", "user_message", "{}")])
    _make_world_db(tmp_path / "world.db", ["idem_dup", "idem_dup"])
    findings, facts = check_effect_accounting(tmp_path)
    assert [finding.code for finding in findings] == ["inv_effect_accounting"]
    assert facts["max_per_key"] == 2


def test_duplicate_with_unknown_report_is_not_a_finding(tmp_path: Path) -> None:
    """有 unknown 呈报时不算"静默"重复——那正是 outbox 的设计意图（把重复变成有界的未知）。"""
    runtime_db = tmp_path / "runtime.db"
    _make_runtime_db(runtime_db, [("br", 0, "e0", "user_message", "{}")])
    con = sqlite3.connect(runtime_db)
    con.execute(
        "INSERT INTO tool_calls(tool_call_id, run_id, branch_id, tool, args_json, args_sha256,"
        " idempotency_key, effect, status, started_at) VALUES"
        "('tc1', 'run', 'br', 'scale_pool', '{}', 'sha', 'idem_dup',"
        " 'write_nonidempotent', 'unknown', 0.0)"
    )
    con.commit()
    con.close()
    _make_world_db(tmp_path / "world.db", ["idem_dup", "idem_dup"])
    findings, _ = check_effect_accounting(tmp_path)
    assert findings == []


def test_unreadable_runtime_db_is_a_finding_not_a_silent_pass(tmp_path: Path) -> None:
    """torn write 的核心：主库读不出来 = **显式发现**，绝不是"没有违规"。

    修复前会怎样：oracle 在 sqlite 异常上静默返回空 findings，于是"截断后 resume"
    会被判成"没问题"——探测器的结论会整个反过来。
    """
    (tmp_path / "runtime.db").write_bytes(b"not a database at all")
    _make_world_db(tmp_path / "world.db", [])
    findings, _ = check_no_tamper(tmp_path / "runtime.db")
    assert [finding.code for finding in findings] == ["inv_log_readable"]


def test_read_effects_works_even_when_runtime_log_is_broken(tmp_path: Path) -> None:
    """账本裁判与 runtime 日志解耦：主库坏了，账本照样读得出来。"""
    (tmp_path / "runtime.db").write_bytes(b"broken")
    _make_world_db(tmp_path / "world.db", ["k1", "k1", "k2"])
    effects = read_effects(tmp_path)
    assert effects["ledger_rows"] == 3
    assert effects["max_per_key"] == 2


def test_termination_requires_a_legal_terminal_state() -> None:
    """inv_recovery_terminates：跑不完/停在半路必须报出来，而不是当成"还在跑"。"""
    findings, _ = check_termination(status="running", resumes_used=6, max_resumes=6)
    assert [finding.code for finding in findings] == ["inv_recovery_terminates"]
    for status in ("completed", "failed", "waiting_human"):
        ok_findings, _ = check_termination(status=status, resumes_used=1, max_resumes=6)
        assert ok_findings == [], status


def test_sample_kill_times_are_seed_determined() -> None:
    """注入时刻必须由 seed 决定：同一个 seed 产生同一串值（可复现的输入）。"""
    from experiments.chaos_fuzz import sample_kill_times

    first = sample_kill_times(seed=20260917, repeats=30, window_ms=180)
    second = sample_kill_times(seed=20260917, repeats=30, window_ms=180)
    other = sample_kill_times(seed=20260918, repeats=30, window_ms=180)
    assert first == second
    assert first != other
    assert all(1 <= value <= 180 for value in first), "采样必须落在标定窗口内"
