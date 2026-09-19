"""噪声人格（R-A4）：把"评分口径敏感性"从一句声明变成一条可执行性质。

三条纪律在这个文件里被钉死：

1. **默认路径零改变**：``error_rate=0`` 时，即使把噪声流传进去，判定与结果逐位不变
   （噪声流是**独立**的随机流，不消耗判定流的随机数）；
2. **口径敏感性真的被激活**：``diagnosis_ok_action_wrong`` 形态下 ``strict ≤ cause_only``
   且至少有一条严格小于——这是 W5–W7 从未出现过的样本（"根因对、动作错"）；
3. **CRN**：噪声只由 ``noise_seed : scenario : repeat`` 决定，四条系统遇到同一串噪声——
   表现为同一次（场景 × 重复序号）上，不同系统提出**同一个**动作。
"""

from __future__ import annotations

import random

import pytest

from opsenv.policy import (
    Disposition,
    NoiseFlavor,
    ReasonerProfile,
    diagnose,
    noise_rng_for,
    perturb_diagnosis,
)
from opsenv.scenario import build_catalog
from opsenv.suite import (
    NOISE_ALLOWED_RED,
    aggregate,
    check_gates,
    grader_rates,
    noisy_profiles,
    run_suite,
)
from opsenv.systems import Operator

CATALOG = build_catalog(per_fault=1)
ALL_CHANNELS = frozenset({"metrics", "resources", "logs", "changes"})  # 取证充分


def _profile(**overrides) -> ReasonerProfile:
    base = ReasonerProfile(
        name="competent-honest", competence=0.9, disposition=Disposition.HONEST, seed=11
    )
    return base.model_copy(update=overrides)


# ------------------------------------------------------------------ 默认路径零改变


def test_q1_default_profiles_have_noise_disabled() -> None:
    """默认人格必须完全关闭噪声（否则既有结论口径就被悄悄换掉了）。"""
    for profile in noisy_profiles(error_rate=0.0):
        assert profile.error_rate == 0.0


def test_noise_disabled_is_bit_identical_even_with_noise_stream() -> None:
    """error_rate=0 时，传不传噪声流结果逐位相同（独立随机流的意义所在）。"""
    profile = _profile()
    scenario = CATALOG[0]
    for repeat in range(6):
        plain = diagnose(
            scenario=scenario,
            channels=set(ALL_CHANNELS),
            profile=profile,
            rng=random.Random(f"{profile.seed}:{scenario.id}:{repeat}"),
        )
        with_stream = diagnose(
            scenario=scenario,
            channels=set(ALL_CHANNELS),
            profile=profile,
            rng=random.Random(f"{profile.seed}:{scenario.id}:{repeat}"),
            noise_rng=noise_rng_for(profile, scenario_id=scenario.id, repeat=repeat),
        )
        assert plain.model_dump() == with_stream.model_dump()


def test_honest_abstain_is_not_perturbed() -> None:
    """证据不足 ⇒ 转人工（action=none）：噪声不把"承认不知道"变成乱动。"""
    profile = _profile(error_rate=1.0, flavor=NoiseFlavor.WRONG_DIAGNOSIS)
    scenario = CATALOG[0]
    result = diagnose(
        scenario=scenario,
        channels=set(),  # 决定性通道未取证
        profile=profile,
        rng=random.Random("x"),
        noise_rng=random.Random("y"),
    )
    assert result.action == "none"
    assert result.honest_abstain is True


# ------------------------------------------------------------------ 噪声形态


def test_wrong_diagnosis_flavor_changes_both_root_cause_and_action() -> None:
    scenario = CATALOG[0]
    profile = _profile(error_rate=1.0, flavor=NoiseFlavor.WRONG_DIAGNOSIS)
    result = diagnose(
        scenario=scenario,
        channels=set(ALL_CHANNELS),
        profile=profile,
        rng=random.Random("base"),
        noise_rng=random.Random("noise"),
    )
    assert result.root_cause != scenario.fault
    assert "噪声人格" in result.detail


def test_action_wrong_flavor_keeps_root_cause_and_breaks_strict_only() -> None:
    """本需求的全部意义：造出「根因对、动作错」的样本。"""
    scenario = CATALOG[0]
    profile = _profile(error_rate=1.0, flavor=NoiseFlavor.DIAGNOSIS_OK_ACTION_WRONG)
    result = diagnose(
        scenario=scenario,
        channels=set(ALL_CHANNELS),
        profile=profile,
        rng=random.Random("base"),
        noise_rng=random.Random("noise"),
    )
    assert result.root_cause == scenario.fault  # cause_only 仍然算对
    assert result.action != scenario.expected_action  # strict 判错


def test_perturb_diagnosis_is_deterministic_for_the_same_stream() -> None:
    scenario = CATALOG[0]
    profile = _profile(error_rate=1.0, flavor=NoiseFlavor.BOTH)
    base = diagnose(
        scenario=scenario,
        channels=set(ALL_CHANNELS),
        profile=_profile(),
        rng=random.Random("base"),
    )
    first = perturb_diagnosis(
        diagnosis=base, scenario=scenario, profile=profile, rng=random.Random("same")
    )
    second = perturb_diagnosis(
        diagnosis=base, scenario=scenario, profile=profile, rng=random.Random("same")
    )
    assert first.model_dump() == second.model_dump()


def test_error_rate_out_of_range_is_rejected() -> None:
    """非法的噪声强度必须在构造期报错，而不是悄悄跑出一个不可解释的口径。"""
    with pytest.raises(ValueError):
        noisy_profiles(error_rate=1.5)


def test_noisy_profiles_do_not_mutate_default_objects() -> None:
    original = noisy_profiles(error_rate=0.0)[0]
    noisy_profiles(error_rate=0.9)
    assert original.error_rate == 0.0


# ------------------------------------------------------------------ CRN / 口径


def test_noise_stream_ignores_system_and_is_repeatable() -> None:
    profile = _profile(noise_seed=4242, error_rate=0.5)
    first = noise_rng_for(profile, scenario_id="s1", repeat=0)
    second = noise_rng_for(profile, scenario_id="s1", repeat=0)
    other = noise_rng_for(profile, scenario_id="s1", repeat=1)
    assert [first.random() for _ in range(3)] == [second.random() for _ in range(3)]
    assert noise_rng_for(profile, scenario_id="s1", repeat=0).random() != other.random()


def test_action_wrong_flavor_activates_grader_sensitivity_in_a_run() -> None:
    """strict ≤ cause_only，且至少一条严格小于——口径敏感性**被激活**。

    只对 ``diagnosis_ok_action_wrong`` 断言"严格小于"：那一档**按定义**每次都造出
    「根因对、动作错」的样本，因此与样本量无关；``BOTH`` 是按概率混合的形态，
    它的"严格小于"是统计性质，见下一条测试（需要足够样本才不该假红）。
    """
    for flavor in (NoiseFlavor.DIAGNOSIS_OK_ACTION_WRONG,):
        results = run_suite(
            catalog=CATALOG[:4],
            systems=("single_shot", "langgraph"),
            profiles=noisy_profiles(error_rate=0.4, flavor=flavor),
            repeats=4,
            operator=Operator(),
        )
        table = grader_rates(results)
        systems = sorted(table["strict"])
        assert systems
        for system in systems:
            assert table["strict"][system] <= table["cause_only"][system] + 1e-12
        assert any(table["strict"][s] < table["cause_only"][s] for s in systems), (
            "噪声口径下 strict 与 cause_only 仍然完全相等——口径敏感性没有被激活"
        )


def test_both_flavor_mixes_the_two_forms_roughly_evenly() -> None:
    """``BOTH`` 是两种形态的混合：约各占一半。

    **P2-20 回归**：这里原来直接调 stdlib 的 ``random.choice`` 计数——它复刻了
    `opsenv/policy.py` 的那一行，生产代码改了混合逻辑它不会红。
    现在走 ``perturb_diagnosis``（生产路径），用产生的 ``detail`` 区分两种形态。
    """
    scenario = CATALOG[0]
    profile = _profile(error_rate=1.0, flavor=NoiseFlavor.BOTH)
    base = diagnose(
        scenario=scenario,
        channels=set(ALL_CHANNELS),
        profile=_profile(),
        rng=random.Random("base"),
    )
    wrong = ok_action = 0
    for index in range(300):
        perturbed = perturb_diagnosis(
            diagnosis=base,
            scenario=scenario,
            profile=profile,
            rng=random.Random(f"mix:{index}"),
        )
        if "根因也错" in perturbed.detail:
            wrong += 1
        elif "根因对、动作错" in perturbed.detail:
            ok_action += 1
    assert wrong + ok_action == 300, "两种形态必须覆盖全部样本（既不漏也不多）"
    assert 0.4 * 300 < wrong < 0.6 * 300, (wrong, ok_action)


def test_both_flavor_activates_sensitivity_on_a_large_enough_sample() -> None:
    results = run_suite(
        catalog=build_catalog(per_fault=1)[:8],
        systems=("single_shot",),
        profiles=noisy_profiles(error_rate=0.5, flavor=NoiseFlavor.BOTH),
        repeats=6,
        operator=Operator(),
    )
    table = grader_rates(results)
    system = "single_shot"
    assert table["strict"][system] <= table["cause_only"][system] + 1e-12
    assert table["strict"][system] < table["cause_only"][system], (
        "48 次运行里一次「根因对、动作错」都没有——要么混合比例坏了，要么样本还不够"
    )


def test_default_and_noisy_runs_share_the_same_proposed_actions_when_unperturbed() -> None:
    """CRN 的可观测形态：同一次（场景 × 重复）上两条系统的**提议动作**必须相同。

    如果噪声流里混进了 system 名，两条系统会在不同次运行上被扰动，这个断言会红
    （审计实测过同型问题：种子污染产生 p=0.0009 的伪影）。
    """
    results = run_suite(
        catalog=CATALOG[:4],
        systems=("single_shot", "langgraph"),
        profiles=noisy_profiles(error_rate=0.5, flavor=NoiseFlavor.DIAGNOSIS_OK_ACTION_WRONG),
        repeats=3,
        operator=Operator(),
    )
    by_key: dict[tuple[str, str, int], dict[str, str]] = {}
    for result in results:
        by_key.setdefault((result.scenario_id, result.profile_name, result.repeat), {})[
            result.system
        ] = result.action
    compared = 0
    for actions in by_key.values():
        if {"single_shot", "langgraph"} <= set(actions):
            assert actions["single_shot"] == actions["langgraph"]
            compared += 1
    assert compared >= 8, f"配对样本太少（{compared}），断言没有意义"


# ------------------------------------------------------------------ 门禁档位


def test_noise_downgrades_only_the_sensitivity_gates() -> None:
    """噪声口径只把"模型有多准"类门禁降级为如实汇报；机制类门禁永远强制。"""
    results = run_suite(
        catalog=CATALOG[:4],
        systems=("workflow", "single_shot", "langgraph", "harness"),
        profiles=noisy_profiles(error_rate=0.3),
        repeats=2,
        operator=Operator(),
    )
    cells = aggregate(results)
    summary = {"by_split": {"dev": 1, "holdout": 1}}
    gates = check_gates(cells, results, catalog_summary=summary, noisy=True)
    # 条数**不随口径变化**才是被验收的性质；绝对数写在正文里会随门禁增删而过期
    # （独立验证 2026-09-19 新增四条定义/CRN 门禁后，旧用例的 `== 14` 就是这么过期的）。
    # 条数本身由 claim `suite-gate-count` 守住。
    assert len(gates) == len(check_gates(cells, results, catalog_summary=summary, noisy=False)), (
        "门禁条数不随口径变化"
    )
    downgraded = {gate.name for gate in gates if not gate.enforced}
    assert downgraded == set(NOISE_ALLOWED_RED)
    for name in NOISE_ALLOWED_RED:
        assert name in {gate.name for gate in gates}


def test_default_mode_keeps_every_gate_enforced() -> None:
    """默认口径下没有任何门禁被降级（否则就是偷偷放宽门禁）。"""
    results = run_suite(
        catalog=CATALOG[:2], systems=("harness",), profiles=noisy_profiles(error_rate=0.0),
        repeats=1, operator=Operator(),
    )
    gates = check_gates(
        aggregate(results), results, catalog_summary={"by_split": {"dev": 1, "holdout": 1}}
    )
    assert all(gate.enforced for gate in gates)
