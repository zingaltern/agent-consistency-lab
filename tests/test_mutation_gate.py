"""变异门禁的"门禁自检"：基线与统计口径本身也要被钉住。

变异测试跑一次要几分钟（1642 个变异体 × 一遍测试集），不可能进默认测试集。所以这里
只测**判定逻辑与入库基线**这两件便宜但关键的事：

1. `mutation_summary` 的计数与幸存率算法（幸存率的分母只含"被判定的变异体"，
   `no tests` 是另一类盲区，混进分母会让数字好看）；
2. 入库基线 `reports/mutation_baseline.json` 的形状与自洽性（计数与列表长度一致、
   幸存率与计数自洽）——基线一旦被手改坏，判定就会失效或假绿。

真实的变异运行与"新增幸存变异 ⇒ 变红"由 nightly job `mutation` 执行，
退化注入验证记录在 `reports/mutation_baseline.json` 的生成 PR 里
（把基线里的一条删掉 → 退出 1 且打印 diff；还原 → 退出 0）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE = PROJECT_ROOT / "reports" / "mutation_baseline.json"
SCRIPT = PROJECT_ROOT / "scripts" / "mutation_check.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("mutation_check", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mutation_summary_counts_and_rate() -> None:
    module = _load_script()
    summary = module.mutation_summary(
        {
            "killed": ["a", "b", "c"],
            "survived": ["d"],
            "no tests": ["e", "f"],
        }
    )
    assert summary["killed"] == 3
    assert summary["survived"] == ["d"]
    assert summary["no_tests"] == ["e", "f"]
    assert summary["total"] == 6
    # 分母只含 killed + survived（no tests 不混进去）
    assert summary["survivor_rate"] == pytest.approx(1 / 4)


def test_mutation_summary_handles_empty_buckets() -> None:
    module = _load_script()
    summary = module.mutation_summary({})
    assert summary["survivor_rate"] == 0.0
    assert summary["total"] == 0


@pytest.mark.skipif(not BASELINE.exists(), reason="基线尚未生成（跑 --update-baseline）")
def test_baseline_is_internally_consistent() -> None:
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    counts = payload["counts"]
    assert counts["survived"] == len(payload["survivors"])
    assert counts["no_tests"] == len(payload["no_tests"])
    assert counts["total"] == counts["killed"] + counts["survived"] + counts["no_tests"]
    decided = counts["killed"] + counts["survived"]
    assert payload["survivor_rate"] == pytest.approx(
        round(counts["survived"] / decided, 4), abs=1e-4
    )
    assert payload["modules"] == [
        "harness/loop.py",
        "harness/store/checkpoints.py",
        "harness/approval.py",
    ]
    assert len(set(payload["survivors"])) == len(payload["survivors"]), "幸存清单里有重复项"


@pytest.mark.skipif(not BASELINE.exists(), reason="基线尚未生成（跑 --update-baseline）")
def test_baseline_records_the_boundary_of_what_it_can_see() -> None:
    """基线必须写明它看不见什么——否则会有人把幸存率当绝对质量分。"""
    note = json.loads(BASELINE.read_text(encoding="utf-8"))["note"]
    assert "子进程" in note
    assert "测试盲区" in note


def test_script_reports_timeout_as_failure() -> None:
    """超时必须判失败：跑不完 ≠ 没有回归（docs/testing.md §2 的退出码纪律）。"""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "这是**失败**而不是跳过" in source
    assert "TimeoutExpired" in source
