"""崩溃矩阵小样本（真实 SIGKILL 子进程）：只跑窗口 2 × 下游幂等开关。

完整矩阵（3 窗口 × 2 去重 × 2 幂等 × N 次）由 CLI 运行：
``python -m experiments.crash_matrix --repeats 5``
"""

from __future__ import annotations

from experiments.crash_matrix import classify, evaluate, run_matrix, summarize

WINDOW = "post_tool_effect_pre_record"


def test_matrix_smoke_window2() -> None:
    rows = run_matrix(windows=(WINDOW,), repeats=2)
    assert len(rows) == 8  # 2 去重 × 2 幂等 × 2 次

    summary = summarize(rows)
    by_cell = {(c["window"], c["dedup"], c["tool_idem"]): c for c in summary}

    for dedup in (True, False):
        idem_cell = by_cell[(WINDOW, dedup, True)]
        non_idem_cell = by_cell[(WINDOW, dedup, False)]
        # 通用断言：注入点命中、恢复成功、日志一致
        for cell in (idem_cell, non_idem_cell):
            assert cell["marker_ok"] == "2/2"
            assert cell["resume_ok"] == "2/2"
            assert cell["log_ok"] == "2/2"
            assert cell["verdict"] == "as-predicted"
        # 本矩阵的核心结论：下游幂等决定该窗口是否产生重复副作用
        assert idem_cell["duplicated_runs"] == "0/2"
        assert idem_cell["max_effects_per_key"] == 1
        assert non_idem_cell["duplicated_runs"] == "2/2"
        assert non_idem_cell["max_effects_per_key"] == 2


def test_classify_marks_window2_without_downstream_idempotency() -> None:
    assert classify(WINDOW, tool_idem=False)["expected_duplicate"] is True
    assert classify(WINDOW, tool_idem=True)["expected_duplicate"] is False
    assert classify("pre_tool_exec", tool_idem=False)["expected_duplicate"] is False


def test_evaluate_flags_prediction_violation() -> None:
    result = {
        "window": WINDOW,
        "tool_idem": False,
        "marker_window": WINDOW,
        "resume_exit_code": 0,
        "log_seq_contiguous": True,
        "log_event_ids_unique": True,
        "invariant_violations": [],
        "duplicate_keys": {},
    }
    verdict = evaluate(result)
    assert verdict["expected_duplicate"] is True
    assert verdict["duplicated"] is False
    assert verdict["prediction_holds"] is False
    assert verdict["verdict"] == "prediction-violated"
