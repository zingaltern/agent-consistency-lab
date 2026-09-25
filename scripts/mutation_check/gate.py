"""七条红灯条件 + 未知状态 fail-closed（判据与渲染分离）。"""

from __future__ import annotations

from typing import Any

from .config import DECIDED_STATUSES


def gate_verdict(summary: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """七条红灯条件 + 未知状态 fail-closed。返回 ``{"failures": [...], ...}``。

    纯函数：不吃 mutmut、不读文件——判定逻辑本身要能被单元测试与退化注入直接钉住。
    """
    base = baseline["status_by_mutant"]
    base_decided = {name for name, status in base.items() if status in DECIDED_STATUSES}
    current = summary["status_by_mutant"]

    # 1. 新增幸存变异（基线里 killed 的变成 survived 也算：那是新增的盲区）。
    new_survivors = [name for name in summary["survived"] if base.get(name) != "survived"]
    # 2. 基线里是判定类的变异体，本轮变成非判定类（含 no tests / segfault / timeout…）。
    regressed_from_decided = sorted(
        name for name in base_decided if name in current and current[name] not in DECIDED_STATUSES
    )
    # 3. 基线里存在的变异体本轮完全缺失（未评估 / 改名 / 缓存缺项）。
    vanished = sorted(name for name in base if name not in current)
    # 4. 新增 no tests（P0-2）。
    new_no_tests = [name for name in summary["no_tests"] if base.get(name) != "no tests"]
    # 6. 无结论集合较基线增长。
    new_inconclusive = sorted(set(summary["inconclusive"]) - baseline["inconclusive"])
    # 7. 同名不同指纹：判据只按"名字 + 状态"对账时，等量改写会让同一批名字指向不同的变异
    #    （独立验证 2026-09-18 · 报告 §5-3）。只比"两边都取到指纹"的名字——
    #    取不到的那一侧由 fingerprint_report 打印条数，绝不假装比对过。
    base_fingerprints = baseline.get("fingerprints") or {}
    content_changed = sorted(
        name
        for name, value in (summary.get("fingerprints") or {}).items()
        if value and base_fingerprints.get(name) and value != base_fingerprints[name]
    )

    failures: list[dict[str, Any]] = []

    def fail(key: str, label: str, names: list[str], hint: str = "") -> None:
        failures.append(
            {"key": key, "label": label, "count": len(names), "names": names, "hint": hint}
        )

    if summary["total"] == 0:
        fail(
            "empty-result-set",
            "本轮**没有产出任何变异体**（total=0）",
            [],
            "这不是「没有盲区」，而是「没跑起来」。常见原因：mutmut 配置未生效、"
            "`mutants/` 被清空、collect error、runner 崩溃。",
        )
    elif summary["decided"] == 0:
        fail(
            "no-decided-mutants",
            "本轮**没有任何一条变异体得到判定**（killed=survived=0）",
            summary["inconclusive"],
            "全是无结论类 / 未覆盖类：这种情况下的 survivor_rate=0 毫无意义。",
        )
    if new_survivors:
        fail("new-survivors", "新增幸存变异（= 新增的测试盲区）", new_survivors)
    if regressed_from_decided:
        fail(
            "decided-to-inconclusive",
            "基线里**有判定**的变异体，本轮变成无结论 / 未覆盖",
            regressed_from_decided,
            "它们从门禁视野里消失了——只有「确实被 killed」才算好消息。",
        )
    if vanished:
        fail(
            "vanished-from-results",
            "基线里的变异体本轮**完全缺失**（未评估 / 改名 / 缓存缺项）",
            vanished,
        )
    if new_no_tests:
        fail(
            "new-no-tests",
            "新增 `no tests` 变异体（= 新增的、完全没有用例覆盖的代码）",
            new_no_tests,
        )
    if new_inconclusive:
        fail(
            "inconclusive-grew",
            "无结论集合较基线**增长**（有新名字进入不可见空间）",
            new_inconclusive,
        )
    if content_changed:
        fail(
            "mutant-content-changed",
            "同名变异体的**内容指纹变了**（名字与状态都对得上，但已经不是同一条变异）",
            content_changed,
            "这是「等量改写」的窗口：条数与编号都没变，只有变异内容变了——"
            "只按名字 + 状态对账会让它静默绿。要么把改动还原，要么确认新内容后"
            "跑 --update-baseline（并在 PR 里说明）。",
        )
    if summary["unknown_statuses"]:
        fail(
            "unknown-status",
            "出现本脚本不知道的 mutmut 状态名（全状态记账的兜底）",
            summary["unknown_statuses"],
            "先把它归类进判定类 / 未覆盖类 / 无结论类，再判定；不要让它漏过去。",
        )

    return {
        "failures": failures,
        "new_survivors": new_survivors,
        "regressed_from_decided": regressed_from_decided,
        "vanished": vanished,
        "new_no_tests": new_no_tests,
        "new_inconclusive": new_inconclusive,
        "content_changed": content_changed,
        "baseline_full_status_coverage": baseline["full_status_coverage"],
        "ok": not failures,
    }



def _report_line(summary: dict[str, Any]) -> str:
    sc = summary["status_counts"]
    parts = [f"{status}={count}" for status, count in sc.items() if count]
    unknown = summary["unknown_statuses"]
    if unknown:
        parts.append("未知状态=" + ",".join(unknown))
    return "[mutation] 全状态记账：" + " ".join(parts) if parts else "[mutation] 全状态记账：（空）"



def _invisible_space_lines(summary: dict[str, Any]) -> list[str]:
    total = summary["total"]
    decided = summary["decided"]
    invisible = summary["invisible_count"]
    share = summary["invisible_share"]
    rate = (
        f"{summary['survivor_rate']:.3f}"
        if decided
        else "n/a（没有任何一条变异体得到判定）"
    )
    return [
        _report_line(summary),
        f"[mutation] 判定类={decided} survivor_rate={rate}（分母只含 killed+survived）",
        f"[mutation] 不可见空间（无结论类 + 未覆盖类）= {invisible}/{total} "
        f"= {share:.1%}：这一部分既不进 survivor_rate，也不等于「测试没问题」；"
        "**survivor_rate 不是覆盖率**。",
    ]


