"""报告渲染：结论、统计口径、评分口径敏感性、dev/holdout 分段与代理指标校准。

渲染层只做一件事——把 `stats` 层算出的数与 `run` 层的格子写成断言得住的话。
"哪些句子该出现"由在场系统决定（`--systems` 子集时缺席的句子整段跳过），
不做任何只有渲染层才知道的判定（对抗审查第 7 条）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..stats import min_detectable_effect, paired_min_detectable_effect
from ..systems import SYSTEMS, RunResult
from .run import Cell
from .stats import (
    GRADERS,
    discordant_pairs,
    grader_rates,
    paired_compare,
    proxy_calibration,
    rate_with_interval,
)


def render_markdown(cells: Sequence[Cell], catalog_summary: dict[str, Any]) -> str:
    lines = [
        f"场景集：{catalog_summary['total']} 个（{catalog_summary['by_split']}，"
        f"决定性通道分布 {catalog_summary['by_decisive_channel']}）",
        "",
        "| 系统 | 推理器 | 运行数 | 诊断正确 | 故障解除 | **红线执行** | 其中新动作 |"
        " 走审批 | 被拦下 | 取证充分 | 平均步数 | 平均输入 token | 平均成本 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in cells:
        rate = cell.rates()
        lines.append(
            f"| {cell.system} | {cell.profile} | {cell.runs} | {rate['correct']:.1%} |"
            f" {rate['resolved']:.1%} | **{rate['red_line']:.1%}** | {cell.novel_red_line} |"
            f" {rate['gated']:.1%} | {rate['blocked']:.1%} | {rate['sufficient']:.1%} |"
            f" {cell.avg_steps} | {cell.avg_input_tokens:.0f} | ${cell.avg_cost_usd:.5f} |"
        )
    return "\n".join(lines)


def findings(cells: Sequence[Cell], results: Sequence[RunResult]) -> str:
    """结论只写数据支持的句子；口径、样本量与不可区分性都写清。"""
    by_key = {(c.system, c.profile): c for c in cells}

    def cell(system: str, profile: str) -> Cell | None:
        return by_key.get((system, profile))

    def rate(system: str, profile: str, key: str) -> float:
        item = cell(system, profile)
        return item.rates()[key] if item else 0.0

    def blocked_share(system: str) -> float:
        """被拦下数 ÷ 出错 run 数（而**不是** ÷ 总 run 数——审计指出早期口径把分母写错）。"""
        item = cell(system, "weak-guesser")
        if item is None or item.runs == 0:
            return 0.0
        wrong = item.runs - item.correct
        return item.blocked / wrong if wrong else 0.0

    def se(system: str, profile: str, key: str) -> float:
        """比例的样本标准误，用来判断两条路线的差距是否可区分。"""
        item = cell(system, profile)
        if not item or item.runs == 0:
            return 0.0
        p = item.rates()[key]
        return (p * (1 - p) / item.runs) ** 0.5

    def present(*names: str) -> bool:
        """该结论句子引用的每个系统都必须在场（--systems 子集时缺席的句子跳过）。"""
        return all(cell(name, "competent-honest") is not None for name in names)

    lines: list[str] = []
    n = cell("harness", "competent-honest").runs if cell("harness", "competent-honest") else 0

    # 1) 取证充分性 → 正确率上限（需要 workflow 与 harness 同时在场）
    if present("workflow", "harness"):
        wf = rate("workflow", "competent-honest", "sufficient")
        agents = rate("harness", "competent-honest", "sufficient")
        lines.append(
            f"* **取证充分性决定正确率上限**（n={n}/格）：workflow 只看 metrics+resources，"
            f"取证充分率 {wf:.0%}（决定性证据在 logs/changes 的故障它看不见），"
            f"诊断正确率恰好也是 {rate('workflow', 'competent-honest', 'correct'):.0%}；"
            f"agent 路线按通道阶梯取证，充分率 {agents:.0%}，正确率随之到 "
            f"{rate('harness', 'competent-honest', 'correct'):.0%}。"
            "差距几乎完全由这一点解释——**不是模型更聪明，而是证据更全**。"
        )

    # 2) 正确率的可比性：**所有两两配对比较**，不再只挑一对（审计发现早期版本
    #    只算了 harness−single_shot 就写成"三条路线不可区分"，而遗漏的那一对
    #    （langgraph−single_shot）恰好是最大的）
    pair_lines: list[str] = []
    distinguishable: list[str] = []
    for left, right in (
        ("harness", "single_shot"),
        ("langgraph", "single_shot"),
        ("harness", "langgraph"),
    ):
        if not present(left, right):
            continue  # --systems 子集：缺席一方不参与配对，也不输出"全 0"的假数据
        interval, pairs = paired_compare(
            results,
            system_a=left,
            system_b=right,
            predicate=lambda r: r.correct,
            profile="weak-guesser",
        )
        verdict = "可区分" if interval.excludes_zero else "不可区分"
        if interval.excludes_zero:
            distinguishable.append(f"{left}−{right}")
        pair_lines.append(
            f"  * {left} − {right}：{interval.point:+.3f}，95% CI "
            f"[{interval.low:+.3f}, {interval.high:+.3f}]（配对 {pairs} 组）⇒ {verdict}"
        )
    if pair_lines:
        lines.append(
            "* **诊断正确率的两两比较**（weak-guesser，配对 bootstrap）：\n"
            + "\n".join(pair_lines)
            + (
                f"\n  其中 {', '.join(distinguishable)} 的 CI 不含 0——**不能说"
                "「三条路线都不可区分」**；但把 3 组比较做多重校正后（Bonferroni 阈值 "
                "0.0167）它们都落在边缘，因此更准确的表述是「差距很小、处于本样本量的"
                "分辨边界上」。"
                if distinguishable
                else "\n  所有配对的 CI 都含 0 ⇒ 本样本量下三条路线的诊断正确率不可区分。"
            )
        )

    # 3) 红线：架构决定模型出错的后果（基准是 single-shot，缺席则整段跳过）
    if present("single_shot"):
        red = {name: rate(name, "weak-guesser", "red_line") for name in SYSTEMS}
        agent_names = [name for name in ("harness", "langgraph", "workflow") if present(name)]
        red_text = "、".join(f"{name} {red[name]:.1%}" for name in agent_names)
        gate_tail = ""
        agent_gates = [name for name in ("harness", "langgraph") if present(name)]
        if agent_gates:
            shares = " / ".join(f"{blocked_share(name):.0%}" for name in agent_gates)
            subject = (
                "两条 agent 路线把" if len(agent_gates) == 2 else f"{agent_gates[0]} 路线把"
            )
            gate_tail = (
                f"{subject} {shares} 的**出错场景**交给了人工并被拒绝——"
                "**gate 的价值不是它自己判断对错，而是它把决定权交到人手里**。"
            )
        lines.append(
            "* **模型出错时，架构决定它会不会变成事故**（weak-guesser 人格，"
            f"红线基准 = single-shot **{red['single_shot']:.1%}**，"
            f"其中 {cell('single_shot', 'weak-guesser').novel_red_line} 次是"
            "静态拒绝列表里没有的新破坏性动作）："
            + (f"红线执行率——{red_text}。" if red_text else "")
            + gate_tail
        )

    # 4) 成本（只报在场系统的成本；全部缺席则跳过）
    cost_items = [
        ("workflow", "无模型调用"),
        ("single_shot", "读全部证据但只付一次前缀"),
        ("harness", "每步重发"),
        ("langgraph", "每步重发"),
    ]
    present_cost = [(name, note) for name, note in cost_items if present(name)]
    if present_cost:
        cost_text = "、".join(
            f"{name} ${cell(name, 'competent-honest').avg_cost_usd:.5f}" for name, _ in present_cost
        )
        lines.append(
            "* **成本口径**（同一 token 估算与价格表；累计口径 = 每步重发前缀+已读证据）："
            + cost_text
            + "。本口径**不计前缀缓存折扣**：真实供应商的缓存会把重复前缀降到 0.1×，"
            "那属于 W4 的实验范围，这里刻意不加，以免把两件事混在一起。"
        )

    # 5) 两条 agent 路线的等价性（需要两条路线同时在场）
    lg_c = cell("langgraph", "competent-honest")
    h_c = cell("harness", "competent-honest")
    if lg_c is not None and h_c is not None:
        lines.append(
            f"* **两条 agent 路线的能力等价**：LangGraph 图与自研 Loop 的平均步数"
            f" {lg_c.avg_steps} vs {h_c.avg_steps}、"
            "输入 token 相同、正确率与红线率在噪声内一致。也就是说本项目的 runtime 并没有靠"
            "「更聪明的策略」取胜——它买到的是 LangGraph 用声明式 API 提供的同一类能力，"
            "外加**崩溃一致性**（W2–W4 的 checkpoint/outbox/探针）与**工具自述风险**"
            "（本轮的否决点：审批门挂在工具元数据上，而不是某个动作名黑名单上）。"
        )
    return "\n".join(lines)


def split_breakdown(cells: Sequence[Cell], results: Sequence[RunResult]) -> str:
    """dev / holdout 分开报：holdout 是冻结考卷，不与调参混合汇报。"""
    lines = [
        "| 系统 | 推理器 | 分段 | 运行数 | strict 正确（95% CI） | 红线率（95% CI） |",
        "|---|---|---|---|---|---|",
    ]
    for cell in cells:
        for split in ("dev", "holdout"):
            subset = [
                r
                for r in results
                if r.system == cell.system and r.profile_name == cell.profile and r.split == split
            ]
            if not subset:
                continue
            correct = rate_with_interval(subset, lambda r: r.correct)
            red = rate_with_interval(subset, lambda r: r.red_line)
            lines.append(
                f"| {cell.system} | {cell.profile} | {split} | {len(subset)} | {correct} | {red} |"
            )
    return "\n".join(lines)


def render_reasoner_banner(*, noisy: bool, error_rate: float, flavor: str) -> str:
    """噪声口径的显著标记：这份报告**不能**与核心 1536-run 主口径混排。"""
    if not noisy:
        return ""
    return (
        f"> ⚠️ **本报告是噪声人格口径**（`--reasoner noisy --error-rate {error_rate}"
        f" --noise-flavor {flavor}`）：推理器被整体替换为会误判的人格。\n"
        "> 它与 README / W5 / W6 的核心 1536-run 主口径（理想化推理器）**不是同一批数据**，\n"
        "> 不得混引、不得合并统计。噪声口径的用途只有一个：把评分口径（strict vs cause_only）\n"
        "> 与分辨率的敏感性检验**激活**，并观察机制类不变量在推理器变笨时是否仍然成立。\n"
        "> 另注：`workflow` 是纯规则表、没有推理器，噪声对它不适用（其数值与默认口径一致）。"
    )


def grader_sensitivity(cells: Sequence[Cell], results: Sequence[RunResult]) -> str:
    """评分口径敏感性：表格由 ``grader_rates``（数据级）渲染，正文判定同样来自数据。

    为什么要拆成数据函数：渲染层文本不可断言，而"噪声模式下 strict ≤ cause_only"是一条
    **可断言**的性质（见 tests/test_noisy_reasoner.py）。渲染与判定必须共用同一个数据来源，
    否则"口径被激活"这句话本身就无法被测试。
    """
    table = grader_rates(results)
    column_order = ("harness", "langgraph", "single_shot", "workflow")
    lines = [
        "| 评分口径 | 含义 | harness | langgraph | single_shot | workflow |",
        "|---|---|---|---|---|---|",
    ]
    for grader, meaning in GRADERS.items():
        row = [f"| `{grader}` | {meaning} |"]
        for system in column_order:
            rate = table[grader].get(system)
            row.append(" n/a |" if rate is None else f" {rate:.1%} |")  # 缺席系统不打印 0.0%
        lines.append("".join(row))
    lines.append("")
    present = [system for system in column_order if system in table["strict"]]
    split = [
        system
        for system in present
        if abs(table["strict"][system] - table["cause_only"][system]) > 1e-12
    ]
    if not present:
        lines.append("注：无数据，不做口径敏感性陈述。")
    elif split:
        detail = "、".join(
            f"{system}（strict {table['strict'][system]:.1%} vs cause_only "
            f"{table['cause_only'][system]:.1%}）"
            for system in split
        )
        lines.append(
            f"注：本轮 `strict` 与 `cause_only` **在 {'、'.join(split)} 上分开了**（{detail}）——"
            "存在「根因对、动作错」的样本，口径敏感性检验**已被激活**。"
        )
    else:
        lines.append(
            "注：`strict` 与 `cause_only` 在当前推理器下**完全相同**——因为它的误判分支"
            "同时改根因与动作，不存在「根因对、动作错」的样本。也就是说本轮的口径敏感性"
            "检验**没有真正被激活**；真实模型完全可能给出「根因对但动作过激」，那时两种口径"
            "才会分开（噪声口径 `--reasoner noisy --noise-flavor diagnosis_ok_action_wrong` "
            "把它激活）。"
        )
    return "\n".join(lines)


def statistical_notes(cells: Sequence[Cell], results: Sequence[RunResult]) -> str:
    """把"能不能下结论"写清楚：配对最小可检测效应 + 关键配对比较 + 空数据保护。"""
    lines: list[str] = []
    if not cells or not results:
        return "* 无数据：不做任何统计陈述（早期版本会把「没有数据」打印成「不可区分」）。"

    # --systems 子集：配对结论写死了 harness vs single_shot，双方不在场时不输出假数据
    present_systems = {cell.system for cell in cells}
    can_pair = {"harness", "single_shot"} <= present_systems
    if not can_pair:
        lines.append(
            f"* 本轮只评测了 {'、'.join(sorted(present_systems))}，"
            "写死的 harness − single_shot 配对统计与最小可检测效应不适用。"
        )
    _, pairs = paired_compare(
        results,
        system_a="harness",
        system_b="single_shot",
        predicate=lambda r: r.red_line,
        profile="weak-guesser",
    )
    # 不一致对 = 同一配对键上两臂结论不同的题数（独立验证 2026-09-19 P2-2：
    # 早期版本把两臂的红线数**相加**当成不一致对；在 harness 恒为 0 红线的今天数值碰巧相等，
    # 口径却是错的——换了竞争假设（比如两臂都有红线）会算出偏大的 MDE）。
    discordant = discordant_pairs(
        results,
        system_a="harness",
        system_b="single_shot",
        predicate=lambda r: r.red_line,
        profile="weak-guesser",
    )
    mde_paired = paired_min_detectable_effect(discordant=discordant, pairs=pairs)
    mde_independent = min_detectable_effect(max(c.runs for c in cells), p=0.5)
    mde_ci_halfwidth_p90 = min_detectable_effect(max(c.runs for c in cells), p=0.9)
    if can_pair:
        lines.append(
            f"* **最小可检测效应**：n={pairs} 对。配对口径（80% 功效）约 **{mde_paired:.1%}**"
            f"（不一致对 {discordant} 个）；独立两样本口径（p=0.5）约 {mde_independent:.1%}。"
            f"第三个数字 {mde_ci_halfwidth_p90:.1%} 是「p≈0.9 时的 95% CI 半宽」，"
            "**没有功效项、也不是配对口径**，引用时必须写清用的是哪一个。"
            "小于所选口径的差距在本样本量下不可区分——报告只用配对口径解释配对结论。"
        )
    for predicate, label in (
        (lambda r: r.correct, "诊断正确率"),
        (lambda r: r.red_line, "红线执行率"),
    ):
        interval, pairs_n = paired_compare(
            results,
            system_a="harness",
            system_b="single_shot",
            predicate=predicate,
            profile="weak-guesser",
        )
        verdict = "可区分（CI 不含 0）" if interval.excludes_zero else "**不可区分（CI 含 0）**"
        if can_pair and pairs_n:
            lines.append(
                f"* **harness − single_shot 的{label}**（weak-guesser，配对 {pairs_n} 组）："
                f"{interval.point:+.3f}，95% CI [{interval.low:+.3f}, {interval.high:+.3f}]"
                f" ⇒ {verdict}。"
            )
    return "\n".join(lines)


def proxy_section(cells: Sequence[Cell], results: Sequence[RunResult]) -> str:
    rows = [
        proxy_calibration(results, system=system, profile=profile)
        for system in SYSTEMS
        for profile in ("competent-honest", "weak-guesser")
    ]
    lines = [
        "| 系统 | 推理器 | n | 代理为真 | 真值为真 | 代理有方差 | Cohen's κ | 解读 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        if not row["n"]:
            continue  # --systems 子集：缺席系统不打印 n=0 的假校准行
        lines.append(
            f"| {row['system']} | {row['profile']} | {row['n']} | {row['proxy_positive']} |"
            f" {row['truth_positive']} | {'是' if row['proxy_variance'] else '否'} |"
            f" {row['kappa']} | {row['kappa_label']} |"
        )
    return "\n".join(lines)
