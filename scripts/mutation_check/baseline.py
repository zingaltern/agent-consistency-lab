"""基线的读、写、刷新计划与"更宽即拒绝"守卫。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import INCONCLUSIVE_STATUSES


def status_map_from_payload(payload: dict[str, Any]) -> tuple[dict[str, str], bool]:
    """从基线/候选 payload 里取 ``{变异体名: 状态}``；第二个返回值是"逐条状态是否齐全"。

    v1 只有 `survivors`/`no_tests` 两个列表（`killed` 没有名字）⇒ 返回 False；
    门禁要据此打印"这一格覆盖不到"。
    """
    full = isinstance(payload.get("status_by_mutant"), dict) and bool(payload["status_by_mutant"])
    if full:
        return {str(k): str(v) for k, v in payload["status_by_mutant"].items()}, True
    status_by_mutant: dict[str, str] = {}
    for name in payload.get("survivors", []):
        status_by_mutant[str(name)] = "survived"
    for name in payload.get("no_tests", []):
        status_by_mutant[str(name)] = "no tests"
    return status_by_mutant, False



def baseline_widening(
    old_payload: dict[str, Any] | None, candidate: dict[str, Any]
) -> list[dict[str, Any]]:
    """候选基线相对旧基线**变宽**的地方；空列表 = 不比旧的更宽。**纯函数**。

    为什么需要它（独立验证 2026-09-18 · 报告 §5-2）：`--update-baseline` 是文档化的
    逃生门（要求 PR 里说明），但技术上**没有任何"比上一版更宽就拒绝"的守卫**——
    于是"顺手把这一轮的盲区冻进基线"在机制上没有任何阻力。第一版基线刷新就实测过一次：
    沿用旧缓存跑出 14.7 秒的混合结果、`no tests` 从 18 虚增到 241。

    "更宽"按**今天已有的红灯条件**定义，四条，一条都不新发明：

    | 判据 | 含义 |
    |---|---|
    | 新增幸存变异 | 候选里 `survived`、旧基线里不是 |
    | 新增 `no tests` | 候选里新增的未覆盖条目 |
    | 无结论集合增长 | 候选的 `inconclusive` 里有旧基线没有的名字 |
    | 基线判定缺失 | 旧基线里有、候选里整条不见（这一格不再被看） |

    "判定类变成无结论"不单列：它必然同时体现在"无结论集合增长"上。
    """
    old_status, _ = status_map_from_payload(old_payload or {})
    new_status, _ = status_map_from_payload(candidate)
    new_survivors = sorted(
        name
        for name, status in new_status.items()
        if status == "survived" and old_status.get(name) != "survived"
    )
    new_no_tests = sorted(
        name
        for name, status in new_status.items()
        if status == "no tests" and old_status.get(name) != "no tests"
    )
    new_inconclusive = sorted(
        set(candidate.get("inconclusive", [])) - set((old_payload or {}).get("inconclusive", []))
    )
    missing = sorted(name for name in old_status if name not in new_status)

    widening: list[dict[str, Any]] = []
    for key, label, names in (
        ("new-survivors", "候选基线里新增了**幸存变异**（新增的测试盲区）", new_survivors),
        ("new-no-tests", "候选基线里新增了 `no tests`（新增的、完全没被测的代码）", new_no_tests),
        ("inconclusive-grew", "候选基线的**无结论集合增长**了", new_inconclusive),
        ("baseline-entries-missing", "旧基线里有、候选里**整条不见**（这一格不再被看）", missing),
    ):
        if names:
            widening.append({"key": key, "label": label, "count": len(names), "names": names})
    return widening



def baseline_refresh_plan(
    *, update_baseline: bool, allow_incremental: bool, mutants_dir: Path, stamp: str
) -> dict[str, Any]:
    """``--update-baseline`` 之前该做什么。**纯函数**：只看入参，不碰文件系统。

    为什么需要它（2026-09-19 实测）：``mutmut run`` 是**增量**的——``mutants/`` 缓存还在时
    它只重跑"函数哈希变了"的变异体，其余直接沿用旧判决。于是改过代码之后直接
    ``--update-baseline``，写出的是一份**混合了旧判决**的基线：本轮实测过一次，
    14.7 秒"跑完"、``no tests`` 从 18 虚增到 241——那不是基线，是混合结果。

    "模块 mtime 变了"之类的信号不可靠：同一个模块里**没改**的函数，它的变异体本来
    就应当保留旧判决。唯一可靠的判据是**缓存干不干净**，所以刷新基线时把缓存整体移开，
    而不是去猜哪些条目还有效。
    """
    target = f"{mutants_dir}.stale-{stamp}"
    if not update_baseline:
        return {
            "action": "none",
            "mutants_dir": str(mutants_dir),
            "moved_to": None,
            "reason": "不是刷新基线的一轮：增量是 mutmut 的正常工作方式（判定读的是全量结果表）",
        }
    if not mutants_dir.exists():
        return {
            "action": "full-run",
            "mutants_dir": str(mutants_dir),
            "moved_to": None,
            "reason": "没有 mutants/ 缓存：本轮本来就是全量",
        }
    if allow_incremental:
        return {
            "action": "incremental",
            "mutants_dir": str(mutants_dir),
            "moved_to": None,
            "reason": "显式给了 --allow-incremental-refresh",
            "warning": (
                f"{mutants_dir} 还在，本轮沿用其中的旧判决 ⇒ 写出的基线可能混合"
                "（旧判决 + 这次重跑的判决）。只在确认缓存与当前代码一致时这么做，"
                "并在 PR 里写明；否则去掉 --allow-incremental-refresh 重跑。"
            ),
        }
    return {
        "action": "move-aside",
        "mutants_dir": str(mutants_dir),
        "moved_to": target,
        "reason": (
            f"刷新基线要先有干净缓存：把 {mutants_dir} 整体移开，本轮因而是全量重跑"
        ),
    }


def apply_baseline_refresh_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """执行 :func:`baseline_refresh_plan` 的决定（唯一的副作用点），返回更新后的 plan。"""
    if plan["action"] == "move-aside":
        source = Path(plan["mutants_dir"])
        target = Path(str(plan["moved_to"]))
        suffix = 2
        while target.exists():  # 同一秒里刷新两次也要有个去处，不许覆盖
            target = Path(f"{plan['moved_to']}-{suffix}")
            suffix += 1
        source.rename(target)
        plan["moved_to"] = str(target)
        print(f"[mutation] {plan['reason']}\n  移到了 {target}（可删；已 gitignore）")
    elif plan["action"] == "incremental":
        print(f"[mutation] **警告** {plan['warning']}")
    return plan


def load_baseline(path: Path) -> dict[str, Any]:
    """读基线。**兼容 v1 / v2**：v1 只有 `survivors`/`no_tests` 两个列表；
    v2 有逐条 `status_by_mutant` 但没有内容指纹（v3 起才有）。

    v1 的 `killed` 只有计数没有名字，所以条件 2/3 在 v1 上只能覆盖
    "基线幸存者" 与 "基线 no tests"——这一点会被打印出来，免得被读成全覆盖。
    v2 缺指纹 ⇒ 条件 7 不生效（打印出来，不假装比对过）。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    status_by_mutant, full = status_map_from_payload(payload)
    raw_fingerprints = payload.get("fingerprints")
    fingerprints = (
        {str(k): str(v) for k, v in raw_fingerprints.items()}
        if isinstance(raw_fingerprints, dict)
        else {}
    )
    return {
        "schema": str(payload.get("schema", "mutation-baseline/v1")),
        "status_by_mutant": status_by_mutant,
        "full_status_coverage": full,
        "fingerprints": fingerprints,
        "fingerprint_algorithm": str(payload.get("fingerprint_algorithm", "")),
        "inconclusive": set(
            name for name, status in status_by_mutant.items() if status in INCONCLUSIVE_STATUSES
        ),
    }



def _summary_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """`--summary-only` / `--json-out` 的公开字段。

    `survivors` 一律是**列表**（v1 曾在这里写个数，而主判定路径写列表——
    同一个字段名两种类型，claim 对账会踩空）；个数是 `survivor_count`。
    """
    counts = payload.get("counts", {})
    status_by_mutant = payload.get("status_by_mutant") or {}
    # 老基线可能只写了计数没写名单：能从 `status_by_mutant` 推的就推出来，
    # 推不出来才回落到 0——但绝不假装"没有不可见空间"。
    if "inconclusive" in payload:
        inconclusive = list(payload["inconclusive"])
    else:
        inconclusive = sorted(
            name for name, status in status_by_mutant.items() if status in INCONCLUSIVE_STATUSES
        )
    invisible = counts.get("invisible")
    if invisible is None:
        decided = counts.get("decided")
        if decided is None:
            decided = counts.get("killed", 0) + counts.get("survived", 0)
        total = counts.get("total", len(status_by_mutant))
        invisible = total - decided
    return {
        "schema": payload.get("schema", "mutation-baseline/v1"),
        "counts": counts,
        "status_counts": payload.get("status_counts", counts),
        "survivor_rate": payload["survivor_rate"],
        "survivors": list(payload.get("survivors", [])),
        "survivor_count": len(payload.get("survivors", [])),
        "no_tests": list(payload.get("no_tests", [])),
        "no_tests_count": len(payload.get("no_tests", [])),
        "inconclusive": inconclusive,
        "inconclusive_count": len(inconclusive),
        "invisible_count": invisible,
        "invisible_share": payload.get("invisible_share", 0.0),
        "modules": payload.get("modules", []),
        # 只给**条数**与算法名：指纹本身 2000+ 条，逐条打在这一行 stdout 上没人读得动。
        # （它是给门禁逐条比对用的，完整名单在基线文件与 `--json-out` 的产物里。）
        "fingerprint_algorithm": payload.get("fingerprint_algorithm", ""),
        "fingerprint_count": len(payload.get("fingerprints") or {}),
    }


