"""真实模型接入的工具 schema（R-B1 后续补全）。

背景：live 路径此前下发给模型的工具**只有名字与描述**（空参数表）。实测（DeepSeek）
在该形状下会用文字描述"我打算调用哪个工具"而不真正调用，于是"真实模型 → 调工具 →
产生副作用 → 崩溃恢复"这条链跑不通。本文件钉住补全后的行为：

1. 工具声明的 schema 必须原样下发（字段名/类型/边界都不能丢——模型据此生成参数，
   丢了就会生成非法参数，被执行前的策略检查拒绝）；
2. 未声明 schema 的工具**回退到空参数表**，形状与从前逐字相同（向后兼容）；
3. live 路径必须带上工具纪律的追加系统消息（否则模型又退回"用文字描述"）；
4. 这些改动**不影响 scripted 路径**：不装 `[otel]`、不联网，全部离线可跑。
"""

from __future__ import annotations

from fakeworld.tools import build_registry
from fakeworld.world import World
from harness.artifacts import DEFAULT_INLINE_TOKEN_BUDGET, ArtifactStore, make_read_artifact_tool
from harness.context import DEFAULT_MAX_INLINE_TOKENS
from harness.live_transport import LiveChatTransport, tools_schema_from_registry
from harness.prompts import LIVE_TOOL_USE_SYSTEM_PROMPT
from harness.tokens import estimate_tokens
from harness.tools import Effect, Tool, ToolRegistry


def _schema_of(tools: list[dict], name: str) -> dict:
    for item in tools:
        if item["function"]["name"] == name:
            return item["function"]["parameters"]
    raise AssertionError(f"未下发工具 {name}：{[t['function']['name'] for t in tools]}")


def test_registry_emits_declared_parameters() -> None:
    """fakeworld 的四个工具都声明了参数；scale_pool 的边界必须与 arg_policy 同域。"""
    registry = build_registry(World(":memory:"), idempotent_impl=False)
    tools = tools_schema_from_registry(registry)
    assert {"query_metrics", "scale_pool", "fetch_logs", "create_ticket"} <= {
        item["function"]["name"] for item in tools
    }

    metrics = _schema_of(tools, "query_metrics")
    assert metrics["required"] == ["service"]
    assert metrics["properties"]["service"]["type"] == "string"

    pool = _schema_of(tools, "scale_pool")
    size = pool["properties"]["size"]
    policy = registry.get("scale_pool").arg_policy
    assert policy is not None
    # schema 里给模型的边界 == 执行前强制的边界（两边不一致会让合法调用被判非法）
    assert size["minimum"] == policy.min
    assert size["maximum"] == policy.max
    assert set(pool["required"]) == {"service", "size"}

    logs = _schema_of(tools, "fetch_logs")
    assert logs["required"] == ["service"]  # lines 可省略（实现自带缺省）


def test_tools_without_declared_schema_fall_back_to_empty_object() -> None:
    """向后兼容：没声明 schema 的工具，下发形状与补全前逐字相同。"""
    bare = Tool(name="noop", effect=Effect.READ, fn=lambda args, key: {}, description="空的")
    tools = tools_schema_from_registry(ToolRegistry({"noop": bare}))
    assert tools[0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_live_payload_carries_schema_and_tool_use_prompt() -> None:
    """请求体里：工具带 schema、system 消息带工具纪律（离线构造，不发请求）。"""
    registry = build_registry(World(":memory:"), idempotent_impl=False)
    transport = LiveChatTransport(
        model="test-model",
        tools=tools_schema_from_registry(registry),
        system_prompt=LIVE_TOOL_USE_SYSTEM_PROMPT,
    )
    payload = transport._payload(("视图块一", "视图块二"))

    assert payload["messages"][0]["role"] == "system"
    assert "只发工具调用" in payload["messages"][0]["content"]
    assert payload["messages"][1]["content"] == "视图块一\n\n视图块二"
    assert _schema_of(payload["tools"], "scale_pool")["properties"]["size"]["maximum"] == 512


def test_read_artifact_slice_fits_the_inline_budget(tmp_path) -> None:
    """读回的切片必须装得进内联预算，否则会被再次卸载成包装层（实测的取证中断原因）。"""
    store = ArtifactStore(tmp_path / "a")
    ref = store.put("2026-09-16T10:00:00Z payment level=WARN pool_wait_ms=1800\n" * 400)
    tool = make_read_artifact_tool(store)

    result = tool.fn({"digest": ref.digest, "limit": 8000}, "k")
    assert estimate_tokens(result["content"]) <= result["inline_token_budget"]
    assert result["truncated"] is True
    assert result["next_offset"] == result["returned_chars"] > 0

    # 续读：从 next_offset 接着读，两次读到的内容不重叠
    follow = tool.fn({"digest": ref.digest, "offset": result["next_offset"]}, "k")
    assert follow["content"] and follow["content"] != result["content"]


def test_read_artifact_budget_matches_context_default() -> None:
    """两个常量分居两个模块（有环，不能互相 import），用这条用例守住它们不漂移。"""
    assert DEFAULT_INLINE_TOKEN_BUDGET == DEFAULT_MAX_INLINE_TOKENS
    # 裁剪要留余量：等于阈值时仍会被渲染层判成"超预算"
    assert 0 < 0.8 * DEFAULT_INLINE_TOKEN_BUDGET < DEFAULT_MAX_INLINE_TOKENS


def test_worker_live_transport_wires_prompt_and_schemas() -> None:
    """worker 的 live 接线：两样都要接上，否则模型退回"用文字描述工具调用"。"""
    from experiments.worker import _live_transport, parse_args

    registry = build_registry(World(":memory:"), idempotent_impl=False)
    args = parse_args(
        [
            "--run-dir",
            "/tmp/never-written",
            "--model",
            "record",
            "--transport",
            "live",
            "--record-dir",
            "/tmp/never-written",
            "--model-name",
            "deepseek-chat",
            "--budget-usd",
            "0.01",
        ]
    )
    transport = _live_transport(args, registry)
    assert transport.system_prompt.strip() != ""
    assert _schema_of(transport.tools, "query_metrics")["required"] == ["service"]
