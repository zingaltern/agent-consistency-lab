"""路线 4：本项目的 runtime——自适应取证 + 工具自述风险触发审批 + 事件日志 + outbox。

`OpsPolicyModel` 替代真实模型的行为（取证顺序、何时停止、给出什么动作），但**不替代**
runtime：审批、outbox、事件日志、视图全部走真实代码路径。
"""

from __future__ import annotations

import random
import time
from typing import Any

from harness.artifacts import ArtifactStore
from harness.chaos import Chaos
from harness.llm import ModelWindow, ScriptedLLMClient
from harness.loop import Loop
from harness.model import ModelTurn
from harness.store.checkpoints import SqliteCheckpointSaver
from harness.store.sqlite_store import SqliteStore
from harness.tokens import PriceTable, estimate_tokens
from harness.tools import Effect, Tool, ToolCallRequest, ToolRegistry

from ..environment import OpsEnvironment
from ..policy import Diagnosis, ReasonerProfile, diagnose
from ..scenario import ALL_ACTIONS, Scenario
from .base import (
    ANOMALY_THRESHOLD,
    CHANNEL_ORDER,
    Operator,
    RunResult,
    _finish,
    anomaly_score,
    cumulative_input_tokens,
)


class OpsPolicyModel:
    """harness 用的策略模型：按通道阶梯取证，够了就下结论并提出处置动作。

    它替代真实模型的行为（取证顺序、何时停止、给出什么动作全由 policy 决定），
    但**不替代** runtime：审批、outbox、事件日志、视图都走真实代码路径。
    """

    def __init__(
        self,
        env: OpsEnvironment,
        profile: ReasonerProfile,
        rng: random.Random,
        noise_rng: random.Random | None = None,
    ) -> None:
        self._env = env
        self._profile = profile
        self._rng = rng
        self._noise_rng = noise_rng
        self._read_order = list(CHANNEL_ORDER)
        self._proposals = 0
        self.last_diagnosis = ""

    @staticmethod
    def _turn(text: str, tool_calls: list[ToolCallRequest] | None = None) -> ModelTurn:
        """带上 usage：输出 token 按生成文本估算（缺了它输出成本会被记成 0）。"""
        return ModelTurn(
            text=text,
            tool_calls=tool_calls or [],
            usage={"completion_tokens": estimate_tokens(text)},
        )

    def next_turn(self, *, step: int, view: Any = None) -> ModelTurn:
        env = self._env
        if env.executed_actions:
            # 处置已执行 ⇒ 收束。缺了这一步会再走一遍取证/下结论逻辑：既覆盖诊断结论，
            # 又会重复提议同一个动作、再次开审批门，把 run 卡在 waiting_human。
            return self._turn(
                f"结论：{self.last_diagnosis}；处置 {env.executed_actions[-1]} 已执行。"
            )
        # 提交过处置请求但没有执行的动作 ⇒ 被人工拒绝，转人工，不再重复提议
        if self._proposals:
            return self._turn("处置请求未获批准，已转人工处理。")
        pending = [c for c in self._read_order if c not in env.channels_read]
        if pending:
            channel = pending[0]
            tool = {
                "metrics": ("query_metrics", {}),
                "resources": ("describe_resource", {}),
                "logs": ("fetch_logs", {"lines": 40}),
                "changes": ("list_recent_changes", {}),
            }[channel]
            # 上一轮刚读过、异常分够了 ⇒ 直接下结论
            if env.channels_read:
                last = [c for c in self._read_order if c in env.channels_read][-1]
                if anomaly_score(last, env) >= ANOMALY_THRESHOLD:
                    return self._conclude(step)
            return self._turn(
                text=f"取证：{channel}",
                tool_calls=[
                    ToolCallRequest(tool_call_id=f"tc_{channel}", tool=tool[0], args=tool[1])
                ],
            )
        return self._conclude(step)

    def _conclude(self, step: int) -> ModelTurn:
        result = diagnose(
            scenario=self._env.scenario,
            channels=self._env.channels_read,
            profile=self._profile,
            rng=self._rng,
            noise_rng=self._noise_rng,
        )
        self.last_diagnosis = result.root_cause
        if result.action == "none":
            return self._turn(f"结论：{result.root_cause}；证据不足，转人工。")
        self._proposals += 1
        return self._turn(
            text=f"结论：{result.root_cause}；建议处置 {result.action}。",
            tool_calls=[
                ToolCallRequest(
                    tool_call_id=f"tc_act_{step}",
                    tool=result.action,
                    args={"service": self._env.scenario.service},
                )
            ],
        )


def build_ops_registry(env: OpsEnvironment, artifacts: ArtifactStore | None = None) -> ToolRegistry:
    """运维壳的工具集：读工具 + 写工具（全部声明需要审批）。"""
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="query_metrics",
            effect=Effect.READ,
            fn=lambda args, key: env.query_metrics(),
            description="读取服务指标",
            tags=("readonly",),
        )
    )
    registry.register(
        Tool(
            name="fetch_logs",
            effect=Effect.READ,
            fn=lambda args, key: env.fetch_logs(int(args.get("lines", 40))),
            description="拉取服务日志",
            tags=("readonly",),
        )
    )
    registry.register(
        Tool(
            name="list_recent_changes",
            effect=Effect.READ,
            fn=lambda args, key: env.list_recent_changes(),
            description="列出最近的发布与配置变更",
            tags=("readonly",),
        )
    )
    registry.register(
        Tool(
            name="describe_resource",
            effect=Effect.READ,
            fn=lambda args, key: env.describe_resource(),
            description="查看资源水位",
            tags=("readonly",),
        )
    )
    for action in ALL_ACTIONS:
        registry.register(
            Tool(
                name=action,
                effect=Effect.WRITE_NONIDEMPOTENT,
                fn=(lambda name: lambda args, key: env.apply_action(name))(action),
                description=f"处置动作：{action}（高危写，需人工审批）",
                tags=("write", "risky"),
                requires_approval=True,
            )
        )
    return registry


def run_harness(
    *,
    scenario: Scenario,
    profile: ReasonerProfile,
    rng: random.Random,
    operator: Operator,
    price: PriceTable | None = None,
    workdir: Any = None,
    noise_rng: random.Random | None = None,
) -> RunResult:
    """本项目的 runtime：自适应取证 + 工具自述风险 → 审批门 + 事件日志 + outbox。"""
    import tempfile
    from pathlib import Path

    price = price or PriceTable()
    started = time.perf_counter()
    env = OpsEnvironment(scenario=scenario)
    directory = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="ops-"))
    directory.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(directory / "runtime.db")
    store.setup()
    artifacts = ArtifactStore(directory / "artifacts")
    registry = build_ops_registry(env, artifacts)
    policy_model = OpsPolicyModel(env, profile, rng, noise_rng=noise_rng)
    llm = ScriptedLLMClient(policy_model, window=ModelWindow(), price=price)
    loop = Loop(
        store,
        SqliteCheckpointSaver(store),
        llm=llm,
        registry=registry,
        compactor=None,
        budget=None,
        chaos=Chaos.disabled(),
    )
    run_id, thread_id, branch_id = f"run-{scenario.id}", f"thr-{scenario.id}", f"br-{scenario.id}"
    store.create_run(run_id, thread_id=thread_id)
    store.create_branch(branch_id, run_id)

    gated = False
    blocked = False
    outcomes = [
        loop.start(
            run_id=run_id,
            thread_id=thread_id,
            branch_id=branch_id,
            task=f"{scenario.service} 服务告警",
        )
    ]
    state = _run_state(store, branch_id)
    if state == "waiting_human":
        gated = True
        pending = _pending_action(store, branch_id)
        if pending and not operator.decide(scenario=scenario, action=pending):
            blocked = True
            loop.approve(
                run_id=run_id, thread_id=thread_id, branch_id=branch_id, decision="rejected"
            )
        else:
            loop.approve(run_id=run_id, thread_id=thread_id, branch_id=branch_id)
        outcomes.append(loop.resume(run_id=run_id, thread_id=thread_id, branch_id=branch_id))

    diagnosis, action = (
        policy_model.last_diagnosis,
        (
            env.executed_actions[-1]
            if env.executed_actions
            else (_pending_action(store, branch_id) or "none")
        ),
    )
    # 四类系统统一口径：前缀 + 已读证据（逐步累积）。
    # harness 内部账本（含缓存读写行）在 W4 报告里，不与本表混用，否则对照不可比。
    input_tokens = cumulative_input_tokens(env)
    output_tokens = _agent_output_tokens(store, branch_id)
    store.close()
    return _finish(
        system="harness",
        profile_name=profile.name,
        scenario=scenario,
        env=env,
        diagnosis=Diagnosis(
            root_cause=diagnosis,
            action=action,
            sufficient=scenario.is_sufficient(env.channels_read),
        ),
        gated=gated,
        blocked=blocked,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        price=price,
        wall_ms=(time.perf_counter() - started) * 1000,
        note="Loop + 审批绑定 + outbox + 事件日志（视图/不变量全程受断言保护）",
    )


def _agent_output_tokens(store: SqliteStore, branch_id: str) -> int:
    """输出 token 从 agent_message 的 usage 里取（模型自己报的数）。"""
    from harness.events import TreeEventType

    total = 0
    for event in store.effective_events(branch_id):
        if event.type == TreeEventType.AGENT_MESSAGE.value:
            total += int(event.payload.get("usage", {}).get("completion_tokens", 0) or 0)
    return total


def _run_state(store: SqliteStore, branch_id: str) -> str:
    from harness.state import reduce_events

    state, _ = reduce_events(store.effective_events(branch_id))
    return state.status.value


def _pending_action(store: SqliteStore, branch_id: str) -> str:
    from harness.events import TreeEventType

    for event in reversed(store.effective_events(branch_id)):
        if event.type == TreeEventType.INTERRUPT.value:
            return str(event.payload.get("tool", ""))
    return ""
