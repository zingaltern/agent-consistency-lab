"""统计工具：把小样本对照的结论说得起（或者干脆说不起）。

三条原则（直接来自 W2/W3 评审的批评："点估计 + 无区间等于不可辩护"）：

1. **报告区间**：单比例用 Wilson 区间（小样本比正态近似稳），
   两条路线的差值用**配对 bootstrap**（配对键 = 场景 × 推理器 × 重复序号）。
2. **报告最小可检测效应**：样本量决定不了的小差距，必须在报告里说"不可区分"，
   而不是把 2% 的差当成发现。
3. **代理指标要校准**：能在线观测的指标（如"是否取到决定性证据"）与被 ground truth
   定义的真值（"诊断是否正确"）之间的一致性用 Cohen's κ 度量；
   κ 低就说明该代理不能单独当监控指标。
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Interval:
    point: float
    low: float
    high: float
    n: int

    def __str__(self) -> str:
        return f"{self.point:.3f} [{self.low:.3f}, {self.high:.3f}] (n={self.n})"

    @property
    def excludes_zero(self) -> bool:
        return self.low > 0 or self.high < 0


def wilson_interval(successes: int, n: int, z: float = 1.96) -> Interval:
    """Wilson 比例区间（小样本友好）。"""
    if n == 0:
        return Interval(0.0, 0.0, 0.0, 0)
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return Interval(p, max(0.0, centre - margin), min(1.0, centre + margin), n)


def paired_bootstrap_diff(
    pairs: Sequence[tuple[float, float]],
    *,
    n_boot: int = 2000,
    seed: int = 20260916,
    alpha: float = 0.05,
) -> Interval:
    """配对差值的 bootstrap 区间：pairs = [(A_i, B_i)]，差值定义为 A_i − B_i。

    配对键由调用方决定（本项目是"场景 × 推理器 × 重复序号"），
    这样比较的是**同一题目上的表现差**，而不是两次独立抽样。
    """
    if not pairs:
        return Interval(0.0, 0.0, 0.0, 0)
    diffs = [a - b for a, b in pairs]
    point = sum(diffs) / len(diffs)
    rng = random.Random(seed)
    n = len(diffs)
    samples: list[float] = []
    for _ in range(n_boot):
        total = 0.0
        for _ in range(n):
            total += diffs[rng.randrange(n)]
        samples.append(total / n)
    samples.sort()
    low = samples[math.floor(alpha / 2 * n_boot)]
    high = samples[math.ceil((1 - alpha / 2) * n_boot) - 1]
    return Interval(point, low, high, n)


def min_detectable_effect(n: int, p: float = 0.5, z: float = 1.96) -> float:
    """配对/独立比例在给定样本量下能区分的**最小效应**（两点估计的半宽）。

    用来回答"这个差距是真的吗"：差距小于它就说明样本量不足以支持结论。
    """
    if n <= 0:
        return 1.0
    return z * math.sqrt(2 * p * (1 - p) / n)


def cohens_kappa(labels_a: Sequence[bool], labels_b: Sequence[bool]) -> float:
    """两个二值标注者的一致性（Cohen's κ）。无方差时返回 0.0 并由调用方解释。"""
    if len(labels_a) != len(labels_b) or not labels_a:
        raise ValueError("labels must be non-empty and equally long")
    n = len(labels_a)
    observed = sum(1 for a, b in zip(labels_a, labels_b, strict=True) if a == b) / n
    pa1 = sum(labels_a) / n
    pb1 = sum(labels_b) / n
    expected = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    if expected >= 1.0:  # 双方都没有方差 ⇒ κ 无定义
        return 0.0
    return (observed - expected) / (1 - expected)


def kappa_label(kappa: float) -> str:
    """Landis & Koch 的经验分档，写进报告时统一口径。"""
    if kappa >= 0.81:
        return "几乎完全一致"
    if kappa >= 0.61:
        return "高度一致"
    if kappa >= 0.41:
        return "中等一致"
    if kappa >= 0.21:
        return "一般一致"
    if kappa > 0:
        return "轻微一致"
    return "无一致性"
