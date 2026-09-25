"""跑批与聚合：同一批场景在四类系统上跑一遍，聚合成可比较的格子。

两个推理器人格共用同一批场景：
* ``competent-honest``：能力 0.9，证据不足时承认不知道（不乱动）
* ``weak-guesser``    ：能力 0.6，证据不足时按最强信号硬猜（更容易出事）
"""

from __future__ import annotations

import random
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import BaseModel

from harness.tokens import PriceTable

from ..policy import Disposition, NoiseFlavor, ReasonerProfile, noise_rng_for
from ..scenario import Scenario
from ..systems import SYSTEMS, Operator, RunResult, run_system


def _live_profiles() -> tuple[ReasonerProfile, ...]:
    """**调用时**从门面读 ``PROFILES``（而不是本模块里那份副本）。

    为什么要这层间接：拆分前 ``PROFILES`` 是 ``opsenv.suite`` 的模块全局，
    ``noisy_profiles`` / ``main`` 每次调用都现读它——因此 `opsenv.suite.PROFILES = ...`
    这种外部赋值是**有效**的，`docs/independent-test-2026-09-17/commands.sh` 正是靠它
    换 ``noise_seed`` 来证明噪声口径可复现。搬进子模块后若各自持有副本，那个赋值会
    **静默失效**：脚本照跑、数字照出，只是口径不再是它以为的那个。
    """

    import opsenv.suite as facade  # 延迟 import：门面要等本模块加载完才成形

    return facade.PROFILES


PROFILES: tuple[ReasonerProfile, ...] = (
    ReasonerProfile(
        name="competent-honest", competence=0.9, disposition=Disposition.HONEST, seed=11
    ),
    ReasonerProfile(name="weak-guesser", competence=0.6, disposition=Disposition.GUESSER, seed=23),
)


def noisy_profiles(
    *, error_rate: float, flavor: NoiseFlavor = NoiseFlavor.BOTH
) -> tuple[ReasonerProfile, ...]:
    """噪声人格：**整体替换**推理器，用于独立口径的重跑（R-A4）。

    只改噪声参数，competence / disposition / seed 与默认人格一致——这样"噪声口径 vs 默认口径"
    的差异只能来自噪声，不能来自别处。默认人格对象本身不被修改（model_copy 返回副本）。
    """
    if not 0.0 <= error_rate <= 1.0:
        raise ValueError(f"error_rate 必须在 [0,1] 内，收到 {error_rate}")
    return tuple(
        profile.model_copy(update={"error_rate": error_rate, "flavor": flavor})
        for profile in _live_profiles()
    )


class Cell(BaseModel):
    system: str
    profile: str
    runs: int
    correct: int
    resolved: int
    red_line: int
    novel_red_line: int
    gated: int
    blocked: int
    sufficient: int
    avg_steps: float
    avg_input_tokens: float
    avg_cost_usd: float
    avg_wall_ms: float

    def rates(self) -> dict[str, float]:
        n = max(1, self.runs)
        return {
            "correct": self.correct / n,
            "resolved": self.resolved / n,
            "red_line": self.red_line / n,
            "novel_red_line": self.novel_red_line / n,
            "gated": self.gated / n,
            "blocked": self.blocked / n,
            "sufficient": self.sufficient / n,
        }


def run_suite(
    *,
    catalog: Sequence[Scenario],
    systems: Sequence[str] = SYSTEMS,
    profiles: Sequence[ReasonerProfile] = PROFILES,
    repeats: int = 3,
    workroot: Path | None = None,
    price: PriceTable | None = None,
    operator: Operator | None = None,
) -> list[RunResult]:
    price = price or PriceTable()
    # operator 可注入：橡皮图章（永远批准）用来证明"gate 的 0% 红线"来自人而不是架构
    operator = operator or Operator()
    root = workroot or Path(tempfile.mkdtemp(prefix="w5-suite-"))
    results: list[RunResult] = []
    for profile in profiles:
        for scenario in catalog:
            for system in systems:
                for repeat in range(repeats):
                    # 共同随机数（CRN）：种子**不含** system 名。早期版本把 system 放进
                    # 种子里，导致同一机制在不同系统名下的硬币流不同——审计实测出
                    # "langgraph 被拦下 29.7% vs harness 20.3%" 这种纯哈希伪影（p=0.0009）。
                    rng = random.Random(f"{profile.seed}:{scenario.id}:{repeat}")
                    # 噪声流与判定流分离，且同样不含 system 名（CRN）：
                    # 同一（场景 × 重复序号）在四条系统上遇到同一串噪声。
                    noise_rng = noise_rng_for(profile, scenario_id=scenario.id, repeat=repeat)
                    workdir = root / f"{profile.name}-{scenario.id}-{system}-{repeat}"
                    workdir.mkdir(parents=True, exist_ok=True)
                    result = run_system(
                        system,
                        scenario=scenario,
                        profile=profile,
                        rng=rng,
                        operator=operator,
                        price=price,
                        workdir=workdir,
                        noise_rng=noise_rng,
                    )
                    results.append(result.model_copy(update={"repeat": repeat}))
    return results


def aggregate(results: Iterable[RunResult]) -> list[Cell]:
    buckets: dict[tuple[str, str], list[RunResult]] = defaultdict(list)
    for result in results:
        buckets[(result.system, result.profile_name)].append(result)
    cells: list[Cell] = []
    for (system, profile), group in sorted(buckets.items()):
        n = len(group)
        cells.append(
            Cell(
                system=system,
                profile=profile,
                runs=n,
                correct=sum(r.correct for r in group),
                resolved=sum(r.resolved for r in group),
                red_line=sum(r.red_line for r in group),
                novel_red_line=sum(r.novel_red_line for r in group),
                gated=sum(r.gated for r in group),
                blocked=sum(r.blocked for r in group),
                sufficient=sum(r.sufficient for r in group),
                avg_steps=round(sum(r.steps for r in group) / n, 2),
                avg_input_tokens=round(sum(r.input_tokens for r in group) / n, 1),
                avg_cost_usd=round(sum(r.cost_usd for r in group) / n, 6),
                avg_wall_ms=round(sum(r.wall_ms for r in group) / n, 2),
            )
        )
    return cells


def group_by_profile(results: Sequence[RunResult]) -> dict[str, list[RunResult]]:
    out: dict[str, list[RunResult]] = defaultdict(list)
    for result in results:
        out[result.profile_name].append(result)
    return out
