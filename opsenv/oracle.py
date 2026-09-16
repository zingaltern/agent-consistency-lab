"""外部账本 oracle：对"任意时刻被 SIGKILL 的运行"做统一判定（R-A2）。

为什么要有它：命名窗口是**采样 6 个点**验证语义，不是验证整条时间轴。随机时刻注入能
覆盖窗口之外的位置，但"覆盖"只有在**判定标准统一**时才有意义——所以这里把不变量判定
固化成纯函数，`experiments/chaos_fuzz.py` 与 `scripts/probe_*.py` 只做驱动与断言。

判定只读两样外部事实（绝不采信 runtime 自己的计数）：

1. ``runtime.db``：事件日志（seq / event_id / tool_result）+ ``tool_calls`` 意图行；
2. ``world.db``：**外部副作用账本**（效果发生了几次，只有它说了算）。

两条纪律：

* **读库必须连 ``-wal``/``-shm`` 一起快照**：进程被 SIGKILL 后已提交的效果可能还在 WAL 里，
  只拷主库会把"效果已发生"读成 0（外部审计实测踩过的坑，见 docs/tester-prompt.md）。
* **判定与"预期模型"分离**：这里只报**原始**不变量违反；某个配置下重复副作用是否
  "符合预期"由调用方按对照面分类（见 chaos_fuzz 的 `expected_codes`）。
  混在一起会让"已知语义"悄悄变成"没有发现"。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LEGAL_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        "completed",  # 任务正常结束
        "failed",  # 明确失败（fatal 错误吸收态）
        "waiting_human",  # 停在审批门：脚本计划内的合法驻留
    }
)

CLOSED_ROW_STATUSES: frozenset[str] = frozenset({"executed", "failed"})


@dataclass(frozen=True)
class Finding:
    code: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "evidence": self.evidence}


@dataclass
class AuditResult:
    findings: list[Finding]
    facts: dict[str, Any]

    @property
    def ok(self) -> bool:
        return not self.findings

    def codes(self) -> list[str]:
        return [finding.code for finding in self.findings]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "findings": [finding.as_dict() for finding in self.findings],
            "facts": self.facts,
        }


def snapshot_db(path: Path) -> Path:
    """把 ``path``（连同 ``-wal``/``-shm``）拷进临时目录后返回主库路径。

    必须以快照读：进程刚被 SIGKILL 时，最后一次已提交的效果可能只存在于 WAL 里。
    """
    target = Path(tempfile.mkdtemp(prefix="oracle-snap-"))
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(path) + suffix)
        if source.exists():
            shutil.copy2(source, target / (path.name + suffix))
    return target / path.name


# ------------------------------------------------------------------ 不变量判定


def read_effects(run_dir: Path) -> dict[str, Any]:
    """只读**外部账本**（`world.db`，连 `-wal` 一起快照）的效果计数。

    与 runtime 的日志解耦：主库被截断/损坏时，账本仍然可读——这正是"外部账本做裁判"
    的意义（判定副作用次数不需要相信 runtime 的任何自述）。
    """
    world_db = run_dir / "world.db"
    if not world_db.exists():
        return {"ledger_rows": 0, "max_per_key": 0, "counts": {}}
    con = sqlite3.connect(snapshot_db(world_db))
    try:
        counts = {
            str(key): int(count)
            for key, count in con.execute(
                "SELECT idempotency_key, COUNT(*) FROM effects GROUP BY idempotency_key"
            ).fetchall()
        }
    finally:
        con.close()
    return {
        "ledger_rows": sum(counts.values()),
        "max_per_key": max(counts.values(), default=0),
        "counts": counts,
    }


def _unreadable(runtime_db: Path, exc: sqlite3.DatabaseError) -> list[Finding]:
    """运行时主库读不出来 = 一条**显式发现**（日志是权威；权威读不了就不是"没问题"）。

    为什么重要：torn write 探测器要求"绝不静默按破损状态续跑"。如果 oracle 在这种情况下
    静默返回空 findings，探测器的判断就会反过来——把"读不出来"读成"没有违规"。
    """
    return [
        Finding(
            code="inv_log_readable",
            detail=f"{runtime_db.name} 无法读取：{type(exc).__name__}: {exc}",
            evidence={"path": str(runtime_db)},
        )
    ]


def check_no_tamper(runtime_db: Path) -> tuple[list[Finding], dict[str, Any]]:
    """inv_no_tamper：``seq`` 在每个分支内从 0 连续、``event_id`` 全局唯一。

    （与外部审计的 b3 日志审计同口径：这两条是"日志没有被删改/漏写"的可判定痕迹。）
    """
    findings: list[Finding] = []
    snapshot = snapshot_db(runtime_db)
    try:
        con = sqlite3.connect(snapshot)
    except sqlite3.DatabaseError as exc:
        return _unreadable(runtime_db, exc), {"events": 0, "branches": 0, "seq_contiguous": False}
    try:
        try:
            rows = con.execute(
                "SELECT branch_id, seq, event_id FROM events ORDER BY branch_id, seq"
            )
            per_branch: dict[str, list[int]] = {}
            ids: list[str] = []
            for branch_id, seq, event_id in rows.fetchall():
                per_branch.setdefault(branch_id, []).append(int(seq))
                ids.append(event_id)
        except sqlite3.DatabaseError as exc:
            # connect() 在 sqlite 上是惰性的：坏文件要到真正查询时才报错
            return _unreadable(runtime_db, exc), {
                "events": 0,
                "branches": 0,
                "seq_contiguous": False,
            }
    finally:
        con.close()

    for branch_id, seqs in per_branch.items():
        if seqs != list(range(len(seqs))):
            findings.append(
                Finding(
                    code="inv_no_tamper",
                    detail=f"分支 {branch_id} 的 seq 不连续：{seqs[:20]}",
                    evidence={"branch_id": branch_id, "seqs": seqs[:50]},
                )
            )
    duplicates = {event_id for event_id in ids if ids.count(event_id) > 1}
    if duplicates:
        findings.append(
            Finding(
                code="inv_no_tamper",
                detail=f"event_id 重复 {len(duplicates)} 个: {sorted(duplicates)[:5]}",
                evidence={"duplicates": sorted(duplicates)[:20]},
            )
        )
    facts = {
        "events": len(ids),
        "branches": len(per_branch),
        "seq_contiguous": not any(f.code == "inv_no_tamper" for f in findings),
    }
    return findings, facts


def check_closed_calls(runtime_db: Path) -> tuple[list[Finding], dict[str, Any]]:
    """inv_closed_calls：不存在"已 executed/failed 的工具行却没有对应 ``tool_result`` 事件"。

    方向刻意只查这一边：loop 的正常顺序是"先写事件、后写行"，因此**行在事件必在**；
    反过来（事件在、行缺失）在关掉去重/outbox 时是正常形态，不能当违规。
    """
    findings: list[Finding] = []
    snapshot = snapshot_db(runtime_db)
    try:
        con = sqlite3.connect(snapshot)
    except sqlite3.DatabaseError as exc:
        return _unreadable(runtime_db, exc), {"closed_rows": 0, "tool_results": 0}
    try:
        closed_rows = [
            row[0]
            for row in con.execute(
                "SELECT tool_call_id FROM tool_calls WHERE status IN ('executed','failed')"
            ).fetchall()
        ]
        event_call_ids: set[str] = set()
        for (payload_json,) in con.execute(
            "SELECT payload_json FROM events WHERE type='tool_result'"
        ).fetchall():
            try:
                event_call_ids.add(str(json.loads(payload_json).get("tool_call_id")))
            except (TypeError, ValueError) as exc:  # pragma: no cover - 坏 payload 别的路径会报
                findings.append(
                    Finding(
                        code="inv_closed_calls",
                        detail=f"tool_result 的 payload 无法解析：{exc}",
                        evidence={"payload": str(payload_json)[:200]},
                    )
                )
    finally:
        con.close()

    unclosed = [call_id for call_id in closed_rows if call_id not in event_call_ids]
    if unclosed:
        findings.append(
            Finding(
                code="inv_closed_calls",
                detail=f"{len(unclosed)} 个已闭合的工具行没有 tool_result 事件: {unclosed[:5]}",
                evidence={"unclosed": unclosed[:20], "tool_results": len(event_call_ids)},
            )
        )
    return findings, {"closed_rows": len(closed_rows), "tool_results": len(event_call_ids)}


def check_effect_accounting(run_dir: Path) -> tuple[list[Finding], dict[str, Any]]:
    """inv_effect_accounting：每个幂等键的效果行数 ≤1，或在 runtime 侧显式呈报了 unknown。

    ">1 行且没有任何 unknown 呈报" = 静默重复，是 fuzz 要抓的东西。
    （配置层面允许重复的对照格由调用方按 `expected_codes` 分类，见模块 docstring。）
    """
    findings: list[Finding] = []
    world_db = run_dir / "world.db"
    runtime_db = run_dir / "runtime.db"
    facts: dict[str, Any] = {"ledger_rows": 0, "max_per_key": 0}
    if not world_db.exists():
        return findings, facts

    snapshot = snapshot_db(world_db)
    con = sqlite3.connect(snapshot)
    try:
        counts = {
            str(key): int(count)
            for key, count in con.execute(
                "SELECT idempotency_key, COUNT(*) FROM effects GROUP BY idempotency_key"
            ).fetchall()
        }
    finally:
        con.close()

    unknown_keys: set[str] = set()
    if runtime_db.exists():
        snapshot_runtime = snapshot_db(runtime_db)
        con = sqlite3.connect(snapshot_runtime)
        try:
            unknown_keys = {
                str(row[0])
                for row in con.execute(
                    "SELECT idempotency_key FROM tool_calls WHERE status='unknown'"
                ).fetchall()
            }
        finally:
            con.close()

    facts["ledger_rows"] = sum(counts.values())
    facts["max_per_key"] = max(counts.values(), default=0)
    for key, count in counts.items():
        if count > 1 and key not in unknown_keys:
            findings.append(
                Finding(
                    code="inv_effect_accounting",
                    detail=f"键 {key} 有 {count} 行效果，且没有任何 unknown 呈报",
                    evidence={"key": key, "rows": count, "unknown_reported": False},
                )
            )
    return findings, facts


def check_termination(
    *, status: str, resumes_used: int, max_resumes: int
) -> tuple[list[Finding], dict[str, Any]]:
    """inv_recovery_terminates：恢复推进必须在有限次 resume 内落到合法终态。"""
    findings: list[Finding] = []
    if status not in LEGAL_TERMINAL_STATUSES:
        findings.append(
            Finding(
                code="inv_recovery_terminates",
                detail=(
                    f"{max_resumes} 次 resume 后仍未达合法终态（当前 status={status!r}；"
                    f"合法终态 = {sorted(LEGAL_TERMINAL_STATUSES)}）"
                ),
                evidence={"status": status, "resumes_used": resumes_used},
            )
        )
    return findings, {"status": status, "resumes_used": resumes_used}


def audit_run(
    run_dir: Path, *, status: str = "completed", resumes_used: int = 0, max_resumes: int = 0
) -> AuditResult:
    """四类判定合成一次审计；``facts`` 里保留原始计数供报告复算。"""
    findings: list[Finding] = []
    facts: dict[str, Any] = {}
    for checker, key in (
        (check_no_tamper(run_dir / "runtime.db"), "log"),
        (check_closed_calls(run_dir / "runtime.db"), "calls"),
        (check_effect_accounting(run_dir), "effects"),
    ):
        findings.extend(checker[0])
        facts[key] = checker[1]
    termination_findings, termination_facts = check_termination(
        status=status, resumes_used=resumes_used, max_resumes=max_resumes
    )
    findings.extend(termination_findings)
    facts["termination"] = termination_facts
    return AuditResult(findings=findings, facts=facts)
