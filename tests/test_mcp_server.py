"""MCP 工具服务的外部验收：**官方 SDK 的 stdio 客户端**驱动真实服务进程。

这一组用例回答设计文档 C 的 M1 验收门："外部 MCP 客户端能列出工具并调用只读工具；
写操作停在审批门并返回可读的待审批结果；approve（含 reject / edit 改参）后续跑"。

* 打在 `mcp` marker 上：默认 addopts 过滤掉，CI 用 `pytest -m mcp` 显式跑（nightly）。
* `pytest.importorskip` 放在**函数体内**：缺 `[mcp]` extra 时是 skip 而不是 collect error，
  这样 `scripts/count_tests.py` 的用例计数在装与不装 extra 的环境里都稳定。
* 判分只认外部账本（`opsenv.oracle.read_effects`，连 `-wal` 一起快照），
  不读 runtime 自己的日志来断言"发生了几次"。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from opsenv.oracle import read_effects

pytestmark = pytest.mark.mcp

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 默认形态下服务端**绝不**使用的能力：elicitation 是可选增强，本包不实现它。
ELICITATION_MUST_NOT_BE_USED = "服务端不得向客户端发 elicitation 请求（审批默认形态即唯一形态）"


def _require_sdk() -> None:
    pytest.importorskip("mcp", reason="需要 [mcp] extra：pip install -e '.[mcp]'")
    pytest.importorskip("mcp.client.stdio")


async def _with_session(run_dir: Path, scenario: Any, **server_args: Any) -> Any:
    """起一个真实服务子进程，用官方 SDK 客户端连上去执行 ``scenario(session)``。"""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    argv = [sys.executable, "-m", "integrations.mcp_server", "--run-dir", str(run_dir)]
    for key, value in server_args.items():
        argv.extend([f"--{key.replace('_', '-')}", str(value)])
    params = StdioServerParameters(command=argv[0], args=argv[1:], cwd=str(PROJECT_ROOT))

    elicitation_calls: list[Any] = []

    async def _refuse_elicitation(_context: Any, _params: Any) -> Any:
        elicitation_calls.append(_params)
        raise AssertionError(ELICITATION_MUST_NOT_BE_USED)

    async with stdio_client(params) as (read_stream, write_stream), ClientSession(
        read_stream, write_stream, elicitation_callback=_refuse_elicitation
    ) as session:
        await session.initialize()
        payload = await scenario(session)
        return payload, elicitation_calls


def _run(run_dir: Path, scenario: Any, **server_args: Any) -> tuple[Any, list[Any]]:
    return asyncio.run(_with_session(run_dir, scenario, **server_args))


def _structured(result: Any) -> dict[str, Any]:
    """工具结果的 structuredContent；拿不到就退回解析文本块（协议兼容性兜底）。"""
    payload = getattr(result, "structured_content", None)
    if isinstance(payload, dict):
        return payload
    for block in result.content:
        if getattr(block, "type", "") == "text":
            return json.loads(block.text)
    raise AssertionError(f"结果里没有可读载荷：{result!r}")


def _names(tools: Any) -> list[str]:
    return sorted(tool.name for tool in tools.tools)


def _schema_of(tools: Any, name: str) -> dict[str, Any]:
    for tool in tools.tools:
        if tool.name == name:
            return tool.input_schema
    raise AssertionError(f"未下发工具 {name}：{_names(tools)}")


# ------------------------------------------------------------------ 只读与清单


def test_list_tools_reflects_the_registry_and_declares_schemas(tmp_path: Path) -> None:
    """tools/list 由注册表直接映射：写工具必须声明参数，治理工具对也必须在。"""
    _require_sdk()

    async def scenario(session: Any) -> Any:
        return await session.list_tools()

    tools, elicitation_calls = _run(tmp_path / "run", scenario)
    assert _names(tools) == [
        "approve",
        "create_ticket",
        "fetch_logs",
        "list_pending_approvals",
        "query_metrics",
        "scale_pool",
    ]
    # 写工具的参数 schema 必须真的声明了（未声明的工具会在客户端侧变成空参数表）
    for write_tool in ("scale_pool", "create_ticket"):
        schema = _schema_of(tools, write_tool)
        assert schema.get("properties"), f"{write_tool} 没有下发参数 schema"
        assert schema["type"] == "object"
    # 参数边界与执行前强制的 ArgPolicy 同域（两边不一致会把合法调用判成非法）
    assert _schema_of(tools, "scale_pool")["properties"]["size"]["maximum"] == 512
    assert elicitation_calls == []


def test_read_only_tool_executes(tmp_path: Path) -> None:
    """只读工具直接执行，并把结果作为工具结果返回。"""
    _require_sdk()
    run_dir = tmp_path / "run"

    async def scenario(session: Any) -> Any:
        return await session.call_tool("query_metrics", {"service": "payment"})

    result, _ = _run(run_dir, scenario)
    payload = _structured(result)
    assert payload["status"] == "executed"
    assert payload["replayed"] is False
    assert result.is_error is False
    assert payload["result"]["service"] == "payment"
    assert read_effects(run_dir)["ledger_rows"] == 0, "只读调用不该产生外部副作用"


# ------------------------------------------------------------------ 审批形态


def test_write_stops_at_the_approval_gate_then_runs_after_approval(tmp_path: Path) -> None:
    """写操作停在审批门 → 返回可读待审批结果 → approve 之后才执行（恰好一次）。"""
    _require_sdk()
    run_dir = tmp_path / "run"

    async def scenario(session: Any) -> dict[str, Any]:
        gated = _structured(
            await session.call_tool("scale_pool", {"service": "payment", "size": 64})
        )
        assert gated["status"] == "pending_approval"
        assert gated["tool_call_id"]
        assert gated["args_sha256"]
        assert gated["approval_ttl_seconds"] > 0
        assert read_effects(run_dir)["ledger_rows"] == 0, "停在审批门时不该有副作用"

        listed = _structured(await session.call_tool("list_pending_approvals", {}))
        assert [item["tool_call_id"] for item in listed["pending"]] == [gated["tool_call_id"]]
        assert listed["pending"][0]["is_current"] is True

        approved = _structured(
            await session.call_tool(
                "approve", {"tool_call_id": gated["tool_call_id"], "decision": "approve"}
            )
        )
        return {"gated": gated, "approved": approved}

    payload, elicitation_calls = _run(run_dir, scenario)
    approved = payload["approved"]
    assert approved["status"] == "approved"
    assert approved["decision"] == "approved"
    assert [item["status"] for item in approved["executed"]] == ["executed"]
    assert approved["run_status"] in {"running", "waiting_human"}
    assert elicitation_calls == []
    assert read_effects(run_dir)["max_per_key"] == 1, "批准之后恰好一次副作用"


def test_reject_closes_the_call_without_side_effect(tmp_path: Path) -> None:
    """拒绝是默认方向之一：调用被闭合为 rejected，且没有副作用。"""
    _require_sdk()
    run_dir = tmp_path / "run"

    async def scenario(session: Any) -> dict[str, Any]:
        gated = _structured(
            await session.call_tool("scale_pool", {"service": "payment", "size": 64})
        )
        rejected = _structured(
            await session.call_tool(
                "approve", {"tool_call_id": gated["tool_call_id"], "decision": "reject"}
            )
        )
        remaining = _structured(await session.call_tool("list_pending_approvals", {}))
        return {"gated": gated, "rejected": rejected, "remaining": remaining}

    payload, _ = _run(run_dir, scenario)
    assert payload["rejected"]["status"] == "rejected"
    assert payload["rejected"]["decision"] == "rejected"
    assert payload["rejected"]["executed"] == []
    assert payload["remaining"]["pending"] == []
    assert read_effects(run_dir)["ledger_rows"] == 0


def test_edit_approval_runs_with_the_edited_args(tmp_path: Path) -> None:
    """改参批准：原调用闭合为 superseded，另起新调用 + 新幂等键，副作用带新参数。"""
    _require_sdk()
    run_dir = tmp_path / "run"

    async def scenario(session: Any) -> dict[str, Any]:
        gated = _structured(
            await session.call_tool("scale_pool", {"service": "payment", "size": 64})
        )
        edited = _structured(
            await session.call_tool(
                "approve",
                {
                    "tool_call_id": gated["tool_call_id"],
                    "decision": "approve",
                    "approved_args": {"service": "payment", "size": 32},
                },
            )
        )
        return {"gated": gated, "edited": edited}

    payload, _ = _run(run_dir, scenario)
    edited = payload["edited"]
    original_id = payload["gated"]["tool_call_id"]
    assert edited["edited"] is True
    assert edited["tool_call_id"] == f"{original_id}__edit1", "改参必须换调用（因此换幂等键）"
    assert [item["status"] for item in edited["executed"]] == ["executed"]
    effects = read_effects(run_dir)
    assert effects["max_per_key"] == 1
    assert len(effects["counts"]) == 1, "改参后只有一次副作用"
    # 副作用里带的是**改后的**参数，不是原参数
    import sqlite3

    from harness.store.snapshot import snapshot_db

    con = sqlite3.connect(snapshot_db(run_dir / "world.db"))
    try:
        rows = con.execute("SELECT payload_json FROM effects").fetchall()
    finally:
        con.close()
    assert json.loads(rows[0][0])["size"] == 32


def test_approve_rejects_a_mismatched_tool_call_id(tmp_path: Path) -> None:
    """批准绑定"某次调用 + 某组参数"：接错调用必须被拒，而不是批准给别人。"""
    _require_sdk()
    run_dir = tmp_path / "run"

    async def scenario(session: Any) -> dict[str, Any]:
        await session.call_tool("scale_pool", {"service": "payment", "size": 64})
        wrong = _structured(
            await session.call_tool("approve", {"tool_call_id": "tc_not_this_one"})
        )
        right = _structured(await session.call_tool("list_pending_approvals", {}))
        return {"wrong": wrong, "right": right}

    payload, _ = _run(run_dir, scenario)
    assert payload["wrong"]["status"] == "rejected"
    assert payload["wrong"]["error_class"] == "tool_call_id_mismatch"
    assert len(payload["right"]["pending"]) == 1
    assert read_effects(run_dir)["ledger_rows"] == 0


# ------------------------------------------------------------------ 失败语义


def test_unregistered_tool_is_recorded_and_reported(tmp_path: Path) -> None:
    """未注册工具：可读错误 + 在日志里留下并闭合（"未注册"不等于"没发生"）。"""
    _require_sdk()
    run_dir = tmp_path / "run"

    async def scenario(session: Any) -> Any:
        return await session.call_tool("no_such_tool", {"anything": 1})

    result, _ = _run(run_dir, scenario)
    payload = _structured(result)
    assert result.is_error is True
    assert payload["status"] == "failed"
    assert payload["error_class"] == "unknown_tool"
    assert read_effects(run_dir)["ledger_rows"] == 0

    # 日志里那次尝试必须留下痕迹（tool_call + tool_result 成对闭合）
    from harness.store import SqliteStore

    ids = json.loads((run_dir / "ids.json").read_text(encoding="utf-8"))
    store = SqliteStore(run_dir / "runtime.db")
    store.setup()
    try:
        events = store.effective_events(ids["branch_id"])
    finally:
        store.close()
    types = [event.type for event in events]
    assert "tool_call" in types
    assert "tool_result" in types
    closed = [
        event.payload["status"] for event in events if event.type == "tool_result"
    ]
    assert closed == ["failed"]


def test_arg_policy_violation_is_rejected_before_the_handler(tmp_path: Path) -> None:
    """参数越出工具自述的安全域：执行前被拒，且不留意图行、不产生副作用。"""
    _require_sdk()
    run_dir = tmp_path / "run"

    async def scenario(session: Any) -> Any:
        # size 上限 512：这里给 9999。它是需审批工具，所以先停在审批门——
        # 这正是真实语义（审批门先于执行前闸门），批准后才会被策略拒。
        gated = _structured(
            await session.call_tool("scale_pool", {"service": "payment", "size": 9999})
        )
        return _structured(
            await session.call_tool(
                "approve", {"tool_call_id": gated["tool_call_id"], "decision": "approve"}
            )
        )

    payload, _ = _run(run_dir, scenario)
    assert [item["status"] for item in payload["executed"]] == ["rejected"]
    assert payload["executed"][0]["error_class"] == "arg_policy_violation"
    assert read_effects(run_dir)["ledger_rows"] == 0


def test_approve_without_pending_call_is_readable(tmp_path: Path) -> None:
    """没有待审批时 approve 给可读失败，而不是抛栈。"""
    _require_sdk()

    async def scenario(session: Any) -> Any:
        return await session.call_tool("approve", {"tool_call_id": "tc_none"})

    result, _ = _run(tmp_path / "run", scenario)
    payload = _structured(result)
    assert payload["error_class"] == "no_pending_interrupt"
    assert result.is_error is True
