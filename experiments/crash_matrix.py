"""崩溃矩阵：窗口 × 运行时去重 × 下游幂等，逐格跑真实 kill -9 子进程。

每一格 = (崩溃窗口, runtime 去重, 下游是否支持幂等键)，重复 N 次：
1. 子进程执行 run，在指定窗口的**第 2 次命中**处被 SIGKILL（第 1 次是读工具）；
2. 校验 crash_marker 证明"确实死在该窗口"；
3. 子进程执行 resume（无注入）；
4. 由外部账本裁定副作用次数，并断言日志不重不漏、不变量无违反。

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
from pathlib import Path
from typing import Any

from fakeworld.world import World
from harness.chaos import WINDOWS
from harness.state import reduce_events
from harness.store.sqlite_store import SqliteStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
W2_WINDOWS = ("pre_tool_exec", "post_tool_effect_pre_record", "post_record_pre_commit")
WRITE_TOOL_OCCURRENCE = 2  # 场景里第 1 次工具调用是只读，第 2 次才是写操作


def run_once(*, window: str, dedup: bool, tool_idem: bool, workdir: Path) -> dict[str, Any]:
    run_dir = workdir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    common = [
        sys.executable,
        "-m",
        "experiments.worker",
        "--run-dir",
        str(run_dir),
        "--dedup",
        "on" if dedup else "off",
        "--tool-idem",
        "on" if tool_idem else "off",
    ]
    env = {
        **os.environ,
        "CHAOS_WINDOWS": f"{window}:{WRITE_TOOL_OCCURRENCE}",
        "PYTHONHASHSEED": "0",
    }
    crashed = subprocess.run(
        [*common, "--mode", "run"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    marker_path = run_dir / "crash_marker.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.exists() else None

    resumed = subprocess.run(
        [*common, "--mode", "resume"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "CHAOS_WINDOWS": "", "PYTHONHASHSEED": "0"},
        capture_output=True,
        text=True,
        timeout=120,
    )

    result: dict[str, Any] = {
        "window": window,
        "dedup": dedup,
        "tool_idem": tool_idem,
        "crash_exit_code": crashed.returncode,
        "marker_window": (marker or {}).get("window"),
        "resume_exit_code": resumed.returncode,
        "resume_stdout": resumed.stdout.strip(),
        "resume_stderr": resumed.stderr.strip()[-400:],
    }
    result.update(analyze(run_dir))
    return result


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
        _, violations = reduce_events(events)
        findings["invariant_violations"] = [v.code for v in violations]
        state, _ = reduce_events(events)
        findings["final_status"] = state.status.value
        findings["open_tool_calls"] = sorted(state.open_tool_calls)
        findings["tool_call_rows"] = _count(store, "tool_calls")
        findings["checkpoints"] = _count(store, "checkpoints")
        findings["orphan_writes"] = _count_orphan_writes(store)
    finally:
        store.close()

    outcome_path = run_dir / "outcome_resume.json"
    if outcome_path.exists():
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        findings["resume_outcome_status"] = outcome["status"]
    return findings


def _count(store: SqliteStore, table: str) -> int:
    row = store._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"])


def _count_orphan_writes(store: SqliteStore) -> int:
    row = store._conn.execute(
        "SELECT COUNT(*) AS n FROM checkpoint_writes w"
        " WHERE NOT EXISTS (SELECT 1 FROM checkpoints c"
        " WHERE c.thread_id=w.thread_id AND c.checkpoint_ns=w.checkpoint_ns"
        " AND c.checkpoint_id=w.checkpoint_id)"
    ).fetchone()
    return int(row["n"])


def classify(window: str, tool_idem: bool) -> dict[str, Any]:
    """本矩阵的预期模型（W2 版，尚未引入 outbox）。"""
    expected_duplicate = window == "post_tool_effect_pre_record" and not tool_idem
    return {
        "expected_duplicate": expected_duplicate,
        "note": (
            "效果已发生但记录未落盘：runtime 侧无法判定，只有下游幂等能兜住 → W3 outbox 的动机"
            if expected_duplicate
            else "预期无重复副作用"
        ),
    }


def evaluate(result: dict[str, Any]) -> dict[str, Any]:
    expectation = classify(result["window"], result["tool_idem"])
    marker_ok = result.get("marker_window") == result["window"]
    resume_ok = result.get("resume_exit_code") == 0
    log_ok = bool(
        result.get("log_seq_contiguous")
        and result.get("log_event_ids_unique")
        and not result.get("invariant_violations")
    )
    duplicated = bool(result.get("duplicate_keys"))
    prediction_holds = duplicated == expectation["expected_duplicate"]
    return {
        **expectation,
        "marker_ok": marker_ok,
        "resume_ok": resume_ok,
        "log_ok": log_ok,
        "duplicated": duplicated,
        "prediction_holds": prediction_holds,
        "verdict": (
            "as-predicted"
            if marker_ok and resume_ok and log_ok and prediction_holds
            else "prediction-violated"
        ),
    }


def run_matrix(
    windows: tuple[str, ...] = W2_WINDOWS,
    repeats: int = 5,
    workroot: Path | None = None,
) -> list[dict[str, Any]]:
    root = workroot or Path(tempfile.mkdtemp(prefix="crash-matrix-"))
    rows: list[dict[str, Any]] = []
    for window in windows:
        for dedup in (True, False):
            for tool_idem in (True, False):
                for index in range(repeats):
                    workdir = root / f"{window}-dedup{int(dedup)}-idem{int(tool_idem)}-{index}"
                    if workdir.exists():
                        shutil.rmtree(workdir)
                    result = run_once(
                        window=window, dedup=dedup, tool_idem=tool_idem, workdir=workdir
                    )
                    rows.append({**result, **evaluate(result), "workdir": str(workdir)})
    return rows


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cells: dict[tuple[str, bool, bool], list[dict[str, Any]]] = {}
    for row in rows:
        cells.setdefault((row["window"], row["dedup"], row["tool_idem"]), []).append(row)
    summary: list[dict[str, Any]] = []
    for (window, dedup, tool_idem), group in sorted(cells.items()):
        runs = len(group)
        summary.append(
            {
                "window": window,
                "dedup": dedup,
                "tool_idem": tool_idem,
                "runs": runs,
                "marker_ok": f"{sum(r['marker_ok'] for r in group)}/{runs}",
                "resume_ok": f"{sum(r['resume_ok'] for r in group)}/{runs}",
                "log_ok": f"{sum(r['log_ok'] for r in group)}/{runs}",
                "duplicated_runs": f"{sum(r['duplicated'] for r in group)}/{runs}",
                "max_effects_per_key": max(r["max_effects_per_key"] for r in group),
                "expected_duplicate": group[0]["expected_duplicate"],
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
        "| 窗口 | runtime 去重 | 下游幂等 | 次数 | 标记命中 | 恢复成功 | 日志一致 |"
        " 出现重复副作用 | 单键最大效果数 | 与预期一致 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in summary:
        lines.append(
            "| {window} | {dedup} | {idem} | {runs} | {marker_ok} | {resume_ok} | {log_ok} |"
            " {duplicated_runs} | {max_effects_per_key} | {verdict} |".format(
                window=cell["window"],
                dedup="on" if cell["dedup"] else "off",
                idem="on" if cell["tool_idem"] else "off",
                runs=cell["runs"],
                marker_ok=cell["marker_ok"],
                resume_ok=cell["resume_ok"],
                log_ok=cell["log_ok"],
                duplicated_runs=cell["duplicated_runs"],
                max_effects_per_key=cell["max_effects_per_key"],
                verdict=cell["verdict"],
            )
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="experiments.crash_matrix")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--windows", default=",".join(W2_WINDOWS))
    parser.add_argument("--workroot", default="")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--md-out", default="")
    args = parser.parse_args(argv)

    for window in args.windows.split(","):
        if window not in WINDOWS:
            raise SystemExit(f"unknown window: {window!r}")
    windows = tuple(w for w in args.windows.split(",") if w)

    rows = run_matrix(
        windows=windows,
        repeats=args.repeats,
        workroot=Path(args.workroot) if args.workroot else None,
    )
    summary = summarize(rows)
    markdown = render_markdown(summary)
    print(markdown)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"summary": summary, "runs": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if args.md_out:
        Path(args.md_out).write_text(markdown + "\n", encoding="utf-8")
    violated = [cell for cell in summary if cell["verdict"] != "as-predicted"]
    return 1 if violated else 0


if __name__ == "__main__":
    sys.exit(main())
