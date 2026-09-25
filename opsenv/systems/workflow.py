"""路线 1：确定性规则 + 静态拒绝列表（无模型调用）。"""

from __future__ import annotations

import random
import time
from typing import Any

from harness.tokens import PriceTable

from ..environment import OpsEnvironment
from ..policy import ReasonerProfile, rule_diagnose
from ..scenario import STATIC_DENY_LIST, Scenario
from .base import Operator, RunResult, _finish, _tokens_for_reads

WORKFLOW_CHANNELS: tuple[str, ...] = ("metrics", "resources")


def run_workflow(
    *,
    scenario: Scenario,
    profile: ReasonerProfile,
    rng: random.Random,
    operator: Operator,
    price: PriceTable | None = None,
    workdir: Any = None,  # 统一接口：只有 harness 需要落盘目录
    noise_rng: random.Random | None = None,  # 统一接口：规则基线没有推理器，噪声对它不适用
) -> RunResult:
    """规则 + 静态拒绝列表。无模型调用 ⇒ 输出 token 为 0，成本只含取证。

    噪声人格（R-A4）挂在**推理器**上；本条路线是纯规则表，没有推理器可扰动，因此
    ``noise_rng`` 只是接口占位。报告里必须如实写明这一点，否则「四系统都加了噪声」会是错话。
    """
    price = price or PriceTable()
    started = time.perf_counter()
    env = OpsEnvironment(scenario=scenario)
    for channel in WORKFLOW_CHANNELS:
        env.read(channel)

    diagnosis = rule_diagnose(scenario.evidence.metrics, scenario.evidence.resources)
    gated = False
    blocked = False
    if env.is_write_action(diagnosis.action):
        if diagnosis.action in STATIC_DENY_LIST:
            # 静态拒绝列表：机器自行否决，**没有**交给人工（gated 仍为 False）
            blocked = True
        else:
            # 静态列表之外的动作：规则直接执行（「未知的未知」在这里穿透）
            env.apply_action(diagnosis.action)
    result = _finish(
        system="workflow",
        profile_name=profile.name,
        scenario=scenario,
        env=env,
        diagnosis=diagnosis,
        gated=gated,
        blocked=blocked,
        input_tokens=_tokens_for_reads(env),
        output_tokens=0,
        price=price,
        wall_ms=(time.perf_counter() - started) * 1000,
        note="规则只覆盖 metrics+resources 可见的模式；写动作仅按静态拒绝列表拦截",
    )
    return result
