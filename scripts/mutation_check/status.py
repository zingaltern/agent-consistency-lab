"""读 mutmut 的状态表并折算成"全状态记账"的摘要。"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from .config import INCONCLUSIVE_STATUSES, KNOWN_STATUSES, PROJECT_ROOT
from .reporting import _abort


def collect_status(*, json_out: str = "") -> dict[str, list[str]]:
    """读 mutmut 的结果表：``{status: [mutant 名]}``。**全状态**，不做任何过滤。"""
    proc = subprocess.run(
        # mutmut 3.x 的 --all 是**带值**选项（default=False 且没有 is_flag），
        # 必须写 --all=true，否则报 "Option '--all' requires an argument"。
        # 要全量是因为 survivor_rate 需要 killed 数作分母（只看非 killed 会把比率算成 1.0），
        # 也因为"无结论类"必须被看见（P0-1 的教训）。
        [sys.executable, "-m", "mutmut", "results", "--all=true"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        _abort(
            "results_unreadable",
            f"[mutation] 读结果失败：{proc.stderr[-400:]}",
            json_out=json_out,
        )
    buckets: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, _, status = line.rpartition(":")
        buckets.setdefault(status.strip(), []).append(name.strip())
    return buckets



def all_mutant_names(buckets: dict[str, list[str]]) -> set[str]:
    """结果表里的**全部**变异体名（所有状态，不做过滤）。

    指纹要对"结果表里出现过的每一条"取——包括幸存、no tests 与无结论类：
    无结论类变成有结论时，内容也该是同一份。
    """
    return {name for names in buckets.values() for name in names}



def mutation_summary(
    buckets: dict[str, list[str]], *, fingerprints: dict[str, str | None] | None = None
) -> dict[str, Any]:
    """把 mutmut 的状态表折算成"全状态记账"的摘要。**不丢弃任何状态**。

    ``fingerprints`` 是可选的内容指纹（`compute_mutant_fingerprints` 的产物）：
    默认空 dict 时，摘要与加指纹之前**逐字相同**（旧调用点不用改）。
    """
    by_status = {status: sorted(names) for status, names in buckets.items()}
    status_by_mutant = {name: status for status, names in buckets.items() for name in names}
    counts = {status: len(buckets.get(status, [])) for status in KNOWN_STATUSES}
    unknown_statuses = sorted(status for status in buckets if status not in KNOWN_STATUSES)
    decided = counts["killed"] + counts["survived"]
    total = len(status_by_mutant)
    # 不认识的状态名一律计入"不可见空间"（fail-closed）：宁可让它显得更差，
    # 也不能让它悄悄消失。
    inconclusive = sorted(
        name
        for name, status in status_by_mutant.items()
        if status in INCONCLUSIVE_STATUSES or status in unknown_statuses
    )
    return {
        "killed": counts["killed"],
        "survived": by_status.get("survived", []),
        "no_tests": by_status.get("no tests", []),
        "inconclusive": inconclusive,
        "status_counts": counts,
        "unknown_statuses": unknown_statuses,
        "by_status": by_status,
        "status_by_mutant": status_by_mutant,
        "total": total,
        "decided": decided,
        "uncovered_count": counts["no tests"],
        "invisible_count": total - decided,
        "invisible_share": (total - decided) / total if total else 0.0,
        # 幸存率只在"被判定的变异体"上算：no tests 与无结论类是另一类盲区，
        # 混进分母会让这个数看起来更好看。**它不是覆盖率**。
        "survivor_rate": (counts["survived"] / decided) if decided else 0.0,
        "fingerprints": dict(fingerprints or {}),
    }



def show_mutant(name: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "mutmut", "show", name],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.stdout.strip()[-600:]


