"""路线 3：LangGraph 图（collect → diagnose → 静态断点人工审批 → act），带 MemorySaver。"""

from __future__ import annotations

import random
import time
from typing import Any, TypedDict

from harness.tokens import PriceTable

from ..environment import OpsEnvironment
from ..policy import Diagnosis, ReasonerProfile, diagnose
from ..scenario import Scenario
from .base import (
    ANOMALY_THRESHOLD,
    CHANNEL_ORDER,
    Operator,
    RunResult,
    _finish,
    anomaly_score,
    cumulative_input_tokens,
)

LANGGRAPH_OUTPUT_TOKENS = 60


class GraphState(TypedDict, total=False):
    """LangGraph 图的共享状态（必须是模块级类型：注解在模块命名空间求值）。"""

    index: int
    diagnosis: str
    action: str
    sufficient: bool
    abstain: bool


def run_langgraph(
    *,
    scenario: Scenario,
    profile: ReasonerProfile,
    rng: random.Random,
    operator: Operator,
    price: PriceTable | None = None,
    workdir: Any = None,  # 统一接口：只有 harness 需要落盘目录
    noise_rng: random.Random | None = None,
) -> RunResult:
    """LangGraph 等价图：collect（自适应）→ diagnose → [静态断点] gate → act。"""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    price = price or PriceTable()
    started = time.perf_counter()
    env = OpsEnvironment(scenario=scenario)

    def collect(state: GraphState) -> GraphState:
        index = state.get("index", 0)
        channel = CHANNEL_ORDER[index]
        env.read(channel)
        index += 1
        if anomaly_score(channel, env) >= ANOMALY_THRESHOLD or index >= len(CHANNEL_ORDER):
            return {"index": index}
        # 证据还不够：回到 collect 再取下一个通道（图上的自环 + 条件边）
        return {"index": index}

    def more_evidence(state: GraphState) -> str:
        if state["index"] >= len(CHANNEL_ORDER):
            return "diagnose"
        channel = CHANNEL_ORDER[state["index"] - 1]
        return "diagnose" if anomaly_score(channel, env) >= ANOMALY_THRESHOLD else "collect"

    def diagnose_node(state: GraphState) -> GraphState:
        result = diagnose(
            scenario=scenario,
            channels=env.channels_read,
            profile=profile,
            rng=rng,
            noise_rng=noise_rng,
        )
        return {
            "diagnosis": result.root_cause,
            "action": result.action,
            "sufficient": result.sufficient,
            "abstain": result.honest_abstain,
        }

    def act(state: GraphState) -> GraphState:
        action = state.get("action", "none")
        if action != "none":
            env.apply_action(action)
        return state

    graph = StateGraph(GraphState)
    graph.add_node("collect", collect)
    graph.add_node("diagnose", diagnose_node)
    graph.add_node("act", act)
    graph.add_edge(START, "collect")
    graph.add_conditional_edges(
        "collect", more_evidence, {"collect": "collect", "diagnose": "diagnose"}
    )
    graph.add_edge("diagnose", "act")
    graph.add_edge("act", END)

    app = graph.compile(checkpointer=MemorySaver(), interrupt_before=["act"])
    config = {"configurable": {"thread_id": f"lg-{scenario.id}"}}
    state = app.invoke({"index": 0}, config)

    gated = False
    blocked = False
    action = state.get("action", "none")
    if env.is_write_action(action):
        gated = True
        if not operator.decide(scenario=scenario, action=action):
            blocked = True
            # 人工拒绝：不继续执行（图停在断点之前）
        else:
            app.invoke(None, config)
    diagnosis = Diagnosis(
        root_cause=state.get("diagnosis", ""),
        action=action,
        sufficient=bool(state.get("sufficient")),
        honest_abstain=bool(state.get("abstain")),
    )
    return _finish(
        system="langgraph",
        profile_name=profile.name,
        scenario=scenario,
        env=env,
        diagnosis=diagnosis,
        gated=gated,
        blocked=blocked,
        input_tokens=cumulative_input_tokens(env),
        output_tokens=LANGGRAPH_OUTPUT_TOKENS,
        price=price,
        wall_ms=(time.perf_counter() - started) * 1000,
        note="图 + MemorySaver + interrupt_before 静态断点实现人工审批",
    )
