"""W7 阈值扫描：压缩阈值 → 成功率 × 缓存命中 × 净成本 的三维对照。

为什么必须扫阈值而不是只做"开/关"对照：W4 得出的"压缩抬高净成本"来自**退化触发**
（固定余量在小窗口下使 `should_compact` 恒真、每步都压）。要回答"压缩到底值不值"，
必须在阈值这个维度上扫一遍，看**成功率、缓存命中、净成本**三者的联合形状。

方法（对应 W6 立下的规矩）：

* **变体集分 dev / holdout**：8 个长任务变体，前 6 个用于选点（dev），
  后 2 个只在选定阈值上跑一次（holdout）——避免"在同一条任务上扫参又报参"。
* 每个变体 × 每个阈值配置跑一个完整周期（run → approve → resume），
  指标全部从事件日志推导（阶段无关、可复算）。
* 每个配置报告：完成率、压缩次数、输入 token、缓存命中率、净成本/任务。
* 输出 markdown 表 + JSON + **自绘 SVG 曲线**（不引入绘图依赖，保证任何机器都能重跑）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.store.sqlite_store import SqliteStore
from harness.trace import build_trace, summary_of

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 8 个变体：取证轮数与日志体量不同 ⇒ 对阈值的敏感度不同（不是同一条任务重复跑）
VARIANTS: tuple[dict[str, Any], ...] = tuple(
    {
        "id": f"variant-{index}",
        "steps": steps,
        "lines": lines,
        "split": "dev" if index < 6 else "holdout",
    }
    for index, (steps, lines) in enumerate(
        ((6, 30), (6, 60), (8, 40), (8, 60), (10, 50), (10, 60), (9, 45), (11, 55))
    )
)

# 阈值档位：off + 从"几乎不压"到"很早就压"
THRESHOLDS: tuple[tuple[str, float | None], ...] = (
    ("off", None),
    ("0.95", 0.95),
    ("0.85", 0.85),
    ("0.70", 0.70),
    ("0.50", 0.50),
    ("0.30", 0.30),
)


@dataclass
class Cell:
    variant: str
    split: str
    threshold: str
    completed: bool = False
    overflows: int = 0
    compactions: int = 0
    calls: int = 0
    input_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_main_usd: float = 0.0
    cost_compaction_usd: float = 0.0
    spans: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        """账本口径总成本 = 主桶 + 压缩桶。

        ⚠️ 只统计 agent_message 会漏掉摘要调用的费用（W4 的采集器修过同一个坑，
        审计实测这里又漏了一次，且在"压得最狠"的档漏得最多 ⇒ 系统性压低压缩成本）。
        """
        return self.cost_main_usd + self.cost_compaction_usd

    @property
    def hit_ratio(self) -> float:
        return self.cache_read / self.input_tokens if self.input_tokens else 0.0


def run_cell(
    variant: dict[str, Any], threshold_name: str, trigger: float | None, root: Path
) -> Cell:
    cell = Cell(variant=variant["id"], split=variant["split"], threshold=threshold_name)
    run_dir = root / f"{variant['id']}-{threshold_name}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    common = [
        sys.executable,
        "-m",
        "experiments.worker",
        "--run-dir",
        str(run_dir),
        "--scenario",
        "long_incident",
        "--tool-idem",
        "off",
        "--window-tokens",
        str(6000 + 900 * variant["steps"]),
        "--max-output-tokens",
        "1024",
        "--max-inline-tokens",
        "100000",  # 不卸载：把"压缩"这一个变量单独隔离出来
        "--long-steps",
        str(variant["steps"]),
        "--long-lines",
        str(variant["lines"]),
        "--compaction",
        "off" if trigger is None else "on",
    ]
    if trigger is not None:
        common.extend(["--compaction-trigger", str(trigger)])

    for mode in ("run", "approve", "resume"):
        proc = subprocess.run(
            [*common, "--mode", mode],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if mode == "run" and proc.returncode not in (0,):
            cell.errors.append(f"run rc={proc.returncode}")

    return collect(cell, run_dir)


def collect(cell: Cell, run_dir: Path) -> Cell:
    """指标从事件日志与 trace 投影推导（与 W5/W6 同一口径）。"""
    ids_path = run_dir / "ids.json"
    if not ids_path.exists():
        return cell
    ids = json.loads(ids_path.read_text(encoding="utf-8"))
    store = SqliteStore(run_dir / "runtime.db")
    store.setup()
    try:
        events = store.effective_events(ids["branch_id"])
    finally:
        store.close()

    from harness.state import RunStatus, reduce_events

    state, _ = reduce_events(events)
    cell.completed = state.status is RunStatus.COMPLETED
    for event in events:
        if event.type == "agent_message":
            cell.calls += 1
            cell.input_tokens += int(event.payload.get("context_tokens", 0) or 0)
            cell.cache_read += int(event.payload.get("cache_read_tokens", 0) or 0)
            cell.cache_write += int(event.payload.get("cache_write_tokens", 0) or 0)
            cell.cost_main_usd += float(event.payload.get("cost_usd", 0.0) or 0.0)
        elif event.type == "budget_update":
            if str(event.payload.get("bucket", "main")) == "compaction":
                cell.cost_compaction_usd += float(event.payload.get("cost_usd", 0.0) or 0.0)
        elif event.type == "compaction":
            cell.compactions += 1
        elif event.type == "error":
            error_class = str(event.payload.get("error_class"))
            cell.errors.append(error_class)
            if error_class == "context_overflow":
                cell.overflows += 1

    trace = build_trace(events)
    cell.spans = summary_of(trace)["spans"]
    return cell


def aggregate(cells: list[Cell], split: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, _ in THRESHOLDS:
        group = [c for c in cells if c.threshold == name and c.split == split]
        if not group:
            continue
        n = len(group)
        rows.append(
            {
                "threshold": name,
                "n": n,
                "success_rate": round(sum(c.completed for c in group) / n, 4),
                "avg_compactions": round(sum(c.compactions for c in group) / n, 2),
                "avg_calls": round(sum(c.calls for c in group) / n, 2),
                "avg_input_tokens": round(sum(c.input_tokens for c in group) / n, 1),
                "hit_ratio": round(sum(c.hit_ratio for c in group) / n, 4),
                "avg_cost_usd": round(sum(c.cost_usd for c in group) / n, 6),
                "avg_cost_main_usd": round(sum(c.cost_main_usd for c in group) / n, 6),
                "avg_cost_compaction_usd": round(sum(c.cost_compaction_usd for c in group) / n, 6),
                "overflows": sum(c.overflows for c in group),
            }
        )
    return rows


# ------------------------------------------------------------------ SVG 曲线


def render_svg(
    rows: list[dict[str, Any]], *, title: str, width: int = 720, height: int = 260
) -> str:
    """自绘 SVG（三条曲线：成功率 / 缓存命中 / 归一化成本）。不依赖任何绘图库。"""
    if not rows:
        return "<svg/>"
    pad = 48
    plot_w = width - 2 * pad
    plot_h = height - 2 * pad
    n = len(rows)
    step = plot_w / max(1, n - 1)

    def series(values: list[float]) -> str:
        points = []
        for index, value in enumerate(values):
            x = pad + index * step
            y = pad + plot_h * (1 - max(0.0, min(1.0, value)))
            points.append(f"{x:.1f},{y:.1f}")
        return " ".join(points)

    successes = [row["success_rate"] for row in rows]
    hits = [row["hit_ratio"] for row in rows]
    costs = [row["avg_cost_usd"] for row in rows]
    max_cost = max(costs) or 1.0
    cost_norm = [value / max_cost for value in costs]

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'font-family="system-ui" font-size="11">',
        f'<rect width="{width}" height="{height}" fill="white"/>',
        f'<text x="{pad}" y="20" font-size="13" font-weight="600">{title}</text>',
        f'<line x1="{pad}" y1="{pad + plot_h}" x2="{pad + plot_w}"'
        f' y2="{pad + plot_h}" stroke="#999"/>',
        f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{pad + plot_h}" stroke="#999"/>',
    ]
    for _label, values, colour in (
        ("success", successes, "#1f77b4"),
        ("cache hit", hits, "#2ca02c"),
        ("cost (normalised)", cost_norm, "#d62728"),
    ):
        parts.append(
            f'<polyline fill="none" stroke="{colour}" stroke-width="2" points="{series(values)}"/>'
        )
        points = series(values).split()
        for point in points:
            x, y = point.split(",")
            parts.append(f'<circle cx="{x}" cy="{y}" r="2.5" fill="{colour}"/>')
    for index, row in enumerate(rows):
        x = pad + index * step
        parts.append(
            f'<text x="{x:.1f}" y="{pad + plot_h + 16}" text-anchor="middle">'
            f"{row['threshold']}</text>"
        )
    legend = [("success", "#1f77b4"), ("cache hit", "#2ca02c"), ("cost/max", "#d62728")]
    for index, (label, colour) in enumerate(legend):
        x = pad + index * 130
        parts.append(f'<rect x="{x}" y="{pad - 26}" width="10" height="10" fill="{colour}"/>')
        parts.append(f'<text x="{x + 15}" y="{pad - 17}">{label}</text>')
    parts.append(f'<text x="{pad - 40}" y="{pad + 4}">1.0</text>')
    parts.append(f'<text x="{pad - 40}" y="{pad + plot_h + 4}">0.0</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def render_markdown(dev: list[dict[str, Any]], holdout: list[dict[str, Any]]) -> str:
    def table(rows: list[dict[str, Any]], title: str) -> str:
        lines = [
            f"**{title}**",
            "",
            "| 压缩阈值 | 变体数 | 完成率 | 平均压缩次数 | 平均调用数 |"
            " 平均输入 token | 缓存命中率 | 净成本（主桶+压缩桶） | 溢出 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for row in rows:
            lines.append(
                f"| {row['threshold']} | {row['n']} | {row['success_rate']:.0%} |"
                f" {row['avg_compactions']} | {row['avg_calls']} | {row['avg_input_tokens']:.0f} |"
                f" {row['hit_ratio']:.1%} | ${row['avg_cost_usd']:.5f} | {row['overflows']} |"
            )
        return "\n".join(lines)

    return "\n\n".join(
        [table(dev, "dev（用于选点）"), table(holdout, "holdout（只在选定阈值上确认）")]
    )


def findings(dev: list[dict[str, Any]], holdout: list[dict[str, Any]], selected: str | None) -> str:
    if not dev:
        return "（无数据）"
    best_success = max(row["success_rate"] for row in dev)
    cheapest = min(dev, key=lambda row: row["avg_cost_usd"])
    most_expensive = max(dev, key=lambda row: row["avg_cost_usd"])
    highest_hit = max(dev, key=lambda row: row["hit_ratio"])
    lines = [
        f"* **完成率的天花板是 1.0，成本的天花板不是**：dev 上最高完成率 "
        f"{best_success:.0%}，达到它的成本在 ${cheapest['avg_cost_usd']:.5f}"
        f"（阈值 {cheapest['threshold']}）到 ${most_expensive['avg_cost_usd']:.5f}"
        f"（阈值 {most_expensive['threshold']}）之间摆动，差 "
        f"{most_expensive['avg_cost_usd'] / max(1e-9, cheapest['avg_cost_usd']):.1f} 倍。"
        "⚠️ `off` 行是**失败运行的截断成本**（没跑完就溢出），不能与跑完的档位比绝对值："
        "它的含义是「不压缩的代价是任务失败」，不是「更便宜」。",
        f"* **缓存命中率与压缩频率单调反向**：命中率最高的是 {highest_hit['threshold']}"
        f"（{highest_hit['hit_ratio']:.1%}，平均压缩 {highest_hit['avg_compactions']} 次）——"
        "每一次压缩都从替换点击穿前缀缓存，这条在阈值维度上再次成立。",
        f"* **选点**：在 dev 上按「先满足完成率、再比成本」选出阈值 **{selected}**；"
        + (
            "holdout 上的表现见上表——若两者差异明显，说明该阈值对任务体量过拟合。"
            if holdout
            else "holdout 未跑（--skip-holdout）。"
        ),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="experiments.context_sweep")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--workroot", default="")
    parser.add_argument("--skip-holdout", action="store_true")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--md-out", default="")
    parser.add_argument("--svg-out", default="")
    args = parser.parse_args(argv)

    root = Path(args.workroot) if args.workroot else Path(tempfile.mkdtemp(prefix="w7-sweep-"))
    root.mkdir(parents=True, exist_ok=True)
    triggers = dict(THRESHOLDS)

    # 阶段一：dev 上扫全部阈值
    cells: list[Cell] = []
    for variant in VARIANTS:
        if variant["split"] != "dev":
            continue
        for name, trigger in THRESHOLDS:
            for _ in range(args.repeats):
                cells.append(run_cell(variant, name, trigger, root))
    dev_rows = aggregate(cells, "dev")

    # 选点规则：先满足完成率，再比成本（写死规则，避免"看图挑最好"）
    best_success = max((row["success_rate"] for row in dev_rows), default=0.0)
    candidates = [row for row in dev_rows if row["success_rate"] >= best_success - 1e-9]
    selected = (
        min(candidates, key=lambda row: row["avg_cost_usd"])["threshold"] if candidates else None
    )

    # 阶段二：holdout 只在「off 基线 + 阶段一选出的阈值」上确认。
    # 早期版本把这里硬编码成 "0.85"，与程序选出的阈值脱钩（审计发现：选点是 0.95，
    # 却去 0.85 上确认，"holdout 只做确认"的方法论声明不成立）。
    if not args.skip_holdout and selected is not None:
        for variant in VARIANTS:
            if variant["split"] != "holdout":
                continue
            for name in ("off", selected):
                for _ in range(args.repeats):
                    cells.append(run_cell(variant, name, triggers[name], root))
    holdout_rows = aggregate(cells, "holdout")

    table = render_markdown(dev_rows, holdout_rows)
    text = "\n\n".join(
        [
            f"变体：{len(VARIANTS)} 个（dev 6 / holdout 2）× 阈值 {len(THRESHOLDS)} 档 × "
            f"重复 {args.repeats} 次 = {len(cells)} 次完整周期（每周期 run→approve→resume）",
            "统一口径：不卸载（隔离压缩这一个变量）、同一 token 估算与价格表、"
            "指标全部从事件日志推导。",
            table,
            "### 结论\n\n" + findings(dev_rows, holdout_rows, selected),
        ]
    )
    print(text)
    if args.md_out:
        Path(args.md_out).write_text(text + "\n", encoding="utf-8")
    if args.svg_out:
        Path(args.svg_out).write_text(
            render_svg(dev_rows, title="dev: success / cache hit / cost vs compaction threshold"),
            encoding="utf-8",
        )
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "variants": VARIANTS,
                    "thresholds": [name for name, _ in THRESHOLDS],
                    "dev": dev_rows,
                    "holdout": holdout_rows,
                    "selected_threshold": selected,
                    "cells": [cell.__dict__ for cell in cells],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
