"""路线 2：一次性读全部通道 + 单次推理 + 直接执行（无 gate）。

同文件里还有反事实基线 ``rule_full``（把规则表写全、读满四通道的规则引擎）——
它默认不参与主表，用来把"通道覆盖"与"架构"各自的贡献分开。
"""

from __future__ import annotations

import random
import time
from typing import Any

from harness.tokens import PriceTable

from ..environment import OpsEnvironment
from ..policy import Diagnosis, ReasonerProfile, diagnose
from ..scenario import (
    CHANGE_RECENCY_PREFIX,
    FAULT_SPECS,
    READ_CHANNELS,
    STATIC_DENY_LIST,
    FaultSpec,
    Scenario,
)
from .base import CHANNEL_ORDER, Operator, RunResult, _finish, _tokens_for_reads

SINGLE_SHOT_OUTPUT_TOKENS = 120


def rule_diagnose_full(channels: dict[str, dict]) -> Diagnosis:
    """读满四通道的规则引擎（"把规则表写全"的反事实）。

    审计指出：workflow 基线之所以差，是因为它**只被允许看两个通道**、且规则表只有
    5 个输出——那是建模选择，不是规则型架构的极限。这个函数用同样的规则思路读满
    四通道，用来量化"通道覆盖"与"架构"各自贡献了多少。

    实现要点（第一版写错过，这里记录原因）：**先在通道级定位异常，再在该通道的
    候选故障里比签名**。若对所有候选故障直接累加签名相似度，弱信号（0.4× 的多个键）
    会累加超过决定性通道的单键匹配，反而比只看两通道的基线的更差——实测只有 37.5%。
    """
    # 1) 通道级定位：第一个异常分越界的通道 = 决定性通道

    decisive = _decisive_channel_of(channels)
    if decisive is None:
        return Diagnosis(root_cause="no_known_pattern", action="none", detail="规则未命中")

    # 2) 在该通道的候选故障里比签名
    best: tuple[float, FaultSpec] | None = None
    data = channels.get(decisive) or {}
    for spec in FAULT_SPECS:
        if spec.decisive_channel != decisive:
            continue
        score = 0.0
        for key, expected in {**spec.metric_signature, **spec.resource_signature}.items():
            actual = data.get(key)
            if isinstance(actual, (int, float)) and expected:
                score += min(1.0, float(actual) / float(expected))
        if decisive == "logs" and spec.log_signature:
            score += (
                1.0
                if any(spec.log_signature[:24] in line for line in data.get("entries", []))
                else 0.0
            )
        if decisive == "changes" and spec.change_signature:
            score += (
                1.0
                if any(
                    spec.change_signature[:24] in str(entry) for entry in data.get("changes", [])
                )
                else 0.0
            )
        if best is None or score > best[0]:
            best = (score, spec)
    if best is None or best[0] <= 0:
        return Diagnosis(root_cause="no_known_pattern", action="none", detail="规则未命中")
    return Diagnosis(
        root_cause=best[1].fault.value,
        action=best[1].expected_action,
        sufficient=True,
        detail="完整规则表（读满四通道）",
    )


def _decisive_channel_of(channels: dict[str, dict]) -> str | None:
    """按通道级异常判据定位决定性通道（与 agent 路线的取证判据同源）。"""
    for channel in CHANNEL_ORDER:
        data = channels.get(channel) or {}
        if channel == "metrics":
            hits = 0
            hits += data.get("pool_wait_ms", 0) >= 1500
            hits += data.get("pool_active", 0) / max(1.0, data.get("pool_max", 64.0)) >= 0.9
            hits += data.get("cache_hit_ratio", 1.0) < 0.15
            hits += data.get("error_rate", 0) >= 0.02
            hits += data.get("handshake_error_rate", 0) >= 0.2
            hits += data.get("db_qps", 0) >= 10_000
            if hits >= 2:
                return channel
        elif channel == "resources":
            worst = max(
                data.get("memory_used_ratio", 0),
                data.get("disk_used_ratio", 0),
                data.get("cpu", 0),
            )
            if worst >= 0.9:
                return channel
        elif channel == "logs":
            if any("level=ERROR" in line for line in data.get("entries", [])):
                return channel
        elif channel == "changes":
            if any(
                str(entry.get("at", "")).startswith(CHANGE_RECENCY_PREFIX)
                for entry in data.get("changes", [])
            ):
                return channel
    return None


def run_rule_full(
    *,
    scenario: Scenario,
    profile: ReasonerProfile,
    rng: random.Random,
    operator: Operator,
    price: PriceTable | None = None,
    workdir: Any = None,
    noise_rng: random.Random | None = None,  # 规则基线同理：忽略
) -> RunResult:
    """反事实基线：把规则表写全（读 metrics+resources+logs+changes），其余与 workflow 同。"""
    price = price or PriceTable()
    started = time.perf_counter()
    env = OpsEnvironment(scenario=scenario)
    channels = {channel: env.read(channel) for channel in READ_CHANNELS}
    diagnosis = rule_diagnose_full(channels)
    blocked = False
    if env.is_write_action(diagnosis.action) and diagnosis.action in STATIC_DENY_LIST:
        blocked = True
    elif env.is_write_action(diagnosis.action):
        env.apply_action(diagnosis.action)
    return _finish(
        system="rule_full",
        profile_name=profile.name,
        scenario=scenario,
        env=env,
        diagnosis=diagnosis,
        gated=False,
        blocked=blocked,
        input_tokens=_tokens_for_reads(env),
        output_tokens=0,
        price=price,
        wall_ms=(time.perf_counter() - started) * 1000,
        note="反事实：读满四通道的规则引擎（审计要求的对照，用来分离'通道覆盖'与'架构'）",
    )


def run_single_shot(
    *,
    scenario: Scenario,
    profile: ReasonerProfile,
    rng: random.Random,
    operator: Operator,
    price: PriceTable | None = None,
    workdir: Any = None,  # 统一接口：只有 harness 需要落盘目录
    noise_rng: random.Random | None = None,
) -> RunResult:
    """一次读全部通道、一次推理、直接执行：没有 gate，没有验证。"""
    price = price or PriceTable()
    started = time.perf_counter()
    env = OpsEnvironment(scenario=scenario)
    for channel in READ_CHANNELS:
        env.read(channel)

    diagnosis = diagnose(
        scenario=scenario,
        channels=env.channels_read,
        profile=profile,
        rng=rng,
        noise_rng=noise_rng,
    )
    if diagnosis.action != "none":
        env.apply_action(diagnosis.action)
    return _finish(
        system="single_shot",
        profile_name=profile.name,
        scenario=scenario,
        env=env,
        diagnosis=diagnosis,
        gated=False,
        blocked=False,
        input_tokens=_tokens_for_reads(env),
        output_tokens=SINGLE_SHOT_OUTPUT_TOKENS,
        price=price,
        wall_ms=(time.perf_counter() - started) * 1000,
        note="一次性读全量证据；动作直接执行，无审批门",
    )
