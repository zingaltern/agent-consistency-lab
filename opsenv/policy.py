"""推理器：把"模型能力"做成受控变量。

四类系统共用**同一个推理器**，因此系统之间的差异只能来自架构，而不能来自模型好坏。
推理器是确定性的（给定 scenario + 已收集通道 + 随机种子）：

* **证据充分性决定正确率**：只有收集到决定性通道，才有 ``competence`` 的概率给出正确诊断；
  否则正确率减半（能猜中纯属运气）。
* **误判落到混淆项上**：错误时从场景声明的混淆项里取一个，而不是随机瞎猜——
  这让"误判"是可信的运维误判（例如把慢查询当成连接池耗尽）。
* **误判动作有一半概率是破坏性的**：模拟"模型给出激进处置"。这条是红线指标的来源：
  有 gate 的系统把它交给人工，没有 gate 的系统直接执行。
* ``honesty``：证据不足时是否承认"证据不足"。承认了就不会乱动（但任务算失败），
  不承认则会按最强信号猜——两种气质在评测里是不同人格，用来观察系统对
  "模型硬猜"的放大效应。
"""

from __future__ import annotations

import random
from enum import StrEnum

from pydantic import BaseModel

from .scenario import Scenario


class Disposition(StrEnum):
    """推理器气质：承认不知道 / 硬猜。"""

    HONEST = "honest"
    GUESSER = "guesser"


class NoiseFlavor(StrEnum):
    """噪声人格的**误判形态**（决定它打穿哪个评分口径）。

    * ``wrong_diagnosis``：根因与动作一起错（既有误判分支的形态）——strict 与
      cause_only 同时下降，两者仍然相等；
    * ``diagnosis_ok_action_wrong``：**根因对、动作错**——只有 strict 下降、cause_only
      不变，于是"评分口径敏感性"才真正被激活（W5–W7 从未出现过的样本）；
    * ``both``：每次命中噪声时二选一（默认）。
    """

    WRONG_DIAGNOSIS = "wrong_diagnosis"
    DIAGNOSIS_OK_ACTION_WRONG = "diagnosis_ok_action_wrong"
    BOTH = "both"


class ReasonerProfile(BaseModel):
    name: str
    competence: float = 0.9  # 证据充分时的正确率
    disposition: Disposition = Disposition.HONEST
    destructive_error_ratio: float = 0.5  # 误判时采用破坏性动作的概率
    seed: int = 7
    # ---- 噪声人格（R-A4）：默认全关；error_rate=0 时判定路径与既有实现逐位一致 ----
    error_rate: float = 0.0
    flavor: NoiseFlavor = NoiseFlavor.BOTH
    noise_seed: int = 101  # 噪声抽样用**独立**种子（见 noise_rng_for）


def noise_rng_for(profile: ReasonerProfile, *, scenario_id: str, repeat: int) -> random.Random:
    """噪声的独立随机流，遵循 CRN 规则：种子 = ``noise_seed:scenario:repeat``，**不含 system 名**。

    为什么独立成流：若与判定流共用一个 ``rng``，即使 ``error_rate=0`` 也会因为多抽了几个数
    而改变后续所有判定（那会让"默认行为零改变"这条验收失效）。独立成流之后，同一次
    （场景 × 重复序号）在四条系统上遇到**同一串**噪声——这正是对照实验要的。
    """
    return random.Random(f"{profile.noise_seed}:{scenario_id}:{repeat}")


class Diagnosis(BaseModel):
    root_cause: str
    action: str
    sufficient: bool = False
    honest_abstain: bool = False
    detail: str = ""


def _misdiagnosis(
    *, scenario: Scenario, profile: ReasonerProfile, rng: random.Random
) -> tuple[str, str, str]:
    """误判形态：落到声明的混淆项上，动作按 destructive_error_ratio 取红线或常规动作。"""
    confuser = rng.choice(scenario.confusers) if scenario.confusers else "unknown_cause"
    forbidden = list(scenario.forbidden_actions)
    if scenario.novel_forbidden:
        forbidden.append(scenario.novel_forbidden)
    if forbidden and rng.random() < profile.destructive_error_ratio:
        return confuser, rng.choice(forbidden), "误判并给出破坏性处置"
    action = rng.choice(
        [
            a
            for a in ("scale_pool", "restart_workload", "clean_logs", "warm_cache")
            if a != scenario.expected_action
        ]
    )
    return confuser, action, "误判并给出常规处置"


def perturb_diagnosis(
    *,
    diagnosis: Diagnosis,
    scenario: Scenario,
    profile: ReasonerProfile,
    rng: random.Random,
) -> Diagnosis:
    """噪声人格：把一次判定改写成"运维上可信的错误"（R-A4）。

    两种形态的区别是本需求的**全部意义**：``wrong_diagnosis`` 只证明"模型会错"，
    ``diagnosis_ok_action_wrong`` 才让 strict 与 cause_only 分开——即证明评分口径本身有意义
    （W5–W7 的结论"两种口径完全相同"，正是因为缺这一档样本）。
    """
    flavor = profile.flavor
    if flavor is NoiseFlavor.BOTH:
        flavor = rng.choice([NoiseFlavor.WRONG_DIAGNOSIS, NoiseFlavor.DIAGNOSIS_OK_ACTION_WRONG])
    if flavor is NoiseFlavor.DIAGNOSIS_OK_ACTION_WRONG:
        _, action, detail = _misdiagnosis(scenario=scenario, profile=profile, rng=rng)
        return Diagnosis(
            root_cause=diagnosis.root_cause,
            action=action,
            sufficient=diagnosis.sufficient,
            detail=f"噪声人格（根因对、动作错）：{detail}",
        )
    confuser, action, detail = _misdiagnosis(scenario=scenario, profile=profile, rng=rng)
    return Diagnosis(
        root_cause=confuser,
        action=action,
        sufficient=diagnosis.sufficient,
        detail=f"噪声人格（根因也错）：{detail}",
    )


def diagnose(
    *,
    scenario: Scenario,
    channels: set[str],
    profile: ReasonerProfile,
    rng: random.Random,
    service_hint: str = "",
    noise_rng: random.Random | None = None,
) -> Diagnosis:
    """判定 + （可选）噪声扰动。

    纪律：``error_rate<=0`` 或没有噪声流时**立即返回**，一个随机数都不多抽——因此默认人格的
    判定与既有实现逐位一致（既有结论口径零改变）。
    """
    result = _diagnose_base(
        scenario=scenario, channels=channels, profile=profile, rng=rng, service_hint=service_hint
    )
    if noise_rng is None or profile.error_rate <= 0:
        return result
    if result.action == "none":
        # 证据不足 ⇒ 转人工（没有可扰动的处置）。噪声人格不该把"承认不知道"变成乱动，
        # 否则它测的就成了另一件事（气质），而不是评分口径的敏感性。
        return result
    if noise_rng.random() >= profile.error_rate:
        return result
    return perturb_diagnosis(diagnosis=result, scenario=scenario, profile=profile, rng=noise_rng)


def _diagnose_base(
    *,
    scenario: Scenario,
    channels: set[str],
    profile: ReasonerProfile,
    rng: random.Random,
    service_hint: str = "",
) -> Diagnosis:
    sufficient = scenario.is_sufficient(channels)
    if not sufficient and profile.disposition is Disposition.HONEST:
        return Diagnosis(
            root_cause="insufficient_evidence",
            action="none",
            sufficient=False,
            honest_abstain=True,
            detail=f"决定性通道 {scenario.decisive_channel} 未取证，拒绝下结论",
        )
    probability = profile.competence if sufficient else profile.competence * 0.4
    if rng.random() < probability:
        return Diagnosis(
            root_cause=scenario.fault,
            action=scenario.expected_action,
            sufficient=sufficient,
            detail="按证据给出的正确诊断",
        )
    # 误判：落到混淆项上；动作按 destructive_error_ratio 取红线动作或普通错误动作
    confuser, action, detail = _misdiagnosis(scenario=scenario, profile=profile, rng=rng)
    return Diagnosis(root_cause=confuser, action=action, sufficient=sufficient, detail=detail)


def rule_diagnose(metrics: dict, resources: dict) -> Diagnosis:
    """workflow 基线用的确定性规则表：只看 metrics + resources。

    规则表覆盖了"指标/资源可见"的故障；对决定性证据在日志或变更里的故障，
    规则只能指向指标上最像的那个混淆项——这就是规则型自动化的天花板。
    """

    def high(name: str, threshold: float) -> bool:
        value = metrics.get(name, resources.get(name, 0.0))
        return float(value) >= threshold

    if high("disk_used_ratio", 0.9):
        return Diagnosis(root_cause="disk_full", action="clean_logs", detail="规则：磁盘水位")
    if high("memory_used_ratio", 0.9):
        return Diagnosis(
            root_cause="memory_leak", action="restart_workload", detail="规则：内存水位"
        )
    if high("cache_hit_ratio", 0.0) and float(metrics.get("cache_hit_ratio", 1.0)) < 0.3:
        return Diagnosis(
            root_cause="cache_stampede", action="warm_cache", detail="规则：缓存命中率"
        )
    if high("pool_wait_ms", 1500) and high("pool_active", 55):
        return Diagnosis(
            root_cause="connection_pool_exhaustion", action="scale_pool", detail="规则：连接池等待"
        )
    if high("p99_ms", 1500):
        # 规则表只有一个"延迟高"分支：落到最常见的混淆项上
        return Diagnosis(
            root_cause="downstream_timeout",
            action="throttle_traffic",
            detail="规则：延迟高→按下游超时处置",
        )
    return Diagnosis(root_cause="no_known_pattern", action="none", detail="规则未命中，转人工")
