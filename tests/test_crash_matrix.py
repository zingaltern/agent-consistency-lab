"""崩溃矩阵小样本（真实 SIGKILL 子进程）：只跑主对照两格 + 篡改控制组。

完整矩阵（16 格 × N 次）由 CLI 运行：
``python -m experiments.crash_matrix --repeats 5``
"""

from __future__ import annotations

from pathlib import Path

from experiments.crash_matrix import (
    PHASE_PLANS,
    Cell,
    build_cells,
    expectation,
    run_cell,
    summarize,
)

WINDOW = "post_tool_effect_pre_record"


def test_primary_contrast_outbox_vs_baseline(tmp_path: Path) -> None:
    baseline = run_cell(Cell(WINDOW, outbox=False, tool_idem=False), 1, tmp_path / "a")
    with_outbox = run_cell(Cell(WINDOW, outbox=True, tool_idem=False), 1, tmp_path / "b")

    base = summarize(baseline)[0]
    fixed = summarize(with_outbox)[0]

    assert base["verdict"] == "as-predicted"
    assert base["duplicated"] == "1/1"
    assert base["max_per_key"] == 2

    assert fixed["verdict"] == "as-predicted"
    assert fixed["duplicated"] == "0/1"
    assert fixed["max_per_key"] == 1
    assert fixed["reconciled"] == 1  # 探针真的做了对账，而不是"什么都没发生"


def test_tamper_control_group_produces_no_effect(tmp_path: Path) -> None:
    rows = run_cell(Cell("(tamper)", outbox=True, tool_idem=False, tamper=True), 1, tmp_path)
    summary = summarize(rows)[0]
    assert summary["verdict"] == "as-predicted"
    assert summary["effects"] == 0
    assert rows[0]["tool_result_statuses"].get("rejected") == 1


def test_no_probe_turns_duplicate_into_unknown(tmp_path: Path) -> None:
    rows = run_cell(Cell(WINDOW, outbox=True, tool_idem=False, probe=False), 1, tmp_path)
    summary = summarize(rows)[0]
    assert summary["verdict"] == "as-predicted"
    assert summary["duplicated"] == "0/1"
    assert summary["unknown_rows"] == 1
    assert summary["effects"] == 1


def test_every_window_has_a_phase_plan() -> None:
    for cell in build_cells():
        assert cell.window in PHASE_PLANS
        assert PHASE_PLANS[cell.window][0][0] == "run"
        assert PHASE_PLANS[cell.window][-1] == ("resume", "")


def test_cells_cover_all_declared_windows() -> None:
    windows = {cell.window for cell in build_cells()}
    for window in (
        "pre_tool_exec",
        "post_tool_effect_pre_record",
        "post_record_pre_commit",
        "post_approval_pre_exec",
        "after_resume",
        "during_compaction",
    ):
        assert window in windows


def test_expectation_marks_baseline_duplicate() -> None:
    assert expectation(Cell(WINDOW, outbox=False, tool_idem=False))["expect_duplicate"] is True
    assert expectation(Cell(WINDOW, outbox=True, tool_idem=False))["expect_duplicate"] is False
    assert (
        expectation(Cell("pre_tool_exec", outbox=False, tool_idem=False))["expect_duplicate"]
        is False
    )
