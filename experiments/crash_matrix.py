"""崩溃矩阵：窗口 × outbox × 下游幂等 × 探针，逐格跑真实 kill -9 子进程。

每格由若干阶段组成（run → approve → resume → resume），第二条起可以带崩溃注入；
所有阶段都在独立进程里跑，因此"崩溃后能依靠什么"这个问题只能在持久化事实上回答。

结论口径固定为"在 X 条件下 A 比 B 好/坏 Y"，不做单点达标声明。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fakeworld.world import World
from harness.chaos import WINDOWS
from harness.state import reduce_events
from harness.store.sqlite_store import SqliteStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 每个窗口的阶段计划：(模式, 崩溃注入规格)。空规格 = 该阶段不注入。
PHASE_PLANS: dict[str, tuple[tuple[str, str], ...]] = {
    "pre_tool_exec": (("run", ""), ("approve", ""), ("resume", "pre_tool_exec:1"), ("resume", "")),
    "post_tool_effect_pre_record": (
        ("run", ""),
        ("approve", ""),
        ("resume", "post_tool_effect_pre_record:1"),
        ("resume", ""),
    ),
    "post_record_pre_commit": (
        ("run", ""),
        ("approve", ""),
        ("resume", "post_record_pre_commit:1"),
        ("resume", ""),
    ),
    "post_approval_pre_exec": (
        ("run", ""),
        ("approve", ""),
        ("resume", "post_approval_pre_exec:1"),
        ("resume", ""),
    ),
    "after_resume": (("run", ""), ("approve", ""), ("resume", "after_resume:1"), ("resume", "")),
    "(tamper)": (("run", ""), ("approve", ""), ("resume", "")),
}


@dataclass(frozen=True)
class Cell:
    window: str
    outbox: bool
    tool_idem: bool
    probe: bool = True
    tamper: bool = False

    def slug(self) -> str:
        return (
            f"{self.window}-outbox{int(self.outbox)}-idem{int(self.tool_idem)}"
            f"-probe{int(self.probe)}-tamper{int(self.tamper)}"
        )


def build_cells() -> list[Cell]:
    """有设计的对照，而不是全交叉：把每个假设放在最省的格子上验证。"""
    cells: list[Cell] = []
    # 主对照（窗口 2）：outbox 与下游幂等各自的作用
    for outbox in (False, True):
        for tool_idem in (False, True):
            cells.append(Cell("post_tool_effect_pre_record", outbox, tool_idem))
    # 没有按键读回能力：outbox 只能把"重复"变成"unknown 待对账"
    cells.append(Cell("post_tool_effect_pre_record", outbox=True, tool_idem=False, probe=False))
    # 其余窗口：outbox 开/关各一遍（非幂等写）
    for window in (
        "pre_tool_exec",
        "post_record_pre_commit",
        "post_approval_pre_exec",
        "after_resume",
    ):
        for outbox in (False, True):
            cells.append(Cell(window, outbox, tool_idem=False))
    # 篡改控制组：批准之后参数被改写，必须拒绝执行
    cells.append(Cell("(tamper)", outbox=True, tool_idem=False, tamper=True))
    return cells


def run_cell(cell: Cell, repeats: int, root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(repeats):
        run_dir = root / f"{cell.slug()}-{index}"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)
        phases = run_phases(cell, run_dir)
        result = {
            "window": cell.window,
            "outbox": cell.outbox,
            "tool_idem": cell.tool_idem,
            "probe": cell.probe,
            "tamper": cell.tamper,
            "phases": phases,
            "workdir": str(run_dir),
            **analyze(run_dir),
        }
        rows.append({**result, **evaluate(cell, result)})
    return rows


def run_phases(cell: Cell, run_dir: Path) -> list[dict[str, Any]]:
    phases: list[dict[str, Any]] = []
    for mode, chaos_spec in PHASE_PLANS[cell.window]:
        command = [
            sys.executable,
            "-m",
            "experiments.worker",
            "--run-dir",
            str(run_dir),
            "--mode",
            mode,
            "--dedup",
            "on",
            "--outbox",
            "on" if cell.outbox else "off",
            "--tool-idem",
            "on" if cell.tool_idem else "off",
            "--probe",
            "on" if cell.probe else "off",
            "--tamper",
            "on" if cell.tamper else "off",
        ]
        env = {**os.environ, "CHAOS_WINDOWS": chaos_spec, "PYTHONHASHSEED": "0"}
        proc = subprocess.run(
            command, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=120
        )
        phases.append(
            {
                "mode": mode,
                "chaos": chaos_spec,
                "exit_code": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip()[-300:],
            }
        )
    return phases


def analyze(run_dir: Path) -> dict[str, Any]:
    ids = json.loads((run_dir / "ids.json").read_text(encoding="utf-8"))
    findings: dict[str, Any] = {}

    with World(run_dir / "world.db") as world:
        counts = world.effect_counts_by_key()
        findings["effects_total"] = world.total_effects()
        findings["effects_by_key"] = counts
        findings["duplicate_keys"] = {k: v for k, v in counts.items() if v > 1}
        findings["max_effects_per_key"] = max(counts.values()) if counts else 0

    store = SqliteStore(run_dir / "runtime.db")
    try:
        store.setup()
        events = store.effective_events(ids["branch_id"])
        seqs = [e.seq for e in events]
        findings["log_size"] = len(events)
        findings["log_seq_contiguous"] = seqs == list(range(len(seqs)))
        findings["log_event_ids_unique"] = len({e.event_id for e in events}) == len(events)
        state, violations = reduce_events(events)
        findings["invariant_violations"] = [v.code for v in violations]
        findings["final_status"] = state.status.value
        findings["open_tool_calls"] = sorted(state.open_tool_calls)
        findings["tool_result_statuses"] = _statuses(events)
        findings["pending_rows"] = _count_where(store, "status='pending'")
        findings["unknown_rows"] = _count_where(store, "status='unknown'")
        findings["checkpoints"] = _count(store, "checkpoints")
        findings["orphan_writes"] = _count_orphan_writes(store)
    finally:
        store.close()

    outcome_path = run_dir / "outcome_resume.json"
    if outcome_path.exists():
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        findings["outcome"] = {
            "status": outcome["status"],
            "executed": outcome["executed"],
            "replayed": outcome["replayed"],
            "reconciled": outcome["reconciled"],
            "unknown": outcome["unknown"],
            "rejected": outcome["rejected"],
            "probes": outcome["probes"],
        }
    marker = run_dir / "crash_marker.json"
    findings["crash_marker"] = (
        json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else None
    )
    return findings


def _statuses(events: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        if event.type == "tool_result":
            status = str(event.payload.get("status"))
            counts[status] = counts.get(status, 0) + 1
    return counts


def _count(store: SqliteStore, table: str) -> int:
    row = store._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"])


def _count_where(store: SqliteStore, where: str) -> int:
    row = store._conn.execute(f"SELECT COUNT(*) AS n FROM tool_calls WHERE {where}").fetchone()
    return int(row["n"])


def _count_orphan_writes(store: SqliteStore) -> int:
    row = store._conn.execute(
        "SELECT COUNT(*) AS n FROM checkpoint_writes w"
        " WHERE NOT EXISTS (SELECT 1 FROM checkpoints c"
        " WHERE c.thread_id=w.thread_id AND c.checkpoint_ns=w.checkpoint_ns"
        " AND c.checkpoint_id=w.checkpoint_id)"
    ).fetchone()
    return int(row["n"])


def expectation(cell: Cell) -> dict[str, Any]:
    """W3 的预期模型。"""
    if cell.tamper:
        return {
            "expect_duplicate": False,
            "expect_unknown_row": False,
            "expect_effects": 0,
            "expect_reconciled_min": 0,
            "note": "审批后参数被改写：预期拒绝执行，0 副作用",
        }
    if cell.window == "post_tool_effect_pre_record" and not cell.tool_idem:
        if not cell.outbox:
            return {
                "expect_duplicate": True,
                "expect_unknown_row": False,
                "expect_effects": 2,
                "expect_reconciled_min": 0,
                "note": "W2 基线：效果已发生、无记录 → 恢复时重跑 → 重复",
            }
        if not cell.probe:
            return {
                "expect_duplicate": False,
                "expect_unknown_row": True,
                "expect_effects": 1,
                "expect_reconciled_min": 0,
                "note": "outbox 有意图行但无按键读回 → 不重跑，转 unknown 待对账",
            }
        return {
            "expect_duplicate": False,
            "expect_unknown_row": False,
            "expect_effects": 1,
            "expect_reconciled_min": 1,
            "note": "outbox + 按键读回 → 探针确认已生效，重放不重跑",
        }
    return {
        "expect_duplicate": False,
        "expect_unknown_row": False,
        "expect_effects": 1,
        "expect_reconciled_min": 0,
        "note": "预期恰好一次副作用",
    }


def evaluate(cell: Cell, result: dict[str, Any]) -> dict[str, Any]:
    expected = expectation(cell)
    crashed = [p for p in result["phases"] if p["exit_code"] != 0]
    if cell.window in WINDOWS:
        marker_ok = (result.get("crash_marker") or {}).get("window") == cell.window
        phases_ok = len(crashed) == 1 and crashed[0]["exit_code"] == -9
    else:
        marker_ok = not crashed
        phases_ok = not crashed
    log_ok = bool(
        result.get("log_seq_contiguous")
        and result.get("log_event_ids_unique")
        and not result.get("invariant_violations")
    )
    duplicated = bool(result.get("duplicate_keys"))
    unknown_row = bool(result.get("unknown_rows"))
    outcome = result.get("outcome") or {}
    reconciled = int(outcome.get("reconciled", 0))
    effects = int(result.get("effects_total", 0))
    closed_ok = not result.get("open_tool_calls")

    prediction_holds = (
        duplicated == expected["expect_duplicate"]
        and unknown_row == expected["expect_unknown_row"]
        and effects == expected["expect_effects"]
        and reconciled >= expected["expect_reconciled_min"]
    )
    return {
        **expected,
        "marker_ok": marker_ok,
        "phases_ok": phases_ok,
        "log_ok": log_ok,
        "duplicated": duplicated,
        "unknown_row": unknown_row,
        "reconciled": reconciled,
        "prediction_holds": prediction_holds,
        "verdict": (
            "as-predicted"
            if prediction_holds and marker_ok and phases_ok and log_ok and closed_ok
            else "prediction-violated"
        ),
    }


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            f"{row['window']} outbox={int(row['outbox'])} idem={int(row['tool_idem'])}"
            f" probe={int(row['probe'])} tamper={int(row['tamper'])}"
        )
        groups.setdefault(key, []).append(row)
    summary: list[dict[str, Any]] = []
    for key, group in sorted(groups.items()):
        runs = len(group)
        summary.append(
            {
                "cell": key,
                "runs": runs,
                "crash_ok": f"{sum(r['marker_ok'] and r['phases_ok'] for r in group)}/{runs}",
                "log_ok": f"{sum(r['log_ok'] for r in group)}/{runs}",
                "effects": group[0]["effects_total"],
                "duplicated": f"{sum(r['duplicated'] for r in group)}/{runs}",
                "max_per_key": max(r["max_effects_per_key"] for r in group),
                "unknown_rows": max(r["unknown_rows"] for r in group),
                "reconciled": max(r["reconciled"] for r in group),
                "expected_dup": group[0]["expect_duplicate"],
                "verdict": (
                    "as-predicted"
                    if all(r["verdict"] == "as-predicted" for r in group)
                    else "prediction-violated"
                ),
            }
        )
    return summary


def render_markdown(summary: list[dict[str, Any]]) -> str:
    lines = [
        "| 格 | 次数 | 注入/阶段 | 日志一致 | 效果数 | 重复运行 | 单键最大 | unknown 行 |"
        " 探针对账 | 预期重复 | 判定 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in summary:
        lines.append(
            "| {cell} | {runs} | {crash_ok} | {log_ok} | {effects} | {duplicated} |"
            " {max_per_key} | {unknown_rows} | {reconciled} | {expected_dup} | {verdict} |".format(
                **cell
            )
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="experiments.crash_matrix")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--workroot", default="")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--md-out", default="")
    args = parser.parse_args(argv)

    cells = build_cells()
    root = Path(args.workroot) if args.workroot else Path(tempfile.mkdtemp(prefix="w3-matrix-"))
    rows: list[dict[str, Any]] = []
    for cell in cells:
        rows.extend(run_cell(cell, args.repeats, root))
    summary = summarize(rows)
    markdown = render_markdown(summary)
    print(markdown)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {"summary": summary, "cells": [c.__dict__ for c in cells], "runs": rows},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    if args.md_out:
        Path(args.md_out).write_text(markdown + "\n", encoding="utf-8")
    violated = [cell for cell in summary if cell["verdict"] != "as-predicted"]
    return 1 if violated else 0


if __name__ == "__main__":
    sys.exit(main())
