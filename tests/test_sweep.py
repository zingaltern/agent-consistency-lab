"""W7 阈值扫描：聚合、选点规则与自绘 SVG（不跑全量扫描，只测纯函数 + 单格端到端）。"""

from __future__ import annotations

from pathlib import Path

from experiments.context_sweep import (
    THRESHOLDS,
    Cell,
    aggregate,
    findings,
    render_markdown,
    render_svg,
    run_cell,
)


def _cell(
    threshold: str, split: str, *, completed: bool, compactions: int, hit: float, cost: float
):
    cell = Cell(variant=f"v-{threshold}-{split}", split=split, threshold=threshold)
    cell.completed = completed
    cell.compactions = compactions
    cell.input_tokens = 10_000
    cell.cache_read = int(10_000 * hit)
    cell.calls = 10
    cell.cost_usd = cost
    return cell


def _dev_cells() -> list[Cell]:
    return [
        # off：便宜但跑不完
        _cell("off", "dev", completed=False, compactions=0, hit=0.68, cost=0.066),
        _cell("off", "dev", completed=True, compactions=0, hit=0.68, cost=0.066),
        # 0.85：能跑完、成本低
        _cell("0.85", "dev", completed=True, compactions=1, hit=0.53, cost=0.093),
        _cell("0.85", "dev", completed=True, compactions=2, hit=0.53, cost=0.093),
        # 0.30：能跑完但更贵、命中率塌
        _cell("0.30", "dev", completed=True, compactions=7, hit=0.07, cost=0.109),
        _cell("0.30", "dev", completed=True, compactions=6, hit=0.07, cost=0.109),
    ]


def test_aggregate_reports_success_hit_and_cost_together() -> None:
    rows = aggregate(_dev_cells(), "dev")
    by_threshold = {row["threshold"]: row for row in rows}
    assert by_threshold["off"]["success_rate"] == 0.5
    assert by_threshold["0.85"]["success_rate"] == 1.0
    assert by_threshold["off"]["hit_ratio"] > by_threshold["0.30"]["hit_ratio"]
    assert by_threshold["off"]["avg_cost_usd"] < by_threshold["0.30"]["avg_cost_usd"]
    assert by_threshold["0.30"]["avg_compactions"] == 6.5


def test_selection_rule_prefers_success_then_cost() -> None:
    """选点规则写死为「先满足完成率、再比成本」，不允许看图挑最好。"""
    rows = aggregate(_dev_cells(), "dev")
    best_success = max(row["success_rate"] for row in rows)
    candidates = [row for row in rows if row["success_rate"] >= best_success - 1e-9]
    selected = min(candidates, key=lambda row: row["avg_cost_usd"])["threshold"]
    assert selected == "0.85"  # off 更便宜但完成率不达标


def test_threshold_axis_is_ordered_from_off_to_aggressive() -> None:
    labels = [name for name, _ in THRESHOLDS]
    assert labels[0] == "off"
    values = [value for _, value in THRESHOLDS[1:]]
    assert values == sorted(values, reverse=True)


def test_svg_contains_three_series_and_axis_labels() -> None:
    svg = render_svg(aggregate(_dev_cells(), "dev"), title="t")
    assert svg.count("polyline") == 3
    for label in ("success", "cache hit", "cost/max"):
        assert label in svg
    for name in ("off", "0.85", "0.30"):
        assert f">{name}</text>" in svg


def test_svg_is_valid_xml() -> None:
    import xml.etree.ElementTree as ET

    svg = render_svg(aggregate(_dev_cells(), "dev"), title="t")
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")


def test_findings_state_the_tradeoff_and_the_selection() -> None:
    dev = aggregate(_dev_cells(), "dev")
    holdout = aggregate(
        [_cell("0.85", "holdout", completed=True, compactions=1, hit=0.6, cost=0.12)], "holdout"
    )
    text = findings(dev, holdout, "0.85")
    assert "0.85" in text
    assert "命中率" in text and "成本" in text


def test_markdown_renders_both_splits() -> None:
    dev = aggregate(_dev_cells(), "dev")
    holdout = aggregate(
        [_cell("off", "holdout", completed=False, compactions=0, hit=0.7, cost=0.1)], "holdout"
    )
    text = render_markdown(dev, holdout)
    assert "dev（用于选点）" in text and "holdout（只在选定阈值上确认）" in text


def test_single_cell_runs_end_to_end(tmp_path: Path) -> None:
    """一次真实扫描格：长任务变体 × 阈值 off（不应溢出）→ 指标从事件日志推导出来。"""
    cell = run_cell(
        {"id": "variant-t", "steps": 6, "lines": 30, "split": "dev"}, "off", None, tmp_path
    )
    assert cell.completed
    assert cell.calls > 0 and cell.input_tokens > 0
    assert cell.spans > 0
    assert cell.overflows == 0


def test_aggressive_threshold_compacts_and_loses_cache(tmp_path: Path) -> None:
    """同一变体压到 0.30：必须真的发生压缩，且缓存命中率低于不压缩。"""
    variant = {"id": "variant-t2", "steps": 8, "lines": 60, "split": "dev"}
    plain = run_cell(variant, "off", None, tmp_path)
    aggressive = run_cell(dict(variant, id="variant-t3"), "0.30", 0.30, tmp_path)
    assert aggressive.compactions >= 1
    assert aggressive.hit_ratio < plain.hit_ratio
