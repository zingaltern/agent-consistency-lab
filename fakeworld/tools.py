"""仿真工具集与脚本化模型（确定性，无网络）。

工具声明与实现的关系：
``scale_pool`` 的**声明效果**随实现能力变化——下游支持幂等键时它声明的就是
``write_idempotent``，否则是 ``write_nonidempotent``。声明必须与真实行为一致，
否则"效果类别由工具声明"这条约束就失去意义。
"""

from __future__ import annotations

from typing import Any

from harness.artifacts import ArtifactStore, make_read_artifact_tool
from harness.tools import Effect, ProbeOutcome, ProbeResult, Tool, ToolRegistry

from .world import World

SCENARIO_POOL_EXHAUSTION = "pool_exhaustion"
SCENARIO_TWO_WRITES = "pool_exhaustion_two_writes"
SCENARIO_LONG_INCIDENT = "long_incident"


def build_registry(
    world: World,
    *,
    idempotent_impl: bool,
    probe_enabled: bool = True,
    artifacts: ArtifactStore | None = None,
) -> ToolRegistry:
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

    def fetch_logs(args: dict[str, Any], _key: str) -> dict[str, Any]:
        service = str(args.get("service", "unknown"))
        lines = int(args.get("lines", 40))
        entries = [
            f"2026-09-16T10:{index:02d}:00Z {service} level=WARN"
            f" pool_wait_ms={1800 + index * 7} active={40 + index} idle=2"
            ' msg="connection acquisition slow"'
            for index in range(lines)
        ]
        return {"service": service, "lines": lines, "entries": entries}

    def create_ticket(args: dict[str, Any], key: str) -> dict[str, Any]:
        return world.apply(
            operation="create_ticket",
            payload=dict(args),
            idempotency_key=key,
            idempotent_impl=True,
        )

    def scale_pool_probe(args: dict[str, Any], key: str) -> ProbeResult:
        exists, detail = world.probe(key)
        return ProbeResult(
            outcome=ProbeOutcome.APPLIED if exists else ProbeOutcome.NOT_APPLIED,
            detail=detail,
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
            description="扩容数据库连接池（高危写，需人工审批）",
            tags=("risky",),
            requires_approval=True,
            probe=scale_pool_probe if probe_enabled else None,
        )
    )
    registry.register(
        Tool(
            name="fetch_logs",
            effect=Effect.READ,
            fn=fetch_logs,
            description="拉取服务日志（只读，结果可能很大）",
            tags=("readonly", "big"),
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
    if artifacts is not None:
        registry.register(make_read_artifact_tool(artifacts))
    return registry


def _single_write_script() -> list[dict[str, Any]]:
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


def scripted_turns(scenario: str, *, steps: int = 8, lines: int = 60) -> list[dict[str, Any]]:
    """确定性脚本：每个 turn 要么提工具调用，要么给最终答复。

    ``steps`` / ``lines`` 只影响长任务场景的体量（W7 的阈值扫描要扫"任务有多长"）。
    """
    if scenario == SCENARIO_POOL_EXHAUSTION:
        return _single_write_script()
    if scenario == SCENARIO_LONG_INCIDENT:
        turns: list[dict[str, Any]] = [
            {
                "text": f"第 {index} 轮取证：拉取支付服务日志。",
                "tool_calls": [
                    {
                        "tool_call_id": f"tc_logs_{index}",
                        "tool": "fetch_logs",
                        "args": {"service": "payment", "lines": lines},
                    }
                ],
            }
            for index in range(1, max(1, steps) + 1)
        ]
        turns.append(_single_write_script()[1])
        turns.append({"text": "多轮取证后确认为连接池耗尽，扩容已完成。", "tool_calls": []})
        return turns
    if scenario == SCENARIO_TWO_WRITES:
        return [
            *_single_write_script()[:2],
            {
                "text": "再确认一次池大小。",
                "tool_calls": [
                    {
                        "tool_call_id": "tc_write_2",
                        "tool": "scale_pool",
                        "args": {"service": "payment", "size": 64},
                    }
                ],
            },
            {"text": "两次扩容完成，任务结束。", "tool_calls": []},
        ]
    raise ValueError(f"unknown scenario: {scenario!r}")
