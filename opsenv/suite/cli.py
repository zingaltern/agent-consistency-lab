"""命令行入口：`python -m opsenv.suite`（门面 `opsenv.suite` 也 re-export 了 `main`）。

参数、默认值、退出码语义与拆分前逐字相同——**不得**在这里改任何判据或阈值。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from ..policy import NoiseFlavor
from ..scenario import build_catalog, summarise
from ..stats import min_detectable_effect, paired_min_detectable_effect
from ..systems import SYSTEMS, LazyOperator, Operator
from .gates import check_gates, render_gates
from .report import (
    findings,
    grader_sensitivity,
    proxy_section,
    render_markdown,
    render_reasoner_banner,
    split_breakdown,
    statistical_notes,
)
from .run import _live_profiles, aggregate, noisy_profiles, run_suite
from .stats import discordant_pairs, grader_rates, paired_compare


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opsenv.suite")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--per-fault", type=int, default=8)
    parser.add_argument("--split", choices=("all", "dev", "holdout"), default="all")
    parser.add_argument("--systems", default=",".join(SYSTEMS))
    parser.add_argument(
        "--operator",
        choices=("oracle", "lazy"),
        default="oracle",
        help="值班人模型：oracle=拒绝破坏性动作；lazy=橡皮图章（消融用）",
    )
    parser.add_argument(
        "--reasoner",
        choices=("default", "noisy"),
        default="default",
        help="推理器口径：default=既有两个人格（零改变）；noisy=整体替换为噪声人格（独立报告）",
    )
    parser.add_argument(
        "--error-rate", type=float, default=0.3, help="噪声人格的误判概率（noisy 口径）"
    )
    parser.add_argument(
        "--noise-flavor",
        choices=tuple(item.value for item in NoiseFlavor),
        default=NoiseFlavor.BOTH.value,
        help="误判形态：wrong_diagnosis / diagnosis_ok_action_wrong / both",
    )
    parser.add_argument("--workroot", default="")
    parser.add_argument("--json-out", default="", help="汇总 JSON（入库）")
    parser.add_argument(
        "--runs-out", default="", help="逐次运行明细 JSON（体积大，不入库，可一条命令再生）"
    )
    parser.add_argument("--md-out", default="")
    parser.add_argument(
        "--gate",
        action="store_true",
        help="启用门禁：任一条不通过则以非零码退出（CI 用）",
    )
    args = parser.parse_args(argv)

    if args.per_fault < 1:
        print("--per-fault 必须 ≥ 1（否则没有任何场景，门禁会空真通过）")
        return 2
    catalog = build_catalog(per_fault=args.per_fault)
    if args.split != "all":
        catalog = [scenario for scenario in catalog if scenario.split == args.split]
    systems = tuple(name for name in args.systems.split(",") if name)
    operator = Operator() if args.operator == "oracle" else LazyOperator()
    noisy = args.reasoner == "noisy"
    profiles = (
        noisy_profiles(error_rate=args.error_rate, flavor=NoiseFlavor(args.noise_flavor))
        if noisy
        else _live_profiles()
    )
    results = run_suite(
        catalog=catalog,
        systems=systems,
        profiles=profiles,
        repeats=args.repeats,
        workroot=Path(args.workroot) if args.workroot else None,
        operator=operator,
    )
    cells = aggregate(results)
    summary = summarise(catalog)
    # 分池判据自己从 results 派生每池的 cells（`cells` 仍给报告用）
    gates = check_gates(results, catalog_summary=summary, noisy=noisy)

    sections = [
        (
            render_reasoner_banner(
                noisy=noisy, error_rate=args.error_rate, flavor=args.noise_flavor
            )
            if noisy
            else ""
        ),
        render_markdown(cells, summary),
        "\n### 结论\n",
        findings(cells, results),
        "\n### 统计口径与可比性\n",
        statistical_notes(cells, results),
        "\n### 评分口径敏感性\n",
        grader_sensitivity(cells, results),
        "\n### dev / holdout 分段\n",
        split_breakdown(cells, results),
        "\n### 代理指标校准（能否用可观测指标替代 ground truth）\n",
        proxy_section(cells, results),
        "\n### 门禁\n",
        render_gates(gates),
    ]
    text = "\n".join(section for section in sections if section) + "\n"
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "catalog": summary,
                    # rates 是文档直接引用的比率（correct/red_line/... 都是计数），
                    # 物化进 JSON 才能被 claim 门禁对账（R-A1：不许对账脚本去猜派生值）
                    "cells": [{**cell.model_dump(), "rates": cell.rates()} for cell in cells],
                    "gates": [gate.model_dump() for gate in gates],
                    "reasoner": {
                        "mode": args.reasoner,
                        "error_rate": args.error_rate if noisy else 0.0,
                        "flavor": args.noise_flavor if noisy else "",
                        "note": (
                            "噪声口径：不得与核心 1536-run 主口径混排（HANDOFF §三.5"
                            "『推理器是受控变量』）"
                            if noisy
                            else "默认口径：与既有 W5/W6 结论同源"
                        ),
                    },
                    "grader_sensitivity": grader_rates(results),
                    "gate_summary": {
                        # 门禁条数是**结构性事实**（不随样本量变化），因此可以被轻 claim 对账；
                        # 它是"门禁条数固定"这条验收的机器可读载体（条数以 claim
                        # `suite-gate-count` 为准，正文不写绝对数）。
                        "total": len(gates),
                        "passed": sum(gate.ok for gate in gates),
                        "failed": sum(not gate.ok for gate in gates),
                    },
                    "statistics": {
                        # 文档里的"静态拒绝列表之外的新动作共 N 次"是**跨格合计**，
                        # 格子级计数无法表达它，因此在这里物化。
                        "novel_red_line_total": sum(cell.novel_red_line for cell in cells),
                        # MDE 有**三种口径**，历史上只物化了第一种，于是文档里三个数字打架
                        # （6.0% / 9.2% / 10.0%，且都自称"最小可检测效应"）。
                        # 三个都物化、各自带口径名：引用时必须写出用的是哪一个。
                        "min_detectable_effect_independent_p90": round(
                            min_detectable_effect(max(c.runs for c in cells), p=0.9), 4
                        ),
                        "min_detectable_effect_independent_p05": round(
                            min_detectable_effect(max(c.runs for c in cells), p=0.5), 4
                        ),
                        "min_detectable_effect_paired": round(
                            paired_min_detectable_effect(
                                discordant=discordant_pairs(
                                    results,
                                    system_a="harness",
                                    system_b="single_shot",
                                    predicate=lambda r: r.red_line,
                                    profile="weak-guesser",
                                ),
                                pairs=paired_compare(
                                    results,
                                    system_a="harness",
                                    system_b="single_shot",
                                    predicate=lambda r: r.red_line,
                                    profile="weak-guesser",
                                )[1],
                            ),
                            4,
                        ),
                        "paired_harness_vs_single_shot": {
                            "correct": asdict(
                                paired_compare(
                                    results,
                                    system_a="harness",
                                    system_b="single_shot",
                                    predicate=lambda r: r.correct,
                                    profile="weak-guesser",
                                )[0]
                            ),
                            "red_line": asdict(
                                paired_compare(
                                    results,
                                    system_a="harness",
                                    system_b="single_shot",
                                    predicate=lambda r: r.red_line,
                                    profile="weak-guesser",
                                )[0]
                            ),
                        },
                    },
                    "note": "逐次运行明细请用 --runs-out 生成（不入库，避免可再生的体积膨胀）",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    if args.runs_out:
        Path(args.runs_out).write_text(
            json.dumps([r.model_dump() for r in results], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if args.md_out:
        Path(args.md_out).write_text(text, encoding="utf-8")

    failed = [gate for gate in gates if not gate.ok and gate.enforced]
    reported = [gate for gate in gates if not gate.ok and not gate.enforced]
    if reported:
        print(
            f"噪声口径下如实记录的红色门禁（不决定退出码）{len(reported)} 条: "
            + ", ".join(gate.name for gate in reported)
        )
    if args.gate and failed:
        print(f"门禁失败 {len(failed)} 条: " + ", ".join(gate.name for gate in failed))
        return 1
    return 0
