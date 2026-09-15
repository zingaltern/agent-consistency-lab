"""仿真工具集与脚本化模型（确定性，无网络）。

工具声明与实现的关系：
``scale_pool`` 的**声明效果**随实现能力变化——下游支持幂等键时它声明的就是
``write_idempotent``，否则是 ``write_nonidempotent``。声明必须与真实行为一致，
否则"效果类别由工具声明"这条约束就失去意义。
"""

from __future__ import annotations

from typing import Any

from harness.tools import Effect, Tool, ToolRegistry

from .world import World

SCENARIO_POOL_EXHAUSTION = "pool_exhaustion"


def build_registry(world: World, *, idempotent_impl: bool) -> ToolRegistry:
    registry = ToolRegistry()

    def query_metrics(args: dict[str, Any], _key: str) -> dict[str, Any]:
        service = args.get("service", "unknown")
        return {"service": service, "p99_ms": 2100, "pool_wait_ms": 1800, "replicas": 4}

    def scale_pool(args: dict[str, Any], key: str) -> dict[str, Any]:
        return world.apply(
            operation="scale_pool",
            payload=dict(args),
            idempotency_key=key,
            idempotent_impl=idempotent_impl,
        )

    def create_ticket(args: dict[str, Any], key: str) -> dict[str, Any]:
        return world.apply(
            operation="create_ticket",
            payload=dict(args),
            idempotency_key=key,
            idempotent_impl=True,
        )

    registry.register(
        Tool(
            name="query_metrics",
            effect=Effect.READ,
            fn=query_metrics,
            description="读取服务指标（只读）",
            tags=("readonly",),
        )
    )
    registry.register(
        Tool(
            name="scale_pool",
            effect=Effect.WRITE_IDEMPOTENT if idempotent_impl else Effect.WRITE_NONIDEMPOTENT,
            fn=scale_pool,
            description="扩容数据库连接池（写操作，需审批）",
            tags=("risky",),
        )
    )
    registry.register(
        Tool(
            name="create_ticket",
            effect=Effect.WRITE_IDEMPOTENT,
            fn=create_ticket,
            description="创建工单（幂等写）",
            tags=("write",),
        )
    )
    return registry


def scripted_turns(scenario: str) -> list[dict[str, Any]]:
    """确定性脚本：每个 turn 要么提工具调用，要么给最终答复。"""
    if scenario != SCENARIO_POOL_EXHAUSTION:
        raise ValueError(f"unknown scenario: {scenario!r}")
    return [
        {
            "text": "支付服务 P99 告警，先取指标。",
            "tool_calls": [
                {
                    "tool_call_id": "tc_read_1",
                    "tool": "query_metrics",
                    "args": {"service": "payment"},
                }
            ],
        },
        {
            "text": "连接池等待占满 P99，建议扩容连接池到 64。",
            "tool_calls": [
                {
                    "tool_call_id": "tc_write_1",
                    "tool": "scale_pool",
                    "args": {"service": "payment", "size": 64},
                }
            ],
        },
        {"text": "已扩容并确认指标回落，任务结束。", "tool_calls": []},
    ]
