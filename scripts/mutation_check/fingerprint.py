"""变异体内容指纹：判据只按名字 + 状态对账时，等量改写会静默绿。"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import MUTANTS_DIR

# 内容指纹的算法标识：写进基线，**指纹换了口径必须换这个名字**——
# 否则新旧基线会被当成"内容变了"而集体误报（判据要说得出自己是怎么算的）。
FINGERPRINT_ALGORITHM = "sha256(normalized-mutmut-diff)/v1"


@contextmanager
def _chdir(path: Path) -> Iterator[None]:
    """临时切到 ``path``：mutmut 的读函数按**当前工作目录**找 ``mutants/``。

    `get_diff_for_mutant` / `MutantLineSpans.load` 内部写死了 ``Path("mutants") / path``，
    给不了目录参数。本脚本其余部分（`mutmut run` / `mutmut results`）本来就以
    `cwd=PROJECT_ROOT` 起子进程，所以在同一目录里读是一致的；离开时必还原。
    """
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def mutant_paths_in_cache(mutants_dir: Path) -> dict[str, Path]:
    """变异体名 → 变异文件路径（相对 ``mutants/``）。

    映射取自 `mutants/**/*.meta` 里的 `exit_code_by_key`：那是 mutmut 自己写下的账本
    （`find_mutant` 也按它走）。**不用名字反推路径**（`harness.approval.xǁ…` →
    `harness/approval.py`）——那是把 mutmut 的命名约定再抄一遍，抄错就是静默错位。
    """
    mapping: dict[str, Path] = {}
    if not mutants_dir.exists():
        return mapping
    for meta_path in sorted(mutants_dir.glob("**/*.meta")):
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        relative = meta_path.relative_to(mutants_dir).with_suffix("")
        for name in payload.get("exit_code_by_key", {}):
            mapping[str(name)] = relative
    return mapping


def normalize_fingerprint_source(diff: str) -> str:
    """规范化：逐行去尾空格 + 丢弃首尾空行。

    行尾空格与文件末尾换行不构成"变异内容变了"；反过来，任何真实的代码改写
    （常数改值、条件取反、语句换序）都会改变规范化文本 ⇒ 指纹改变。
    """
    return "\n".join(line.rstrip() for line in diff.splitlines()).strip()


def compute_mutant_fingerprints(
    names: list[str] | set[str], *, mutants_dir: Path | None = None
) -> dict[str, str | None]:
    """给每条变异体算内容指纹；取不到就记 ``None``（**绝不编一个**）。

    内容取自 mutmut 自己的渲染（`get_diff_for_mutant`），因此与 `mutmut show` 的 diff 部分
    逐字相同——本机实测 40 条样本零差异，且同一条重复调用输出逐字相同。
    取不到的三类原因都落到 ``None``：`mutants/` 不存在、名字不在任何 `.meta` 里、
    渲染时读文件失败；调用方负责把"有几条取不到"打印出来。
    """
    from mutmut.mutation.diff_apply import get_diff_for_mutant  # 懒 import：--summary-only 不需要

    mutants_dir = Path(mutants_dir or MUTANTS_DIR)
    if not mutants_dir.exists():
        return {name: None for name in names}
    if mutants_dir.name != "mutants":
        raise ValueError(
            f"{mutants_dir} 不叫 `mutants`：mutmut 的读函数按当前目录找 `mutants/`，"
            "换个名字会让指纹静默全空"
        )
    paths = mutant_paths_in_cache(mutants_dir)
    fingerprints: dict[str, str | None] = {}
    with _chdir(mutants_dir.parent):
        for name in sorted(names):
            relative = paths.get(name)
            if relative is None:
                fingerprints[name] = None
                continue
            try:
                diff = get_diff_for_mutant(name, path=str(relative))
            except Exception:  # 渲染失败 ⇒ 如实记 None（下面会打印条数）
                fingerprints[name] = None
                continue
            digest = hashlib.sha256(
                normalize_fingerprint_source(diff).encode("utf-8")
            ).hexdigest()
            fingerprints[name] = f"sha256:{digest}"
    return fingerprints


def fingerprint_report(fingerprints: dict[str, str | None]) -> list[str]:
    """要打印的指纹口径说明（每次运行都打，免得被读成"内容肯定没变"）。"""
    total = len(fingerprints)
    readable = sum(1 for value in fingerprints.values() if value)
    lines = [
        f"[mutation] 内容指纹（{FINGERPRINT_ALGORITHM}）：{readable}/{total} 条可读"
    ]
    if readable < total:
        lines.append(
            f"  ⚠️ {total - readable} 条取不到内容（mutants/ 缺文件或 .meta 里没有这个名字）："
            "这些条目**不参与**指纹比对，不假装比对过。"
        )
    return lines


def fingerprint_comparison_lines(
    fingerprints: dict[str, str | None], baseline_fingerprints: dict[str, str]
) -> list[str]:
    """"这一轮到底比了几条"——**必须分两种说法**：基线没有 vs 本轮取不到。

    两种形态的处置不同：前者跑一次 `--update-baseline` 就有得比了；后者说明这一轮
    `mutants/` 侧取不到内容，得先修缓存。把两者混成一句"没有可比对的指纹"会指向错的处置。
    """
    compared = sum(
        1 for name, value in fingerprints.items() if value and baseline_fingerprints.get(name)
    )
    if compared:
        return [f"  与基线逐条比对：{compared} 条同名可比对。"]
    if baseline_fingerprints:
        return [
            "  ⚠️ 本轮没有任何条目能与基线比对（当前侧取不到内容）："
            "**只记账、不判内容**——这不是「内容没变」，而是「这一格没在看」。"
        ]
    return [
        "  ⚠️ 基线里没有可比对的指纹（旧基线或刚刷新）：本轮**只记账、不判内容**；"
        "跑一次 `--update-baseline` 即可让这条判据生效。"
    ]



